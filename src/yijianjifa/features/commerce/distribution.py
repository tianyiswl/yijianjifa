from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class StoreAssignmentValidation:
    blocking_video_ids: set[str]

    @property
    def ok(self) -> bool:
        return not self.blocking_video_ids


def allocate_stores(
    video_ids: Sequence[str],
    pool: Sequence[str],
    manual_overrides: Mapping[str, str],
) -> dict[str, str | None]:
    """Assign stores while preserving explicit per-video overrides.

    Automatic videos are placed on the currently least-used pool store so the
    total plan stays as even as possible after respecting manual choices.
    """

    pool_ids = list(dict.fromkeys(str(store_id) for store_id in pool if str(store_id)))
    manual = {str(video_id): str(store_id) for video_id, store_id in manual_overrides.items()}
    counts = {store_id: 0 for store_id in pool_ids}
    plan: dict[str, str | None] = {}

    for video_id in video_ids:
        video_id = str(video_id)
        if video_id in manual:
            store_id = manual[video_id]
            plan[video_id] = store_id
            if store_id in counts:
                counts[store_id] += 1

    for video_id in video_ids:
        video_id = str(video_id)
        if video_id in plan:
            continue
        if not pool_ids:
            plan[video_id] = None
            continue
        store_id = min(pool_ids, key=lambda candidate: (counts[candidate], pool_ids.index(candidate)))
        plan[video_id] = store_id
        counts[store_id] += 1

    return plan


def validate_store_assignments(
    assignments: Mapping[str, str | None],
    *,
    active_pool: set[str],
    manual_overrides: Mapping[str, str],
) -> StoreAssignmentValidation:
    """Block missing or stale assignments instead of silently substituting."""

    active = set(active_pool)
    manual = {str(video_id): str(store_id) for video_id, store_id in manual_overrides.items()}
    blocking: set[str] = set()
    for video_id, store_id in assignments.items():
        if not store_id:
            blocking.add(str(video_id))
            continue
        if str(store_id) not in active:
            # This covers the important manual-store-removed case and also
            # protects against any stale automatic plan after the pool changes.
            blocking.add(str(video_id))
            continue
        if str(video_id) in manual and manual[str(video_id)] != str(store_id):
            blocking.add(str(video_id))
    return StoreAssignmentValidation(blocking)
