from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PresetValidationError(ValueError):
    pass


class DailyRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal['daily', 'interval', 'exact', 'immediate'] = 'daily'
    daily_times: tuple[str, ...] | None = None
    first_delay_hours: float = Field(default=3, gt=0)
    per_day: int = Field(default=3, ge=1)
    days: int = Field(default=5, ge=1)
    interval_hours: float = Field(default=3, gt=0)


    @model_validator(mode="after")
    def _validate_clocks(self) -> "DailyRule":
        import re
        if self.daily_times is not None:
            if (self.mode != 'daily' or len(self.daily_times) != self.per_day
                    or len(set(self.daily_times)) != len(self.daily_times)
                    or any(not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value) for value in self.daily_times)):
                raise ValueError('invalid daily clock rules')
        return self


class AudioSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    keep_original_sound: bool = True
    tts_voice: str = "system_default"
    tts_speed: float = Field(default=1.0, gt=0)
    original_volume: float = Field(default=0.0, ge=0)
    voice_volume: float = Field(default=1.0, ge=0)
    music_volume: float = Field(default=0.35, ge=0)


class SubtitleSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spoken_enabled: bool = True
    full_time_enabled: bool = False
    font: str = "system_default"
    font_size: int = Field(default=75, ge=8)
    position: Literal["top", "center", "bottom"] = "bottom"
    max_chars_per_line: int = Field(default=18, ge=1)


class MixPreset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["mix"] = "mix"
    name: str
    material_scope: Literal["all", "categories"] = "all"
    material_category_ids: tuple[str, ...] = ()
    use_copy: bool = True
    copy_scope: Literal["all", "categories"] = "all"
    copy_category_ids: tuple[str, ...] = ()
    quantity: int = Field(default=10, ge=1)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal["720p", "1080p"] = "720p"
    frame_fit: Literal["preserve_blur", "crop_fill"] = "preserve_blur"
    background_music_mode: Literal["none", "fixed", "random"] = "none"
    transition_mode: Literal["none", "fixed", "random"] = "none"
    audio: AudioSettings = Field(default_factory=AudioSettings)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)

    # Additive fields preserve legacy preset JSON and retain complete local mix settings.
    total_ms: int = Field(default=15000, gt=0)
    segment_ms: int = Field(default=3000, gt=0)
    total_range_ms: list[int] | None = None
    segment_range_ms: list[int] | None = None
    copy_mode: Literal["sequential", "random"] = "sequential"
    sentence_cut: bool = False
    spoken_style: dict | None = None
    full_style: dict | None = None
    background_music_id: str | None = None
    music_category_ids: tuple[str, ...] = ()
    transition_name: Literal["fade", "slideleft", "slideright"] | None = None
    transition_duration_ms: int = Field(default=500, ge=0, le=60000)

    @model_validator(mode="after")
    def _validate_local_settings(self) -> "MixPreset":
        from yijianjifa.features.mixing.duration_ranges import validate_range
        from yijianjifa.features.mixing.subtitle_styles import validate_style
        validate_range(self.total_range_ms)
        validate_range(self.segment_range_ms)
        for style in (self.spoken_style, self.full_style):
            if style is not None: validate_style(style)
        return self

    @model_validator(mode="after")
    def _validate_scopes(self) -> "MixPreset":
        if self.material_scope == "categories" and not self.material_category_ids:
            raise ValueError("category material scope requires category ids")
        if self.copy_scope == "categories" and not self.copy_category_ids:
            raise ValueError("category copy scope requires category ids")
        return self


class NormalPublishPreset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["normal_publish"] = "normal_publish"
    name: str
    platform: Literal["douyin"] = "douyin"
    keep_start_time: bool = False
    use_existing_cover: bool = False
    account_mode: Literal["platform_default", "specific", "multiple"] = "platform_default"
    account_ids: tuple[str, ...] = ()
    schedule_rule: DailyRule = Field(default_factory=DailyRule)
    distribution_mode: Literal["divide", "whole_batch_per_account"] = "divide"
    cover_mode: Literal["first_frame", "video_frame", "uploaded_image"] = "first_frame"
    location_rule: Literal["none", "confirm_when_used"] = "none"
    declaration_rule: Literal["confirm_when_used"] = "confirm_when_used"


class CommercePreset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["commerce"] = "commerce"
    name: str
    platform: Literal["douyin"] = "douyin"
    account_id: str | None = None
    schedule_rule: DailyRule = Field(default_factory=DailyRule)
    store_rule: Literal["confirm_when_used", "favorites_then_confirm"] = "confirm_when_used"
    commission_filter: Literal["all", "confirmed_only"] = "all"
    favorite_music_mode: Literal["none", "fixed", "random"] = "none"
    cover_mode: Literal["first_frame", "video_frame", "uploaded_image"] = "first_frame"
    declaration_rule: Literal["confirm_when_used"] = "confirm_when_used"

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_multi_account(cls, value):
        if isinstance(value, dict) and "account_ids" in value:
            raise PresetValidationError("legacy multi-account commerce presets are not supported")
        return value


class MixThenPublishPreset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["mix_then_publish"] = "mix_then_publish"
    name: str
    mix_preset_id: str
    linkage: Literal["per_item", "whole_batch"] = "per_item"
    platform: Literal["douyin"] = "douyin"
    account_id: str | None = None
    content_type: Literal["normal_video"] = "normal_video"
    publish_mode: Literal["scheduled_only"] = "scheduled_only"
    first_delay_hours: float = Field(default=3, gt=0)
    interval_hours: float = Field(default=3, gt=0)


PresetModel = MixPreset | NormalPublishPreset | CommercePreset | MixThenPublishPreset
