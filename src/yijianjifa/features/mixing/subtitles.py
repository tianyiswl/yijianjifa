from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubtitleSpec:
    spoken_enabled: bool
    full_time_text: str | None
    warning_code: str | None = None


def build_subtitle_spec(
    *,
    spoken_enabled: bool,
    full_enabled: bool,
    batch_full_text: str,
    copy_full_text: str,
    title: str,
) -> SubtitleSpec:
    del title  # The title is deliberately never used as a full-time subtitle fallback.
    if not full_enabled:
        return SubtitleSpec(spoken_enabled=spoken_enabled, full_time_text=None)

    batch = str(batch_full_text or "").strip()
    own = str(copy_full_text or "").strip()
    if batch:
        return SubtitleSpec(spoken_enabled=spoken_enabled, full_time_text=batch)
    if own:
        return SubtitleSpec(spoken_enabled=spoken_enabled, full_time_text=own)
    return SubtitleSpec(
        spoken_enabled=spoken_enabled,
        full_time_text=None,
        warning_code="full_subtitle_empty",
    )
