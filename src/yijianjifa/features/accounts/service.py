from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import uuid

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.features.accounts.models import Account, CapabilitySnapshot
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base, Setting


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_LOGIN_CHECK_PREFIX = 'account-login-check:'
_LOGIN_CHECK_STATES = {'valid', 'login_required', 'identity_mismatch', 'unavailable'}
_LOGIN_CHECK_REASONS = {'timeout', 'network_unavailable', 'verification_required', 'identity_unavailable',
    'session_unavailable', 'cleanup_failed', 'platform_unavailable', 'session_changed', 'invalid_provider_result',
    'account_busy', 'login_in_progress', 'account_unavailable', 'identity_mismatch', 'login_required',
    'session_missing', 'provider_unavailable'}
_LOGIN_CHECK_STAGES = {'session_read', 'profile_read', 'clone', 'driver_start', 'browser_launch',
    'session_restore', 'navigation_first', 'navigation_second', 'surface', 'identity_first', 'identity_second',
    'context_close', 'driver_stop', 'cleanup', 'worker_start', 'parent_deadline'}


def _check_detail(reason, diagnostic):
    if reason is not None and (type(reason) is not str or reason not in _LOGIN_CHECK_REASONS):
        raise ValueError('invalid login check cause')
    if diagnostic is not None and (type(diagnostic) is not dict or set(diagnostic) != {'stage', 'elapsed_ms'}
            or type(diagnostic['stage']) is not str or diagnostic['stage'] not in _LOGIN_CHECK_STAGES
            or type(diagnostic['elapsed_ms']) is not int or not 0 <= diagnostic['elapsed_ms'] <= 120000):
        raise ValueError('invalid login check detail')
    return {'reason': reason, 'diagnostic': dict(diagnostic) if diagnostic is not None else None}


