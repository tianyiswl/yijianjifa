from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import uuid

from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.features.finished.exporter import ExportManifestEntry, export_finished_records
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.library.usage_progress import UsageProgressService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base, CopyRecord, Category, CategoryLink


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FinishedVideo(Base):
    __tablename__ = "finished_videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    origin_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    original_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stored_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    copy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


@dataclass(frozen=True, slots=True)
class FinishedVideoRecord:
    id: str
    origin_type: str
    display_name: str
    original_path: Path | None
    stored_path: Path | None
    content_hash: str
    duration_ms: int
    title: str
    body: str
    tags: str
    copy_id: str | None
    task_locked: bool

    @property
    def effective_path(self) -> Path:
        path = self.stored_path or self.original_path
        if path is None:
            raise RuntimeError("finished video has no file path")
        return path


class FinishedVideoService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def register_generated(
        self,
        path: str | Path,
        *,
        duration_ms: int,
        title: str = "",
        body: str = "",
        tags: str = "",
        display_name: str | None = None,
        copy_id: str | None = None,
    ) -> FinishedVideoRecord:
        source = Path(path).expanduser().resolve()
        self._validate_video_file(source, duration_ms)
        row = FinishedVideo(
            origin_type="generated",
            display_name=display_name or source.name,
            original_path=None,
            stored_path=str(source),
            content_hash=sha256_file(source),
            duration_ms=duration_ms,
            title=str(title or ""),
            body=str(body or ""),
            tags=str(tags or ""),
            copy_id=copy_id,
        )
        with session_scope(self.engine) as session:
            session.add(row)
            session.flush()
            return self._record(row)

    def import_external(
        self,
        path: str | Path,
        *,
        duration_ms: int,
        title: str = "",
        body: str = "",
        tags: str = "",
        display_name: str | None = None,
    ) -> FinishedVideoRecord:
        source = Path(path).expanduser().resolve()
        self._validate_video_file(source, duration_ms)
        row = FinishedVideo(
            origin_type="external",
            display_name=display_name or source.name,
            original_path=str(source),
            stored_path=None,
            content_hash=sha256_file(source),
            duration_ms=duration_ms,
            title=str(title or ""),
            body=str(body or ""),
            tags=str(tags or ""),
        )
        with session_scope(self.engine) as session:
            session.add(row)
            session.flush()
            return self._record(row)

    def get(self, finished_id: str) -> FinishedVideoRecord | None:
        with session_scope(self.engine) as session:
            row = session.get(FinishedVideo, finished_id)
            return None if row is None else self._record(row)

    def list_by_ids(self, finished_ids: Iterable[str]) -> list[FinishedVideoRecord]:
        ids = list(dict.fromkeys(finished_ids))
        if not ids:
            return []
        with session_scope(self.engine) as session:
            rows = session.scalars(select(FinishedVideo).where(FinishedVideo.id.in_(ids))).all()
            by_id = {row.id: self._record(row) for row in rows}
        return [by_id[item] for item in ids if item in by_id]

    def category_ids(self, finished_id):
        with session_scope(self.engine) as session:
            return set(session.scalars(select(CategoryLink.category_id).where(CategoryLink.target_type=='finished',CategoryLink.target_id==finished_id)))

    def set_categories(self, finished_id, category_ids):
        ids=set(category_ids)
        with session_scope(self.engine) as session:
            if session.get(FinishedVideo,finished_id) is None:raise KeyError(finished_id)
            valid=set(session.scalars(select(Category.id).where(Category.kind=='finished')))
            if not ids<=valid:raise ValueError('invalid finished category')
            session.query(CategoryLink).filter(CategoryLink.target_type=='finished',CategoryLink.target_id==finished_id).delete(synchronize_session=False)
            for cid in ids:session.add(CategoryLink(category_id=cid,target_type='finished',target_id=finished_id))

    def rename(self, finished_id: str, display_name: str) -> FinishedVideoRecord:
        from yijianjifa.features.library.display_names import name_only
        name = name_only(display_name)
        if not name:
            raise ValueError("display name cannot be blank")
        with session_scope(self.engine) as session:
            row = session.get(FinishedVideo, finished_id)
            if row is None:
                raise KeyError(finished_id)
            row.display_name = name
            session.flush()
            return self._record(row)

    def update_metadata(self, finished_id: str, *, title: str, body: str, tags: str) -> FinishedVideoRecord:
        with session_scope(self.engine) as session:
            row = session.get(FinishedVideo, finished_id)
            if row is None:
                raise KeyError(finished_id)
            row.title = str(title or "")
            row.body = str(body or "")
            row.tags = str(tags or "")
            session.flush()
            return self._record(row)

    def assign_copy(
        self,
        finished_id: str,
        copy_id: str,
        *,
        replace_existing: bool,
        progress: UsageProgressService,
    ) -> FinishedVideoRecord:
        with session_scope(self.engine) as session:
            row = session.get(FinishedVideo, finished_id)
            if row is None:
                raise KeyError(finished_id)
            copy = session.get(CopyRecord, copy_id)
            if copy is None:
                raise KeyError(copy_id)
            if replace_existing or not row.title.strip():
                row.title = copy.title
            if replace_existing or not row.body.strip():
                row.body = copy.body
            if replace_existing or not row.tags.strip():
                row.tags = copy.tags
            row.copy_id = copy.id
            session.flush()
            record = self._record(row)
        # Usage progress advances only after the association was persisted successfully.
        progress.mark_used("finished_metadata", copy_id)
        return record

    def set_task_lock(self, finished_id: str, locked: bool) -> None:
        with session_scope(self.engine) as session:
            row = session.get(FinishedVideo, finished_id)
            if row is None:
                raise KeyError(finished_id)
            row.task_locked = bool(locked)

    def delete(self, finished_id: str) -> None:
        owned_file: Path | None = None
        with session_scope(self.engine) as session:
            row = session.get(FinishedVideo, finished_id)
            if row is None:
                return
            if row.task_locked:
                raise RuntimeError("finished video is referenced by an active task")
            from yijianjifa.features.library.active_references import ensure_not_referenced
            ensure_not_referenced(session,[row.id,row.content_hash,row.original_path,row.stored_path])
            if row.origin_type == "generated" and row.stored_path:
                owned_file = Path(row.stored_path)
            session.query(CategoryLink).filter(CategoryLink.target_type=="finished",CategoryLink.target_id==finished_id).delete(synchronize_session=False)
            session.delete(row)
        if owned_file is not None and owned_file.exists():
            owned_file.unlink()

    def export(
        self,
        finished_ids: Iterable[str],
        *,
        destination: str | Path,
        include_metadata: bool,
    ) -> list[ExportManifestEntry]:
        return export_finished_records(
            self.list_by_ids(finished_ids),
            destination=destination,
            include_metadata=include_metadata,
        )

    @staticmethod
    def _validate_video_file(path: Path, duration_ms: int) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")

    @staticmethod
    def _record(row: FinishedVideo) -> FinishedVideoRecord:
        return FinishedVideoRecord(
            id=row.id,
            origin_type=row.origin_type,
            display_name=row.display_name,
            original_path=Path(row.original_path) if row.original_path else None,
            stored_path=Path(row.stored_path) if row.stored_path else None,
            content_hash=row.content_hash,
            duration_ms=row.duration_ms,
            title=row.title,
            body=row.body,
            tags=row.tags,
            copy_id=row.copy_id,
            task_locked=row.task_locked,
        )
