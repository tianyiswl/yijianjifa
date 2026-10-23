from __future__ import annotations

import json

from sqlalchemy import Engine

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class LibraryPreferences:
    """Persistent import preferences for video/music library behavior."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def keep_copy(self, kind: str) -> bool:
        self._validate_kind(kind)
        key = self._key(kind)
        with session_scope(self.engine) as session:
            row = session.get(Setting, key)
            if row is None:
                return False
            return bool(json.loads(row.value_json))

    def set_keep_copy(self, kind: str, enabled: bool) -> None:
        self._validate_kind(kind)
        key = self._key(kind)
        value = json.dumps(bool(enabled))
        with session_scope(self.engine) as session:
            row = session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value_json=value))
            else:
                row.value_json = value

    @staticmethod
    def _key(kind: str) -> str:
        return f"library:{kind}:keep_copy"

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in {"video", "music"}:
            raise ValueError("kind must be 'video' or 'music'")
