from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.features.accounts.models import Account, CapabilitySnapshot
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
