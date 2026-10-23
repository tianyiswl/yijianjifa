from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import Engine, select

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting

_PREFIX = "software_schedule:"


@dataclass(frozen=True, slots=True)
class SoftwareScheduleItem:
    id: str
    when: datetime
    payload: dict[str, Any]
    state: str
    auto_submitted: bool


class SoftwareScheduler:
    def __init__(self, engine: Engine):
        self.engine = engine

    def schedule(self, *, when: datetime, payload: Mapping[str, Any]) -> SoftwareScheduleItem:
        self._require_aware(when)
        item = SoftwareScheduleItem(
            id=str(uuid.uuid4()),
            when=when,
            payload=dict(payload),
            state="waiting",
            auto_submitted=False,
        )
        self._save(item)
        return item

    def get(self, item_id: str) -> SoftwareScheduleItem:
        with session_scope(self.engine) as session:
            row = session.get(Setting, self._key(item_id))
            if row is None:
                raise KeyError(item_id)
            return self._decode(item_id, row.value_json)

    def on_startup(self, *, now: datetime) -> list[str]:
        self._require_aware(now)
        missed: list[str] = []
        with session_scope(self.engine) as session:
            rows = list(session.scalars(select(Setting).where(Setting.key.like(f"{_PREFIX}%"))))
            for row in rows:
                item_id = row.key[len(_PREFIX):]
                item = self._decode(item_id, row.value_json)
                if item.state == "waiting" and item.when < now:
                    updated = SoftwareScheduleItem(
                        id=item.id,
                        when=item.when,
                        payload=item.payload,
                        state="missed_waiting_user",
                        auto_submitted=False,
                    )
                    row.value_json = self._encode(updated)
                    missed.append(item.id)
        return missed

    def auto_submitted(self, item_id: str) -> bool:
        return self.get(item_id).auto_submitted

    def _save(self, item: SoftwareScheduleItem) -> None:
        with session_scope(self.engine) as session:
            session.add(Setting(key=self._key(item.id), value_json=self._encode(item)))

    @staticmethod
    def _key(item_id: str) -> str:
        return f"{_PREFIX}{item_id}"

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("software schedule times must be timezone-aware")

    @staticmethod
    def _encode(item: SoftwareScheduleItem) -> str:
        return json.dumps(
            {
                "when": item.when.isoformat(),
                "payload": item.payload,
                "state": item.state,
                "auto_submitted": item.auto_submitted,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _decode(item_id: str, value: str) -> SoftwareScheduleItem:
        raw = json.loads(value)
        return SoftwareScheduleItem(
            id=item_id,
            when=datetime.fromisoformat(raw["when"]),
            payload=dict(raw.get("payload", {})),
            state=raw["state"],
            auto_submitted=bool(raw.get("auto_submitted", False)),
        )
