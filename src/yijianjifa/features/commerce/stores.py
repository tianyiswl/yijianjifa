from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from yijianjifa.core.selection import FilteredSelection

CommissionState = Literal["confirmed", "unknown", "none"]
StoreScope = Literal["local", "national"]


@dataclass(frozen=True, slots=True)
class StoreCandidate:
    id: str
    name: str
    region: str
    scope: StoreScope
    commission: CommissionState
    address: str = ""
    favorite: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ValueError("store id and name are required")
        if self.commission not in {"confirmed", "unknown", "none"}:
            raise ValueError("invalid commission state")
        if self.scope not in {"local", "national"}:
            raise ValueError("invalid store scope")


@dataclass(frozen=True, slots=True)
class StoreFilter:
    query: str = ""
    region: str | None = None
    scope: StoreScope | None = None
    commission: Literal["all", "confirmed_only"] = "all"
    favorite_only: bool = False


def filter_stores(rows: list[StoreCandidate] | tuple[StoreCandidate, ...], spec: StoreFilter) -> list[StoreCandidate]:
    query = spec.query.strip().casefold()
    result: list[StoreCandidate] = []
    for row in rows:
        searchable = " ".join((row.id, row.name, row.address, row.region)).casefold()
        if query and query not in searchable:
            continue
        if spec.region is not None and row.region != spec.region:
            continue
        if spec.scope is not None and row.scope != spec.scope:
            continue
        if spec.commission == "confirmed_only" and row.commission != "confirmed":
            continue
        if spec.favorite_only and not row.favorite:
            continue
        result.append(row)
    return result


class StoreViewModel:
    """Headless selection model shared by the eventual Qt store picker.

    Selection is based on stable store IDs, so Select All can cover the full
    filtered result rather than just the currently rendered page.
    """

    def __init__(self, rows: list[StoreCandidate] | tuple[StoreCandidate, ...]):
        self._rows = tuple(rows)
        self._filter = StoreFilter()
        self._filtered = filter_stores(self._rows, self._filter)
        self._selection = FilteredSelection()

    def apply_filter(self, spec: StoreFilter) -> None:
        self._filter = spec
        self._filtered = filter_stores(self._rows, spec)

    @property
    def filtered_ids(self) -> tuple[str, ...]:
        return tuple(row.id for row in self._filtered)

    @property
    def selected_ids(self) -> set[str]:
        return self._selection.selected_ids

    @property
    def selected_count(self) -> int:
        return self._selection.count

    def select(self, store_id: str) -> None:
        self._selection.select(store_id)

    def clear(self, store_id: str) -> None:
        self._selection.clear(store_id)

    def select_all(self) -> None:
        self._selection.select_all(self.filtered_ids)

    def clear_all(self) -> None:
        self._selection.clear_all(self.filtered_ids)
