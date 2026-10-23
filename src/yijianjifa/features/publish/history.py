from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid
import json

from sqlalchemy import DateTime, Integer, String, select, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base, Setting


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PublishHistoryRow(Base):
    __tablename__ = "publish_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    video_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False, default='video', server_default='video')
    content_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True,
        default=lambda context: context.get_current_parameters().get('video_hash'))
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


@dataclass(frozen=True, slots=True)
class PublishHistoryRecord:
    id: str
    video_hash: str
    account_id: str
    attempt: int
    state: str
    task_id: str | None


class DuplicatePublishBlocked(RuntimeError):
    pass


_QUICK_BLOCK_STATES = {
    "published",
    "queued",
    "running",
    "platform_scheduled",
    "result_unknown",
    "pre_submit_failed",
    "paused_auth",
}
_V2_ACTIVE_STATES = {'waiting', 'waiting_adapter', 'waiting_configuration',
                     'missed_schedule', 'interrupted', 'platform_accepted', 'processing', 'reviewing'}
_QUICK_BLOCK_STATES |= _V2_ACTIVE_STATES
_ACTIVE_DUPLICATE_BLOCK_STATES = {"queued", "running", "platform_scheduled", "result_unknown", "paused_auth", "pre_submit_failed"} | _V2_ACTIVE_STATES


def rearrange_permission(session, row):
    """An explicit, unused grant for precisely this removed historical attempt."""
    if not row.task_id or row.state not in {'result_unknown', 'platform_scheduled', 'published'}:
        return None
    saved = session.get(Setting, 'publish-rearrange:' + row.id)
    if saved is None:
        return None
    grant = json.loads(saved.value_json)
    if grant.get('consumed_by') or grant.get('history_state') != row.state:
        return None
    from yijianjifa.app_services.publish_review import PublishReview
    _, entries, eligible, digest, _ = PublishReview(None)._read(session, row.task_id)
    if not eligible or not entries or entries[-1]['outcome'] != 'removed' or digest != grant.get('review_digest'):
        return None
    if grant.get('video_hash') != row.video_hash or grant.get('account_id') != row.account_id:
        return None
    return saved


def effective_history_state(session, row):
    return 'cancelled' if rearrange_permission(session, row) is not None else row.state


class PublishHistoryRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def record(self, video_hash: str, account_id: str, state: str, *, task_id: str | None = None) -> PublishHistoryRecord:
        with session_scope(self.engine) as session:
            max_attempt = session.scalar(
                select(func.max(PublishHistoryRow.attempt)).where(
                    PublishHistoryRow.video_hash == video_hash,
                    PublishHistoryRow.account_id == account_id,
                )
            )
            row = PublishHistoryRow(
                video_hash=video_hash,
                account_id=account_id,
                attempt=(max_attempt or 0) + 1,
                state=state,
                task_id=task_id,
            )
            session.add(row)
            session.flush()
            if state == 'queued' and task_id:
                for previous in session.scalars(select(PublishHistoryRow).where(
                        PublishHistoryRow.video_hash == video_hash, PublishHistoryRow.account_id == account_id,
                        PublishHistoryRow.id != row.id)):
                    permission = rearrange_permission(session, previous)
                    if permission is not None:
                        grant = json.loads(permission.value_json)
                        grant.update(consumed_by=task_id, consumed_history_id=row.id,
                                     consumed_at=_utcnow().isoformat())
                        permission.value_json = json.dumps(grant, ensure_ascii=False)
            return self._record(row)

    def has_quick_block(self, video_hash: str) -> bool:
        with session_scope(self.engine) as session:
            return any(effective_history_state(session, row) in _QUICK_BLOCK_STATES
                       for row in session.scalars(select(PublishHistoryRow).where(
                           PublishHistoryRow.video_hash == video_hash)))

    def assert_can_create(self, video_hash: str, account_id: str, *, allow_repeat_success: bool, allow_new_request=False) -> None:
        with session_scope(self.engine) as session:
            states = {effective_history_state(session, row) for row in session.scalars(
                select(PublishHistoryRow).where(PublishHistoryRow.video_hash == video_hash,
                                               PublishHistoryRow.account_id == account_id))}
        blocking = (self.target_blocking_states(states,allow_new_request=True) if allow_new_request else states & _ACTIVE_DUPLICATE_BLOCK_STATES)
        if "published" in states and not allow_repeat_success and not allow_new_request:
            raise DuplicatePublishBlocked("video was already published to this account; explicit repeat confirmation required")
        if blocking:
            raise DuplicatePublishBlocked(f"unresolved publish state blocks duplicate: {sorted(blocking)}")

        with session_scope(self.engine) as session:
            try:
                self.assert_no_other_target_history(session, video_hash, account_id,
                    allow_repeat_success=allow_repeat_success, legacy=True,allow_new_request=allow_new_request)
            except ValueError as exc:
                raise DuplicatePublishBlocked(str(exc)) from exc

    def states_for_video(self, video_hash: str) -> set[str]:
        with session_scope(self.engine) as session:
            return set(
                session.scalars(
                    select(PublishHistoryRow.state).where(PublishHistoryRow.video_hash == video_hash)
                ).all()
            )

    @staticmethod
    def target_blocking_states(states, *, allow_repeat_success=False,allow_new_request=False):
        """One conservative predicate for candidates and all V2 write paths."""
        if type(allow_repeat_success) is not bool:
            raise ValueError('invalid repeat confirmation')
        if type(allow_new_request) is not bool:raise ValueError('invalid new publication intent')
        if allow_new_request:
            return set(states) & {'queued','waiting','running','paused_auth','identity_unverified'}
        clear={'cancelled','platform_rejected'}
        if allow_repeat_success:clear.add('published')
        return set(states)-clear

    @staticmethod
    def _target_identities(session, account_ids):
        from yijianjifa.features.accounts.service import AccountRow
        from .platforms.catalog import PlatformCatalog
        if (type(account_ids) is not list or not 1 <= len(account_ids) <= 200 or
                any(type(aid) is not str or not 1 <= len(aid) <= 64 for aid in account_ids) or
                len(set(account_ids)) != len(account_ids)):
            raise ValueError('invalid publish targets')
        catalog = PlatformCatalog()
        identities = {}
        for aid in account_ids:
            account = session.get(AccountRow, aid)
            if account is None or account.login_state == 'removed':
                raise ValueError('publish target does not exist')
            catalog.get(account.platform)
            if not account.platform_user_id:
                raise ValueError('publish target identity is missing')
            identity = (account.platform, account.platform_user_id)
            if identity in identities.values():
                raise ValueError('duplicate publish target identity')
            identities[aid] = identity
        return identities

    @staticmethod
    def _frozen_history_identity(session, row, cache):
        """Resolve precisely the recorded account, even after metadata removal."""
        if not row.task_id:
            return None
        if row.task_id not in cache:
            from .normal_service import PublishTaskRow
            from yijianjifa.features.tasks.service import TaskCenterRow
            identities = set()
            snapshots = [session.get(model, row.task_id) for model in (PublishTaskRow, TaskCenterRow)]
            for task in snapshots:
                if task is None:
                    continue
                snapshot = json.loads(task.snapshot_json)
                if type(snapshot) is not dict:
                    raise ValueError('historical target snapshot is invalid')
                for account in snapshot.get('accounts', []):
                    if type(account) is not dict:
                        raise ValueError('historical target identity is invalid')
                    aid, platform, uid = account.get('id'), account.get('platform'), account.get('platform_user_id')
                    if (type(aid) is str and type(platform) is str and platform and
                            type(uid) is str and uid):
                        identities.add((aid, platform, uid))
            cache[row.task_id] = identities
        found = {(platform, uid) for aid, platform, uid in cache[row.task_id] if aid == row.account_id}
        if len(found) > 1:
            raise ValueError('historical target identity conflicts')
        return next(iter(found), None)

    @staticmethod
    def assert_no_other_target_history(session, video_hash, account_id, *, history_id=None,
                                      identity=None, allow_repeat_success=False, legacy=False,allow_new_request=False):
        """Shared execution gate; exclude only the caller's precise history row.

        Frozen stable identities take priority over mutable account metadata.
        V1's explicit successful-repeat permission remains V1-only.
        """
        from yijianjifa.features.accounts.service import AccountRow
        cache = {}
        own = session.get(PublishHistoryRow, history_id) if history_id else None
        if identity is None and own is not None:
            identity = PublishHistoryRepository._frozen_history_identity(session, own, cache)
        if identity is None:
            account = session.get(AccountRow, account_id)
            if account is not None and account.platform_user_id:
                identity = (account.platform, account.platform_user_id)
        for row in session.scalars(select(PublishHistoryRow).where(PublishHistoryRow.video_hash == video_hash)):
            if row.id == history_id:
                continue
            state = effective_history_state(session, row)
            blocked = (bool(PublishHistoryRepository.target_blocking_states({state},allow_new_request=True)) if allow_new_request else
                       ((state in _ACTIVE_DUPLICATE_BLOCK_STATES or (state=='published' and not allow_repeat_success)) if legacy else bool(PublishHistoryRepository.target_blocking_states({state}))))
            if not blocked:
                continue
            other = PublishHistoryRepository._frozen_history_identity(session, row, cache)
            if other is None:
                account = session.get(AccountRow, row.account_id)
                if account is not None and account.platform_user_id:
                    other = (account.platform, account.platform_user_id)
            if (row.account_id == account_id or (identity is not None and
                    (other is None or other == identity))):
                raise ValueError('existing target history blocks duplicate submission')

    def states_for_targets(self, account_ids):
        """Internal hash/account index, keyed by platform + stable UID identity.

        Missing historical identity cannot prove a different target. Preserve a
        verification blocker for that video rather than letting re-add bypass it.
        No raw stable identity is returned to callers or public DTOs.
        """
        from yijianjifa.features.accounts.service import AccountRow
        with session_scope(self.engine) as session:
            identities = self._target_identities(session, account_ids)
            owners = {identity: aid for aid, identity in identities.items()}
            result, cache = {}, {}
            rows = session.execute(select(PublishHistoryRow, AccountRow.platform, AccountRow.platform_user_id)
                                   .outerjoin(AccountRow, AccountRow.id == PublishHistoryRow.account_id))
            for row, platform, uid in rows:
                if row.content_type != 'video':
                    continue
                state = effective_history_state(session, row)
                identity = self._frozen_history_identity(session, row, cache)
                if identity is None and platform and uid:
                    identity = (platform, uid)
                if row.account_id in identities and identity is not None and identity != identities[row.account_id]:
                    raise ValueError('target identity differs from frozen history')
                if identity in owners:
                    result.setdefault((row.video_hash, owners[identity]), set()).add(state)
                elif identity is None and self.target_blocking_states({state}):
                    for aid in account_ids:
                        result.setdefault((row.video_hash, aid), set()).add('identity_unverified')
            return result

    @staticmethod
    def assert_no_other_content_history(session, content_type, content_hash, account_id, *, history_id=None,
                                        identity=None, allow_new_request=False):
        if content_type == 'video':
            return PublishHistoryRepository.assert_no_other_target_history(session, content_hash, account_id,
                history_id=history_id, identity=identity, allow_new_request=allow_new_request)
        if content_type != 'image_post' or not content_hash:
            raise ValueError('invalid content history identity')
        from yijianjifa.features.accounts.service import AccountRow
        cache = {}
        for row in session.scalars(select(PublishHistoryRow).where(
                PublishHistoryRow.content_type == content_type, PublishHistoryRow.content_hash == content_hash)):
            if row.id == history_id:
                continue
            if not PublishHistoryRepository.target_blocking_states({row.state}, allow_new_request=allow_new_request):
                continue
            other = PublishHistoryRepository._frozen_history_identity(session, row, cache)
            if other is None:
                account = session.get(AccountRow, row.account_id)
                if account and account.platform_user_id:
                    other = (account.platform, account.platform_user_id)
            if row.account_id == account_id or identity is not None and (other is None or other == identity):
                raise ValueError('existing target history blocks duplicate submission')

    def states_for_target(self, video_hash, account_id):
        return self.states_for_targets([account_id]).get((video_hash, account_id), set())

    def assert_target_can_create(self, video_hash, account_id, *, allow_repeat_success=False,allow_new_request=False):
        states = self.states_for_target(video_hash, account_id)
        if self.target_blocking_states(states, allow_repeat_success=allow_repeat_success,allow_new_request=allow_new_request):
            raise DuplicatePublishBlocked('existing or unverified target delivery blocks duplicate')

    @staticmethod
    def _record(row: PublishHistoryRow) -> PublishHistoryRecord:
        return PublishHistoryRecord(row.id, row.video_hash, row.account_id, row.attempt, row.state, row.task_id)
