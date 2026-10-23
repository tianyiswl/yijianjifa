from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class FrozenSnapshot:
    """Immutable JSON snapshot used by confirmed tasks and history."""

    json: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FrozenSnapshot":
        return cls(json.dumps(deepcopy(dict(value)), ensure_ascii=False, sort_keys=True))

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self.json)
