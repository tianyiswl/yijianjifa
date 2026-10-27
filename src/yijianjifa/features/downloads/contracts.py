from dataclasses import dataclass, field
from typing import Literal, Protocol
import math
import re

ErrorCode = Literal['input_limit', 'single_category_only', 'unsafe_source', 'redirect_limit', 'size_limit', 'timeout', 'disk_space', 'cancelled', 'network', 'source_missing', 'unsupported_source', 'metadata_missing', 'verification_required', 'account_busy', 'account_unavailable', 'session_missing', 'session_invalid', 'identity_mismatch', 'no_audio', 'media_invalid', 'media_tool_missing', 'media_failed', 'model_required', 'model_invalid', 'model_unknown', 'subtitle_conflict', 'subtitle_invalid', 'no_speech', 'ocr_failed', 'no_text', 'local_format_unsupported', 'local_source_missing', 'local_reselect_required']
AuxiliaryKind = Literal['resolve', 'cover', 'preview', 'region', 'resource_import', 'resource_preview', 'resource_reveal', 'resource_delete', 'subtitle_export', 'model_download', 'model_import']

class DownloadError(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code)

@dataclass(frozen=True)
class SourceInput:
    kind: Literal['direct', 'douyin_video', 'douyin_profile', 'local']
    source_key: str
    canonical_source: str | None = field(repr=False)

@dataclass(frozen=True)
class Candidate:
    id: str
    platform: str
    work_id: str
    source_key: str
    canonical_source: str | None
    title: str = ''
    author: str = ''
    duration_ms: int | None = None
    published_at: str | None = None
    cover_resource_id: str | None = None

@dataclass(frozen=True)
class ResolvePage:
    candidates: tuple[Candidate, ...]
    cursor: str | None
    has_more: bool
    completion: Literal['partial', 'complete', 'limited'] = 'partial'

@dataclass(frozen=True)
class MediaSource:
    handle: str = field(repr=False)
    watermark: Literal['clean', 'original', 'unknown'] = 'unknown'

class CancelToken(Protocol):
    def cancelled(self) -> bool: ...

@dataclass(frozen=True)
class Progress:
    stage: str
    current: int
    total: int | None

@dataclass(frozen=True)
class VerificationEvent:
    id: str
    reason: Literal['login', 'captcha']
    state: Literal['required', 'resolved', 'cancelled'] = 'required'


@dataclass(frozen=True)
class Region:
    x: float = 0
    y: float = .6
    width: float = 1
    height: float = .4

    def __post_init__(self):
        values = (self.x, self.y, self.width, self.height)
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
                or self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0
                or self.x + self.width > 1 or self.y + self.height > 1):
            raise DownloadError('input_limit')


@dataclass(frozen=True)
class DownloadOptions:
    save_video: bool = True
    prefer_clean: bool = True
    audio_format: Literal['mp3', 'wav'] | None = None
    silent_video: bool = False
    asr_language: Literal['auto', 'zh', 'en'] | None = None
    ocr_enabled: bool = False
    model_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if (any(type(value) is not bool for value in (self.save_video, self.prefer_clean, self.silent_video, self.ocr_enabled))
                or self.audio_format not in (None, 'mp3', 'wav')
                or self.asr_language not in (None, 'auto', 'zh', 'en')
                or type(self.model_ids) is not tuple or len(self.model_ids) > 8
                or any(type(model) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', model) for model in self.model_ids)
                or len(set(self.model_ids)) != len(self.model_ids)
                or not (self.save_video or self.audio_format or self.silent_video or self.asr_language or self.ocr_enabled)):
            raise DownloadError('input_limit')


StageState = Literal['waiting', 'running', 'succeeded', 'failed', 'skipped', 'cancelled', 'interrupted']


@dataclass(frozen=True)
class StageResult:
    stage: str
    state: StageState
    resource_id: str | None = None
    error_code: ErrorCode | None = None


@dataclass(frozen=True)
class OutputResource:
    id: str
    candidate_id: str
    item_id: str | None
    kind: str
    sha256: str
    size_bytes: int
    duration_ms: int | None = None


@dataclass(frozen=True)
class SubtitleSegment:
    id: str
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None

    def __post_init__(self):
        if (type(self.id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.id)
                or type(self.start_ms) is not int or type(self.end_ms) is not int
                or self.start_ms < 0 or self.end_ms <= self.start_ms
                or type(self.text) is not str or len(self.text) > 10000
                or self.confidence is not None and (type(self.confidence) not in (float, int)
                    or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1)):
            raise DownloadError('input_limit')
