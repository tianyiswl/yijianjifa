from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.engine import Engine

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class GlobalSettings:
    OUTPUT_KEY = "global:output_directory"
    SOUND_KEY = "global:notification_sound_enabled"

    def __init__(self, engine: Engine, *, default_output_directory: str | Path):
        self.engine = engine
        self.default_output_directory = Path(default_output_directory)

    @property
    def output_directory(self) -> Path:
        raw = self._read(self.OUTPUT_KEY)
        if raw is None:
            return self.default_output_directory
        return Path(str(raw))

    @output_directory.setter
    def output_directory(self, value: str | Path) -> None:
        path = Path(value)
        self._write(self.OUTPUT_KEY, str(path))

    @property
    def notification_sound_enabled(self) -> bool:
        raw = self._read(self.SOUND_KEY)
        return True if raw is None else bool(raw)

    @notification_sound_enabled.setter
    def notification_sound_enabled(self, value: bool) -> None:
        self._write(self.SOUND_KEY, bool(value))

    def _read(self, key: str):
        with session_scope(self.engine) as session:
            row = session.get(Setting, key)
            return None if row is None else json.loads(row.value_json)

    def _write(self, key: str, value) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        with session_scope(self.engine) as session:
            row = session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value_json=encoded))
            else:
                row.value_json = encoded
