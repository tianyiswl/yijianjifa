from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import DateTime, String, Text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.commerce.distribution import validate_store_assignments
from yijianjifa.features.commerce.models import CommerceDraft
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CommerceTaskRow(Base):
    __tablename__ = "commerce_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmed", index=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class CommerceService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.accounts = AccountService(engine)

    def initial_account_id(self, platform: str = "douyin") -> str | None:
        """Preselect only the platform default when commerce is confirmed usable."""

        account = self.accounts.default(platform)
        if account is None:
            return None
        if account.login_state != "valid":
            return None
        if account.capabilities.commerce != "available":
            return None
        return account.id

    def confirm_draft(self, draft: CommerceDraft) -> str:
        if draft.account_id is None:
            raise ValueError("commerce batch requires exactly one account")
        account = self.accounts.get(draft.account_id)
        if account.platform != "douyin":
            raise ValueError("V1 commerce supports Douyin accounts only")
        if account.login_state != "valid" or account.capabilities.commerce != "available":
            raise ValueError("commerce account is not currently usable")
        selected = set(draft.selected_video_ids)
        if not selected:
            raise ValueError("commerce batch requires at least one video")
        missing = selected.difference(draft.store_assignments)
        validation = validate_store_assignments(
            draft.store_assignments,
            active_pool=set(draft.store_pool),
            manual_overrides=draft.manual_store_overrides,
        )
        if missing or validation.blocking_video_ids:
            raise ValueError("every commerce video requires a valid store")
        payload = draft.model_dump(mode="json")
        # Defensive assertion: legacy multi-account payloads can never be stored.
        if isinstance(payload.get("account_id"), list):
            raise ValueError("commerce account_id must be scalar")
        with session_scope(self.engine) as session:
            row = CommerceTaskRow(
                account_id=draft.account_id,
                state="confirmed",
                snapshot_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            )
            session.add(row)
            session.flush()
            return row.id
