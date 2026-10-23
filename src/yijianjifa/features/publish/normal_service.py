from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.core.snapshots import FrozenSnapshot
from yijianjifa.features.finished.service import FinishedVideo
from yijianjifa.features.mixing.models import MixAutoPublishIntent
from yijianjifa.features.publish.history import PublishHistoryRepository
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PublishTaskRow(Base):
    __tablename__ = "publish_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="confirmed", index=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


@dataclass(frozen=True, slots=True)
class ConfirmedPublishTask:
    id: str
    platform: str
    state: str
    snapshot_json: str

    @property
    def snapshot(self) -> dict[str, Any]:
        return json.loads(self.snapshot_json)


@dataclass(frozen=True, slots=True)
class MaterializedAutoPublishItem:
    source_index: int
    video_id: str
    planned_time: Any
    platform: str
    account_id: str
    content_type: str
    publish_mode: str
    chain_mode: str


class AdvancedVideoSelection:
    def __init__(self, *, new_ids: set[str], published_ids: set[str]):
        self.new_ids = set(new_ids)
        self.published_ids = set(published_ids)
        self.selected_ids: set[str] = set()
        self._show_published = False

    @property
    def show_published(self) -> bool:
        return self._show_published

    @show_published.setter
    def show_published(self, value: bool) -> None:
        self._show_published = bool(value)
        if not self._show_published:
            self.selected_ids.difference_update(self.published_ids)

    def select_ids(self, ids: set[str]) -> None:
        allowed = self.new_ids | (self.published_ids if self._show_published else set())
        self.selected_ids.update(set(ids) & allowed)


class NormalPublishService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.history = PublishHistoryRepository(engine)

    def quick_candidate_ids(self) -> set[str]:
        with session_scope(self.engine) as session:
            rows = session.scalars(select(FinishedVideo)).all()
            candidates = [(row.id, row.content_hash) for row in rows]
        return {finished_id for finished_id, content_hash in candidates if not self.history.has_quick_block(content_hash)}

    def confirm_publish_draft(self, draft: Mapping[str, Any]) -> ConfirmedPublishTask:
        snapshot = FrozenSnapshot.from_mapping(draft)
        platform = str(snapshot.data.get("platform", "douyin"))
        with session_scope(self.engine) as session:
            row = PublishTaskRow(platform=platform, state="confirmed", snapshot_json=snapshot.json)
            session.add(row)
            session.flush()
            return ConfirmedPublishTask(row.id, row.platform, row.state, row.snapshot_json)

    def get_task(self, task_id: str) -> ConfirmedPublishTask:
        with session_scope(self.engine) as session:
            row = session.get(PublishTaskRow, task_id)
            if row is None:
                raise KeyError(task_id)
            return ConfirmedPublishTask(row.id, row.platform, row.state, row.snapshot_json)

    def materialize_mix_auto_publish(
        self,
        intent: MixAutoPublishIntent,
        *,
        successful_outputs: Mapping[int, str],
        planned_times: list[Any] | tuple[Any, ...],
    ) -> list[MaterializedAutoPublishItem]:
        if not intent.enabled or not intent.account_id:
            raise ValueError("mix auto-publish intent must be enabled with one account")
        result: list[MaterializedAutoPublishItem] = []
        for source_index, video_id in sorted(successful_outputs.items()):
            if source_index < 0 or source_index >= len(planned_times):
                raise IndexError("successful output index has no pre-confirmed publish slot")
            result.append(
                MaterializedAutoPublishItem(
                    source_index=source_index,
                    video_id=video_id,
                    planned_time=planned_times[source_index],
                    platform=intent.platform,
                    account_id=intent.account_id,
                    content_type=intent.content_type,
                    publish_mode=intent.publish_mode,
                    chain_mode=intent.chain_mode,
                )
            )
        return result
