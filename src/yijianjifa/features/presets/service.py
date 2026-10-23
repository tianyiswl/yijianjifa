from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.features.library.copy_service import CategoryRepository
from yijianjifa.features.presets.models import (
    CommercePreset,
    MixPreset,
    MixThenPublishPreset,
    NormalPublishPreset,
    PresetModel,
)
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PresetRow(Base):
    __tablename__ = "presets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


@dataclass(frozen=True, slots=True)
class StoredPreset:
    id: str
    preset: PresetModel

    @property
    def name(self) -> str:
        return self.preset.name

    @property
    def kind(self) -> str:
        return self.preset.kind


@dataclass(frozen=True, slots=True)
class MaterializedPreset:
    id: str
    kind: str
    state: str
    payload: dict[str, Any]
    missing_category_ids: tuple[str, ...] = ()

    def __getattr__(self, name: str):
        try:
            return self.payload[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


_ADAPTER = TypeAdapter(PresetModel)


class PresetService:
    def __init__(self, engine: Engine, *, categories: CategoryRepository | None = None):
        self.engine = engine
        self.categories = categories or CategoryRepository(engine)

    def create_mix(self, *, name: str, material_category_ids: list[str] | tuple[str, ...] = (), copy_category_ids: list[str] | tuple[str, ...] = (), **kwargs) -> StoredPreset:
        material_ids = tuple(material_category_ids)
        copy_ids = tuple(copy_category_ids)
        model = MixPreset(
            name=name,
            material_scope="categories" if material_ids else "all",
            material_category_ids=material_ids,
            copy_scope="categories" if copy_ids else "all",
            copy_category_ids=copy_ids,
            **kwargs,
        )
        return self.create(model)

    def create(self, preset: PresetModel) -> StoredPreset:
        payload = preset.model_dump_json()
        with session_scope(self.engine) as session:
            row = PresetRow(kind=preset.kind, name=preset.name, payload_json=payload)
            session.add(row)
            session.flush()
            return StoredPreset(row.id, preset)

    def get(self, preset_id: str) -> StoredPreset:
        with session_scope(self.engine) as session:
            row = session.get(PresetRow, preset_id)
            if row is None:
                raise KeyError(preset_id)
            return StoredPreset(row.id, _ADAPTER.validate_json(row.payload_json))

    def list(self, *, kind: str | None = None) -> list[StoredPreset]:
        with session_scope(self.engine) as session:
            stmt = select(PresetRow)
            if kind is not None:
                stmt = stmt.where(PresetRow.kind == kind)
            rows = session.scalars(stmt.order_by(PresetRow.name, PresetRow.id)).all()
            return [StoredPreset(row.id, _ADAPTER.validate_json(row.payload_json)) for row in rows]

    def rename(self, preset_id: str, name: str) -> StoredPreset:
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("preset name cannot be blank")
        stored = self.get(preset_id)
        updated = stored.preset.model_copy(update={"name": normalized})
        return self.update(preset_id, updated)

    def update(self, preset_id: str, preset: PresetModel) -> StoredPreset:
        with session_scope(self.engine) as session:
            row = session.get(PresetRow, preset_id)
            if row is None:
                raise KeyError(preset_id)
            row.kind = preset.kind
            row.name = preset.name
            row.payload_json = preset.model_dump_json()
            row.updated_at = _utcnow()
            session.flush()
            return StoredPreset(row.id, preset)

    def materialize(self, preset_id: str) -> MaterializedPreset:
        stored = self.get(preset_id)
        payload = stored.preset.model_dump()
        missing: list[str] = []
        if isinstance(stored.preset, MixPreset):
            video_ids = {c.id for c in self.categories.list("video")}
            copy_ids = {c.id for c in self.categories.list("copy")}
            missing.extend(i for i in stored.preset.material_category_ids if i not in video_ids)
            missing.extend(i for i in stored.preset.copy_category_ids if i not in copy_ids)
        return MaterializedPreset(
            id=stored.id,
            kind=stored.kind,
            state="needs_repair" if missing else "ready",
            payload=payload,
            missing_category_ids=tuple(dict.fromkeys(missing)),
        )

    def delete(self, preset_id: str) -> None:
        with session_scope(self.engine) as session:
            row = session.get(PresetRow, preset_id)
            if row is not None:
                session.delete(row)
