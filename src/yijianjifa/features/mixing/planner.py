from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable

from yijianjifa.features.mixing.fingerprint import composition_fingerprint
from yijianjifa.features.mixing.models import Composition, SegmentUse


@dataclass(frozen=True, slots=True)
class SourceClip:
    asset_id: str
    content_hash: str
    duration_ms: int
    category_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class PlanningResult:
    compositions: tuple[Composition, ...]
    missing_count: int
    relaxed_rules: bool = False


class CompositionPlanner:
    def plan(
        self,
        *,
        quantity: int,
        required_categories: set[str],
        sources: Iterable[SourceClip],
        segment_duration_ms: int,
        historical_fingerprints: Iterable[str] = (),
    ) -> PlanningResult:
        if quantity < 1:
            raise ValueError("quantity must be >= 1")
        if segment_duration_ms < 1:
            raise ValueError("segment_duration_ms must be >= 1")

        source_list = sorted(list(sources), key=lambda x: x.asset_id)
        if not source_list:
            return PlanningResult((), quantity)

        categories = tuple(sorted(required_categories))
        pools = {
            category: [source for source in source_list if category in source.category_ids]
            for category in categories
        }
        if any(not pool for pool in pools.values()):
            return PlanningResult((), quantity)

        blocked = set(historical_fingerprints)
        seen: set[str] = set()
        results: list[Composition] = []
        max_attempts = max(100, (quantity + len(blocked)) * 100)

        for attempt in range(max_attempts):
            selected: list[SourceClip]
            if categories:
                selected = [
                    pools[category][(attempt + index) % len(pools[category])]
                    for index, category in enumerate(categories)
                ]
                if selected:
                    rotation = attempt % len(selected)
                    selected = selected[rotation:] + selected[:rotation]
            else:
                selected = [source_list[attempt % len(source_list)]]

            segments: list[SegmentUse] = []
            for index, source in enumerate(selected):
                duration = min(segment_duration_ms, source.duration_ms)
                if duration <= 0:
                    segments = []
                    break
                max_start = source.duration_ms - duration
                start = 0 if max_start <= 0 else (attempt * 997 + index * 211) % (max_start + 1)
                segments.append(
                    SegmentUse(
                        asset_id=source.asset_id,
                        asset_content_hash=source.content_hash,
                        source_start_ms=start,
                        duration_ms=duration,
                        category_ids=source.category_ids,
                    )
                )
            if not segments:
                continue

            composition = Composition(tuple(segments))
            if required_categories and not required_categories <= composition.covered_categories:
                continue
            fingerprint = composition_fingerprint(composition)
            if fingerprint in blocked or fingerprint in seen:
                continue
            seen.add(fingerprint)
            results.append(composition)
            if len(results) >= quantity:
                break

        return PlanningResult(tuple(results), quantity - len(results), relaxed_rules=False)

    def plan_for_sentences(
        self,
        *,
        quantity: int,
        sentence_durations_ms: Iterable[int],
        required_categories: set[str],
        sources: Iterable[SourceClip],
        historical_fingerprints: Iterable[str] = (),
        variation_seed: int | None = None,
        variant_index: int = 0,
    ) -> PlanningResult:
        """Plan visuals against actual spoken-sentence durations.

        Every sentence starts a fresh visual segment. If a source clip is
        shorter than the sentence, more segments are chained until the exact
        sentence duration is covered. Required categories are scheduled before
        free-choice fills, so multiple categories can occur inside one sentence.
        """
        if quantity < 1:
            raise ValueError("quantity must be >= 1")
        durations = [int(value) for value in sentence_durations_ms]
        if not durations or any(value <= 0 for value in durations):
            raise ValueError("sentence durations must all be positive")

        source_list = sorted(
            [source for source in sources if source.duration_ms > 0],
            key=lambda source: source.asset_id,
        )
        if not source_list:
            return PlanningResult((), quantity)

        categories = tuple(sorted(required_categories))
        pools = {
            category: [source for source in source_list if category in source.category_ids]
            for category in categories
        }
        if any(not pool for pool in pools.values()):
            return PlanningResult((), quantity)
        if sum(durations) < len(categories):
            return PlanningResult((), quantity)

        # A batch shares one shuffled opening pool; each item takes a different
        # opening before wrapping. The rest and source offsets vary per item.
        # No seed preserves the behavior of already frozen legacy tasks.
        if variation_seed is not None:
            random.Random(variation_seed).shuffle(source_list)
        blocked = set(historical_fingerprints)
        seen: set[str] = set()
        compositions: list[Composition] = []
        max_attempts = max(100, (quantity + len(blocked)) * 100)

        for attempt in range(max_attempts):
            rng = None
            ordered_sources = source_list
            used_hashes: set[str] = set()
            if variation_seed is not None:
                rng = random.Random(f"{variation_seed}:{variant_index}:{attempt}")
                anchor = source_list[(variant_index + attempt) % len(source_list)]
                rest = [source for source in source_list if source != anchor]
                rng.shuffle(rest)
                ordered_sources = [anchor, *rest]
            if categories:
                rotation = attempt % len(categories)
                required_order = list(categories[rotation:] + categories[:rotation])
            else:
                required_order = []
            if rng is not None and categories:
                rng.shuffle(required_order)
                # Start with a category covered by the reserved opening source.
                required_order.sort(key=lambda c: c not in ordered_sources[0].category_ids)
            uncovered = set(categories)
            required_cursor = 0
            source_cursor = attempt
            segment_number = 0
            timeline_remaining = sum(durations)
            segments: list[SegmentUse] = []

            for sentence_index, sentence_duration in enumerate(durations):
                remaining = sentence_duration
                while remaining > 0:
                    source: SourceClip
                    pending_categories = [category for category in required_order if category in uncovered]
                    if pending_categories:
                        category = pending_categories[required_cursor % len(pending_categories)]
                        pool = pools[category]
                        if rng is not None:
                            pool = [s for s in ordered_sources if category in s.category_ids]
                            available = [s for s in pool if s.content_hash not in used_hashes]
                            if not available:
                                available = [s for s in pool if not segments or s.content_hash != segments[-1].asset_content_hash]
                            source = (available or pool)[0]
                        else:
                            source = pool[(attempt + segment_number) % len(pool)]
                        remaining_required = max(1, len(uncovered))
                        fair_share = max(1, timeline_remaining // remaining_required)
                        duration = min(source.duration_ms, remaining, fair_share)
                        uncovered.difference_update(source.category_ids)
                        required_cursor += 1
                    else:
                        if rng is not None:
                            available = [s for s in ordered_sources if s.content_hash not in used_hashes]
                            if not available:
                                used_hashes.clear()
                                available = [s for s in ordered_sources if not segments or s.content_hash != segments[-1].asset_content_hash]
                            source = (available or ordered_sources)[0]
                        else:
                            source = source_list[source_cursor % len(source_list)]
                        source_cursor += 1
                        duration = min(source.duration_ms, remaining)

                    if duration <= 0:
                        break
                    max_start = source.duration_ms - duration
                    start = 0 if max_start <= 0 else (attempt * 997 + segment_number * 211) % (max_start + 1)
                    if rng is not None:
                        start = rng.randint(0, max_start)
                    used_hashes.add(source.content_hash)
                    segments.append(
                        SegmentUse(
                            asset_id=source.asset_id,
                            asset_content_hash=source.content_hash,
                            source_start_ms=start,
                            duration_ms=duration,
                            category_ids=source.category_ids,
                            sentence_index=sentence_index,
                        )
                    )
                    remaining -= duration
                    timeline_remaining -= duration
                    segment_number += 1

            composition = Composition(tuple(segments))
            if uncovered or sum(segment.duration_ms for segment in composition.segments) != sum(durations):
                continue
            fingerprint = composition_fingerprint(composition)
            if fingerprint in blocked or fingerprint in seen:
                continue
            seen.add(fingerprint)
            compositions.append(composition)
            if len(compositions) >= quantity:
                break

        return PlanningResult(tuple(compositions), quantity - len(compositions), relaxed_rules=False)
