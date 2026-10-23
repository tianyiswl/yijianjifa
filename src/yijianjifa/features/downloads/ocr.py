"""Local subtitle OCR, called while the Runtime owns its single processing slot.

Only backend-owned paths reach this module. The killable worker receives explicit
verified models and streams cropped frames in memory; it never downloads models.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import stat
import sys
import time
from typing import Callable, Iterable
from uuid import uuid4

from yijianjifa.os_integration.resources import ffmpeg_path, ffprobe_path
from .contracts import CancelToken, DownloadError, Progress, Region, SubtitleSegment
from .media import MediaProcessor, _check_cancel, _run_process
from .models import ModelStore

DEFAULT_OCR_MODEL_IDS = ('rapidocr-det-ppocrv6-small', 'rapidocr-cls-mobile-v2', 'rapidocr-rec-ppocrv6-small')
_MODEL_COMPONENTS = dict(zip(DEFAULT_OCR_MODEL_IDS, ('Det', 'Cls', 'Rec')))
_MAX_DURATION_MS = 2 * 60 * 60 * 1000
_MAX_SEGMENTS = 10000
_RESULT_LIMIT = 2 * 1024 * 1024
_OUTPUT_LIMIT = 4 * 1024 * 1024
_WORKER_ERRORS = {'ocr_failed', 'no_text', 'model_required', 'model_invalid', 'media_invalid',
                  'media_tool_missing', 'source_missing', 'input_limit', 'size_limit'}


@dataclass(frozen=True)
class _Text:
    text: str
    confidence: float | None

    def __post_init__(self):
        if (type(self.text) is not str or len(self.text) > 4096
                or self.confidence is not None and (type(self.confidence) not in (float, int)
                    or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1)):
            raise DownloadError('ocr_failed')
        normalized = '\n'.join(' '.join(line.split()) for line in self.text.splitlines() if line.strip())
        object.__setattr__(self, 'text', normalized)


def _crop_box(width: int, height: int, region: Region) -> tuple[int, int, int, int]:
    if (type(width) is not int or type(height) is not int or not 0 < width <= 8192
            or not 0 < height <= 8192 or width * height > 16777216 or not isinstance(region, Region)):
        raise DownloadError('media_invalid')
    left, top = math.floor(region.x * width), math.floor(region.y * height)
    right = min(width, math.ceil((region.x + region.width) * width))
    bottom = min(height, math.ceil((region.y + region.height) * height))
    return left, top, max(1, right - left), max(1, bottom - top)


def _scan_timeline(frames: Iterable[tuple[int, object]], duration_ms: int,
                   infer: Callable[[object], _Text], progress: Callable[[Progress], None], *,
                   start_ms: int = 0, end_ms: int | None = None) -> tuple[SubtitleSegment, ...]:
    """500ms base observations; refine changed intervals on the 100ms frame grid.

    At most five cropped frames wait for the next base observation. Equal base
    observations can hide a short intervening subtitle; no precision is implied.
    """
    if type(duration_ms) is not int or not 0 < duration_ms <= _MAX_DURATION_MS:
        raise DownloadError('media_invalid')
    end_ms = duration_ms if end_ms is None else end_ms
    if (type(start_ms) is not int or type(end_ms) is not int
            or not 0 <= start_ms < end_ms <= duration_ms):
        raise DownloadError('media_invalid')
    rows, pending = [], []
    active = _Text('', None)
    started, expected, budget = start_ms, start_ms, 0
    previous_base = None

    def observe(at: int, value: _Text) -> None:
        nonlocal active, started, budget
        if not isinstance(value, _Text):
            raise DownloadError('ocr_failed')
        if value.text == active.text:
            scores = [s for s in (active.confidence, value.confidence) if s is not None]
            active = _Text(active.text, min(scores) if scores else None)
            return
        if active.text:
            budget += len(active.text.encode('utf-8')) + 160
            if len(rows) >= _MAX_SEGMENTS or budget > _RESULT_LIMIT:
                raise DownloadError('size_limit')
            rows.append(SubtitleSegment(uuid4().hex, started, at, active.text, active.confidence))
        active, started = value, at

    def base(at: int, frame: object) -> None:
        nonlocal previous_base
        value = infer(frame)
        if previous_base is not None and value.text != previous_base.text:
            for fine_at, fine in pending[:-1]:
                observe(fine_at, infer(fine))
        observe(at, value)
        previous_base = value
        pending.clear()
        progress(Progress('ocr', at, duration_ms))

    for at, frame in frames:
        if type(at) is not int or at != expected or at >= end_ms:
            raise DownloadError('media_invalid')
        expected += 100
        pending.append((at, frame))
        if (at - start_ms) % 500 == 0:
            base(at, frame)
    if expected == start_ms or expected < end_ms:
        raise DownloadError('media_invalid')
    if pending:
        base(*pending[-1])
    observe(end_ms, _Text('', None))
    progress(Progress('ocr', duration_ms, duration_ms))
    if not rows:
        raise DownloadError('no_text')
    return tuple(rows)


def _worker_command(payload: dict, worker_python: Path | None) -> tuple[Path, tuple[str, ...]]:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(',', ':'))
    if getattr(sys, 'frozen', False):
        if worker_python is not None:
            raise DownloadError('ocr_failed')
        # Task14 must dispatch this mode before importing/starting the Qt Host.
        return Path(sys.executable).absolute(), ('--yjjf-ocr-worker', encoded)
    return Path(worker_python or sys.executable).absolute(), ('-m', 'yijianjifa.features.downloads.ocr_worker', encoded)


class OcrEngine:
    origin = 'ocr'

    def __init__(self, models: ModelStore, *, ffmpeg: Path | None = None, ffprobe: Path | None = None,
                 worker_python: Path | None = None, timeout: float = 3600, terminate_grace: float = .2):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(terminate_grace) not in (int, float) or not math.isfinite(terminate_grace)
                or not 0 <= terminate_grace <= 2):
            raise DownloadError('input_limit')
        self.models = models
        self.ffmpeg = Path(ffmpeg or ffmpeg_path()).resolve()
        self.ffprobe = Path(ffprobe or ffprobe_path()).absolute()
        self.worker_python = Path(worker_python).absolute() if worker_python is not None else None
        self.timeout, self.grace = timeout, terminate_grace
        self.last_diagnostics: dict = {}

    def recognize(self, video: Path, *, region: Region, model_ids: tuple[str, ...], cancel: CancelToken,
                  progress: Callable[[Progress], None]) -> tuple[SubtitleSegment, ...]:
        deadline = time.monotonic() + self.timeout
        _check_cancel(cancel, deadline)
        self.last_diagnostics = {}
        if (not isinstance(region, Region) or type(model_ids) is not tuple or len(model_ids) > 3
                or any(type(model_id) is not str for model_id in model_ids)
                or len(set(model_ids)) != len(model_ids)):
            raise DownloadError('input_limit')
        if not model_ids:
            raise DownloadError('model_required')
        if any(model_id not in _MODEL_COMPONENTS for model_id in model_ids):
            raise DownloadError('model_unknown')
        if set(model_ids) != set(DEFAULT_OCR_MODEL_IDS):
            raise DownloadError('model_required')
        models = {component: str(self.models.require(model_id).absolute())
                  for model_id, component in _MODEL_COMPONENTS.items()}
        _check_cancel(cancel, deadline)
        video = Path(video).absolute()
        try:
            info = video.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise DownloadError('media_invalid')
        except FileNotFoundError:
            raise DownloadError('source_missing') from None
        except OSError:
            raise DownloadError('media_invalid') from None
        MediaProcessor._verify_mp4_header(video)
        duration_ms, stream, width, height, start_ms, end_ms = self._probe(video, cancel, deadline)
        crop = _crop_box(width, height, region)
        ratio = min(1, 1280 / max(crop[2:]))
        scaled = [max(1, round(crop[2] * ratio)), max(1, round(crop[3] * ratio))]
        payload = {'video': str(video), 'ffmpeg': str(self.ffmpeg), 'models': models, 'crop': crop,
                   'size': scaled, 'stream': stream, 'duration_ms': duration_ms,
                   'start_ms': start_ms, 'end_ms': end_ms}
        tool, args = _worker_command(payload, self.worker_python)
        raw = _run_process(tool, args, cancel=cancel, deadline=deadline, grace=self.grace,
                           on_line=lambda line: self._progress(line, duration_ms, progress),
                           output_limit=_OUTPUT_LIMIT, failure_code='ocr_failed')
        _check_cancel(cancel, deadline)
        rows = self._result(raw, duration_ms, start_ms=start_ms, end_ms=end_ms)
        progress(Progress('ocr', duration_ms, duration_ms))
        return rows

    def _probe(self, video: Path, cancel: CancelToken, deadline: float) -> tuple[int, int, int, int, int, int]:
        args = ('-v', 'error', '-protocol_whitelist', 'file,pipe', '-f', 'mov', '-enable_drefs', '0',
                '-use_absolute_path', '0', '-show_streams', '-show_format', '-of', 'json', str(video))
        raw = _run_process(self.ffprobe, args, cancel=cancel, deadline=deadline, grace=self.grace,
                           output_limit=65536, failure_code='media_invalid')
        try:
            parsed = json.loads(raw)
            streams = parsed['streams']
            if type(streams) is not list or not 0 < len(streams) <= 64:
                raise ValueError
            stream = next(row for row in streams if row['codec_type'] == 'video'
                          and not row.get('disposition', {}).get('attached_pic'))
            # Use the same container playback duration as preview/output/ASR.
            # Stream length alone excludes a delayed video track's leading gap.
            duration = float(parsed['format']['duration'])
            origin = float(parsed['format'].get('start_time', 0))
            video_start = float(stream.get('start_time', origin)) - origin
            video_length = float(stream.get('duration', duration - max(0, video_start)))
            duration_ms = round(duration * 1000)
            start_ms = max(0, round(video_start * 1000))
            end_ms = min(duration_ms, round((video_start + video_length) * 1000))
            index, width, height = stream['index'], stream['width'], stream['height']
            if (not all(math.isfinite(value) for value in (duration, origin, video_start, video_length))
                    or not 0 < duration_ms <= _MAX_DURATION_MS or video_length <= 0
                    or not 0 <= start_ms < end_ms <= duration_ms
                    or abs(origin) > _MAX_DURATION_MS / 1000
                    or video_start + video_length > duration + .1
                    or type(index) is not int or not 0 <= index < 1024):
                raise ValueError
            rotation = next((row['rotation'] for row in stream.get('side_data_list', [])
                             if row.get('side_data_type') == 'Display Matrix'), stream.get('tags', {}).get('rotate', 0))
            rotation = float(rotation)
            if not math.isfinite(rotation) or rotation % 90:
                raise ValueError
            if rotation % 180:
                width, height = height, width
            _crop_box(width, height, Region(0, 0, 1, 1))
            return duration_ms, index, width, height, start_ms, end_ms
        except (ValueError, TypeError, KeyError, StopIteration, OverflowError):
            raise DownloadError('media_invalid') from None

    @staticmethod
    def _progress(line: bytes, duration_ms: int, callback: Callable[[Progress], None]) -> None:
        try:
            event = json.loads(line)
            if (event.get('type') == 'progress' and type(event.get('current')) is int
                    and event.get('total') == duration_ms and 0 <= event['current'] <= duration_ms):
                callback(Progress('ocr', event['current'], duration_ms))
        except (ValueError, TypeError, AttributeError):
            pass  # Partial result lines are validated from the complete bounded capture.

    def _result(self, raw: bytes, duration_ms: int, *, start_ms: int = 0,
                end_ms: int | None = None) -> tuple[SubtitleSegment, ...]:
        if len(raw) >= _OUTPUT_LIMIT:
            raise DownloadError('size_limit')
        end_ms = duration_ms if end_ms is None else end_ms
        try:
            messages = [json.loads(line) for line in raw.splitlines() if line.strip()]
            finals = [message for message in messages if message.get('type') in ('result', 'error')]
            if len(finals) != 1 or finals[0] is not messages[-1]:
                raise ValueError
            result = finals[0]
            if result['type'] == 'error':
                raise DownloadError(result['code'] if result.get('code') in _WORKER_ERRORS else 'ocr_failed')
            if result['origin'] != 'ocr' or result['duration_ms'] != duration_ms:
                raise ValueError
            segments = result['segments']
            if type(segments) is not list or len(segments) > _MAX_SEGMENTS:
                raise ValueError
            if not segments:
                raise DownloadError('no_text')
            rows, previous_end = [], start_ms
            for row in segments:
                value = _Text(row['text'], row['confidence'])
                start, end = row['start_ms'], row['end_ms']
                if (type(start) is not int or type(end) is not int or not previous_end <= start < end <= end_ms
                        or not value.text):
                    raise ValueError
                rows.append(SubtitleSegment(uuid4().hex, start, end, value.text, value.confidence))
                previous_end = end
            diagnostics = result.get('diagnostics', {})
            if type(diagnostics) is not dict:
                raise ValueError
            self.last_diagnostics = {key: value for key, value in diagnostics.items() if key in
                ('engine_load_count', 'providers', 'rapidocr_version', 'onnxruntime_version', 'network_attempt_count',
                 'decoded_frames', 'ocr_calls', 'max_buffer_frames', 'base_sample_ms', 'boundary_sample_ms')}
            return tuple(rows)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise DownloadError('ocr_failed') from None
