from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(slots=True)
class FilteredSelection:
    """Stable-ID selection independent of pagination and row positions."""

    _ids: set[str] = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self._ids)

    @property
    def selected_ids(self) -> set[str]:
        return set(self._ids)

    def is_selected(self, stable_id: str) -> bool:
        return stable_id in self._ids

    def select(self, stable_id: str) -> None:
        self._ids.add(stable_id)

    def clear(self, stable_id: str) -> None:
        self._ids.discard(stable_id)

    def select_all(self, filtered_ids: Iterable[str]) -> None:
        self._ids.update(filtered_ids)

    def clear_all(self, filtered_ids: Iterable[str]) -> None:
        self._ids.difference_update(filtered_ids)

    def context_targets(self, clicked_id: str) -> set[str]:
        if clicked_id in self._ids:
            return set(self._ids)
        return {clicked_id}

    def clear_everything(self) -> None:
        self._ids.clear()
