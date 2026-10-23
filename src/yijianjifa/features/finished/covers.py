from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    code: str | None = None


@dataclass(frozen=True, slots=True)
class CoverChoice:
    kind: str
    frame_ms: int | None = None
    image_path: Path | None = None

    @classmethod
    def first_frame(cls) -> "CoverChoice":
        return cls(kind="first_frame", frame_ms=0)

    @classmethod
    def frame(cls, frame_ms: int) -> "CoverChoice":
        return cls(kind="frame", frame_ms=int(frame_ms))

    @classmethod
    def uploaded(cls, image_path: str | Path) -> "CoverChoice":
        return cls(kind="uploaded", image_path=Path(image_path))


def validate_frame_time(duration_ms: int, frame_ms: int) -> ValidationResult:
    if duration_ms <= 0:
        return ValidationResult(False, "invalid_video_duration")
    if frame_ms < 0 or frame_ms >= duration_ms:
        return ValidationResult(False, "cover_frame_out_of_range")
    return ValidationResult(True)
