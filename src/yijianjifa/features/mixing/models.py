from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TransitionSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: Literal["none", "fixed", "random"] = "none"
    name: str | None = None
    duration_ms: int = Field(default=500, ge=0)


class BackgroundMusicSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: Literal["none", "fixed", "random"] = "none"
    music_id: str | None = None
    category_ids: tuple[str, ...] = ()


class MixPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    material_scope: Literal["all", "categories"] = "all"
    material_category_ids: tuple[str, ...] = ()
    use_copy: bool = True
    copy_scope: Literal["all", "categories"] = "all"
    copy_category_ids: tuple[str, ...] = ()
    quantity: int = Field(default=10, ge=1)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal["720p", "1080p"] = "720p"
    frame_fit: Literal["preserve_blur", "crop_fill"] = "preserve_blur"
    transition: TransitionSpec = TransitionSpec()
    background_music: BackgroundMusicSpec = BackgroundMusicSpec()

    @classmethod
    def quick(cls) -> "MixPlan":
        return cls()


class MixAutoPublishIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    platform: Literal["douyin"] = "douyin"
    account_id: str | None = None
    content_type: Literal["normal_video"] = "normal_video"
    publish_mode: Literal["scheduled_only"] = "scheduled_only"
    first_delay_hours: float = Field(default=3, gt=0)
    interval_hours: float = Field(default=3, gt=0)
    chain_mode: Literal["per_item", "whole_batch"] = "per_item"

    @model_validator(mode="after")
    def _require_account_when_enabled(self) -> "MixAutoPublishIntent":
        if self.enabled and not (self.account_id or "").strip():
            raise ValueError("enabled auto-publish requires one Douyin account")
        return self


@dataclass(frozen=True, slots=True)
class SegmentUse:
    asset_id: str
    asset_content_hash: str
    source_start_ms: int
    duration_ms: int
    category_ids: frozenset[str]
    sentence_index: int | None = None


@dataclass(frozen=True, slots=True)
class Composition:
    segments: tuple[SegmentUse, ...]

    @property
    def covered_categories(self) -> frozenset[str]:
        values: set[str] = set()
        for segment in self.segments:
            values.update(segment.category_ids)
        return frozenset(values)