def _login_check_fingerprint(account) -> str:
    value = json.dumps([account.platform, account.platform_user_id, account.session_ref],
                       ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _observation_time(value=None) -> datetime:
    value = _utcnow() if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('login observation needs an aware time')
    return value.astimezone(timezone.utc)


def _saved_login_check(account, value_json):
    try:
        value = json.loads(value_json)
        base = {'v', 'fingerprint', 'checked_at', 'state'}
        if (type(value) is not dict or type(value.get('v')) is not int
                or not (value['v'] == 1 and set(value) == base or
                        value['v'] == 2 and set(value) == base | {'reason', 'diagnostic'})
                or value['fingerprint'] != _login_check_fingerprint(account)
                or type(value['state']) is not str or value['state'] not in _LOGIN_CHECK_STATES
                or type(value['checked_at']) is not str):
            return None
        checked_at = _observation_time(datetime.fromisoformat(value['checked_at']))
        result = {'checked_at': checked_at.isoformat(), 'state': value['state']}
        if value['v'] == 2:
            detail = _check_detail(value['reason'], value['diagnostic'])
            result.update({key: item for key, item in detail.items() if item is not None})
        return result
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


class AccountRow(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform", "platform_user_id", name="uq_account_platform_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    platform_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    nickname: Mapped[str] = mapped_column(String(255), nullable=False)
    remark: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    group_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    login_state: Mapped[str] = mapped_column(String(32), nullable=False, default="valid")
    session_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_json: Mapped[str] = mapped_column(Text, nullable=False, default='{"ordinary":"pending","commerce":"pending","location_scope":"pending"}')
    avatar_data: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class AccountIdentityMismatch(RuntimeError):
    pass


class AccountService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(
        self,
        platform: str,
        platform_user_id: str,
        nickname: str,
        *,
        remark: str = "",
        group_name: str = "",
    ) -> Account:
        platform = str(platform).strip()
        platform_user_id = str(platform_user_id).strip()
        nickname = str(nickname).strip()
        if not platform or not platform_user_id or not nickname:
            raise ValueError("platform, platform_user_id and nickname are required")
        row = AccountRow(
            platform=platform,
            platform_user_id=platform_user_id,
            nickname=nickname,
            remark=str(remark or "").strip(),
            group_name=str(group_name or "").strip(),
            capability_json=json.dumps(asdict(CapabilitySnapshot()), ensure_ascii=False, sort_keys=True),
        )
        with session_scope(self.engine) as session:
            previous = session.scalar(select(AccountRow).where(AccountRow.platform == platform, AccountRow.platform_user_id == platform_user_id, AccountRow.login_state == 'removed'))
            if previous is not None:
                previous.nickname=nickname; previous.remark=row.remark; previous.group_name=row.group_name
                previous.login_state='pending_verification'; previous.session_ref=None
                previous.capability_json=row.capability_json; previous.avatar_data=''; previous.is_default=False
                row=previous
            else:
                session.add(row)
            session.flush()
            return self._record(row)

    def get(self, account_id: str) -> Account:
        with session_scope(self.engine) as session:
            row = session.get(AccountRow, account_id)
            if row is None or row.login_state == "removed":
                raise KeyError(account_id)
            return self._record(row)

    def list(self, platform: str | None = None) -> list[Account]:
        with session_scope(self.engine) as session:
            stmt = select(AccountRow).where(AccountRow.login_state != "removed")
            if platform is not None:
                stmt = stmt.where(AccountRow.platform == platform)
            rows = session.scalars(stmt.order_by(AccountRow.platform, AccountRow.nickname, AccountRow.id)).all()
            return [self._record(row) for row in rows]

    @staticmethod
    def record_login_check(session, account, state: str, *, observed_at=None, reason=None, diagnostic=None) -> bool:
        """The native runtime calls this inside its same-version transaction."""
        missing = state == 'session_missing'
        state = 'login_required' if missing else state
        if type(state) is not str or state not in _LOGIN_CHECK_STATES:
            raise ValueError('invalid login check state')
        detail = _check_detail(reason, diagnostic)
        observed_at = _observation_time(observed_at)
        if state == 'valid' and not account.session_ref:
            return False
        key = _LOGIN_CHECK_PREFIX + account.id
        fingerprint = _login_check_fingerprint(account)
        row = session.get(Setting, key)
        if row is not None:
            previous = _saved_login_check(account, row.value_json)
            if previous is not None and datetime.fromisoformat(previous['checked_at']) >= observed_at:
                return False
        if state == 'valid':
            account.login_state = 'valid'
        elif state == 'login_required':
            account.login_state = 'pending_verification' if missing or not account.session_ref else 'expired'
        value = {'v': 1, 'fingerprint': fingerprint, 'checked_at': observed_at.isoformat(), 'state': state}
        if reason is not None or diagnostic is not None:
            value.update(v=2, **detail)
        value = json.dumps(value, sort_keys=True)
        if row is None:
            session.add(Setting(key=key, value_json=value))
        else:
            row.value_json = value
        return True

    def observe_login_check(self, account_id: str, *, platform_user_id: str, session_ref,
                           state: str, observed_at=None, reason=None, diagnostic=None) -> bool:
        """Accept native identity evidence only for its unchanged saved archive.

        The start time of the observation prevents a slow, older request from
        replacing evidence collected by a newer login or publishing operation.
        """
        observed_at = _observation_time(observed_at)
        if type(state) is not str or state not in _LOGIN_CHECK_STATES | {'session_missing'}:
            raise ValueError('invalid login check state')
        with self.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(connection) as session:
                    account = session.get(AccountRow, account_id)
                    if (account is None or account.login_state == 'removed'
                            or account.platform_user_id != platform_user_id or account.session_ref != session_ref):
                        accepted = False
                    else:
                        accepted = self.record_login_check(session, account, state, observed_at=observed_at,
                                                           reason=reason, diagnostic=diagnostic)
                connection.commit()
                return accepted
            except BaseException:
                connection.rollback()
                raise

    def login_checks(self, accounts) -> dict:
        """Return display-only evidence for the current accepted session versions.

        Account reads never start a browser or refresh online credentials. A new
        login archive cannot inherit an earlier archive's successful check.
        """
        accounts = {account.id: account for account in accounts}
        result = {account_id: None for account_id in accounts}
        if not accounts:
            return result
        keys = [_LOGIN_CHECK_PREFIX + account_id for account_id in accounts]
        with session_scope(self.engine) as session:
            rows = session.scalars(select(Setting).where(Setting.key.in_(keys))).all()
            for row in rows:
                account_id = row.key[len(_LOGIN_CHECK_PREFIX):]
                result[account_id] = _saved_login_check(accounts[account_id], row.value_json)
        return result

    def remove(self, account_id: str):
        """Retain platform identity and historical references; remove local access."""
        from yijianjifa.features.tasks.service import TaskCenterRow
        from yijianjifa.features.mixing.queue import MixTask
        from yijianjifa.features.publish.normal_service import PublishTaskRow
        from yijianjifa.features.commerce.service import CommerceTaskRow
        from yijianjifa.app_services.library_queries import CommandRejected
        def references(value):
            if isinstance(value, dict): return any(references(v) for v in value.values())
            if isinstance(value, list): return any(references(v) for v in value)
            return value == account_id
        with session_scope(self.engine) as session:
            row=session.get(AccountRow,account_id)
            if row is None: raise KeyError(account_id)
            if row.login_state=='removed': return {'removed':True}
            terminal={'completed','completed_with_errors','cancelled','published','failed'}
            for model in (TaskCenterRow,MixTask,PublishTaskRow,CommerceTaskRow):
                for task in session.scalars(select(model).where(model.state.not_in(terminal))):
                    if getattr(task,'account_id',None)==account_id or references(json.loads(task.snapshot_json)):
                        raise CommandRejected('account_delete_blocked')
            from yijianjifa.storage.models import Setting
            for cached in session.scalars(select(Setting).where(Setting.key.startswith(f'publish-metadata:{account_id}:'))):
                session.delete(cached)
            row.is_default=False;row.login_state='removed';row.session_ref=None
            row.avatar_data='';row.capability_json=json.dumps(asdict(CapabilitySnapshot()))
            return {'removed':True}

    def set_default(self, platform: str, account_id: str) -> Account:
        with session_scope(self.engine) as session:
            row = session.get(AccountRow, account_id)
            if row is None or row.login_state == "removed":
                raise KeyError(account_id)
            if row.platform != platform:
                raise ValueError("account does not belong to requested platform")
            session.execute(
                update(AccountRow)
                .where(AccountRow.platform == platform)
                .values(is_default=False)
            )
            row.is_default = True
            session.flush()
            return self._record(row)

    def update_metadata(self, account_id: str, *, remark: str, group_name: str) -> Account:
        if type(remark) is not str or len(remark)>255 or type(group_name) is not str or len(group_name)>128:
            raise ValueError('invalid account metadata')
        with session_scope(self.engine) as session:
            row=session.get(AccountRow,account_id)
            if row is None or row.login_state=="removed":raise KeyError(account_id)
            row.remark=remark.strip()
            row.group_name=group_name.strip()
            session.flush()
            return self._record(row)

    def default(self, platform: str) -> Account | None:
        with session_scope(self.engine) as session:
            row = session.scalar(
                select(AccountRow).where(
                    AccountRow.platform == platform,
                    AccountRow.is_default.is_(True),
                ).limit(1)
            )
            return None if row is None else self._record(row)

    def assert_same_identity(self, account_id: str, observed_platform_user_id: str) -> None:
        account = self.get(account_id)
        if account.platform_user_id != str(observed_platform_user_id).strip():
            raise AccountIdentityMismatch(
                f"relogin identity mismatch for {account_id}: expected {account.platform_user_id}"
            )

    def attach_session(self, account_id: str, store: SessionStore, secret_blob: bytes) -> str:
        # Verify account exists before writing a profile directory.
        self.get(account_id)
        # Immutable versions keep the committed reference usable if DB commit fails
        # or the process stops here. Unreferenced versions are not auto-promoted.
        ref = store.put(account_id, secret_blob, versioned=True)
        with session_scope(self.engine) as session:
            row = session.get(AccountRow, account_id)
            if row is None or row.login_state == "removed":
                raise KeyError(account_id)
            row.session_ref = ref
            row.login_state = "valid"
        return ref

    def set_login_state(self, account_id: str, state: str) -> Account:
        if state not in {"valid", "expired", "pending_verification"}:
            raise ValueError("invalid login state")
        with session_scope(self.engine) as session:
            row = session.get(AccountRow, account_id)
            if row is None or row.login_state == "removed":
                raise KeyError(account_id)
            row.login_state = state
            session.flush()
            return self._record(row)

    def update_capabilities(self, account_id: str, snapshot: CapabilitySnapshot) -> Account:
        with session_scope(self.engine) as session:
            row = session.get(AccountRow, account_id)
            if row is None or row.login_state == "removed":
                raise KeyError(account_id)
            row.capability_json = json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True)
            session.flush()
            return self._record(row)

    @staticmethod
    def _record(row: AccountRow) -> Account:
        raw = json.loads(row.capability_json)
        return Account(
            id=row.id,
            platform=row.platform,
            platform_user_id=row.platform_user_id,
            nickname=row.nickname,
            remark=row.remark,
            group_name=row.group_name,
            is_default=row.is_default,
            login_state=row.login_state,
            session_ref=row.session_ref,
            capabilities=CapabilitySnapshot(**raw),
            avatar_data=row.avatar_data,
            last_login_at=row.last_login_at.replace(tzinfo=timezone.utc) if row.last_login_at and row.last_login_at.tzinfo is None else row.last_login_at,
        )


class AccountCapabilityChecker:
    def check(self, adapter, *, allow_upload_probe: bool = False) -> CapabilitySnapshot:
        return adapter.check_capabilities(allow_upload_probe=allow_upload_probe)
