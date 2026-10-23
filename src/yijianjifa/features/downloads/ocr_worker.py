"""Pure OCR child entry point; frozen Host dispatches --yjjf-ocr-worker here.

No Qt, database, Host, processing semaphore, model download, or frame files.
The parent's process group/Windows Job owns this process and its FFmpeg child.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib.metadata
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import urllib.request

from .contracts import DownloadError
from .ocr import _MAX_DURATION_MS, _RESULT_LIMIT, _Text, _WORKER_ERRORS, _scan_timeline


def _emit(message: dict) -> None:
    raw = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > _RESULT_LIMIT:
        raise DownloadError('size_limit')
    sys.stdout.buffer.write(raw + b'\n')
    sys.stdout.buffer.flush()


def _payload(argument: str) -> dict:
    try:
        if len(argument) > 32768:
            raise ValueError
        data = json.loads(argument)
        for key, missing_code in (('video', 'source_missing'), ('ffmpeg', 'media_tool_missing')):
            path = Path(data[key])
            try:
                if not path.is_absolute() or not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError
            except FileNotFoundError:
                raise DownloadError(missing_code) from None
        if set(data['models']) != {'Det', 'Cls', 'Rec'}:
            raise ValueError
        for selected in data['models'].values():
            path = Path(selected)
            if not path.is_absolute() or not stat.S_ISREG(path.lstat().st_mode):
                raise DownloadError('model_required')
        duration, stream = data['duration_ms'], data['stream']
        if (type(duration) is not int or not 0 < duration <= _MAX_DURATION_MS
                or type(stream) is not int or not 0 <= stream < 1024
                or len(data['crop']) != 4 or len(data['size']) != 2
                or any(type(x) is not int for x in [*data['crop'], *data['size']])
                or any(not 0 <= x < 8192 for x in data['crop'][:2])
                or any(not 0 < x <= 8192 for x in data['crop'][2:])
                or any(not 0 < x <= 1280 for x in data['size'])):
            raise ValueError
        _sample_window(data)
        return data
    except FileNotFoundError:
        raise DownloadError('model_required') from None
    except (ValueError, KeyError, TypeError, OSError):
        raise DownloadError('ocr_failed') from None


def _offline_guard(attempts: list) -> None:
    def blocked(*args, **kwargs):
        attempts.append(True)
        raise DownloadError('ocr_failed')
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked
    urllib.request.urlopen = blocked
    import requests
    requests.sessions.Session.request = blocked
    from rapidocr.utils.download_file import DownloadFile
    DownloadFile.run = blocked


def _load_engine(models: dict, attempts: list):
    # The worker is dedicated to one video: process-wide guards are intentional.
    if (importlib.metadata.version('rapidocr') != '3.9.2'
            or importlib.metadata.version('onnxruntime') != '1.22.1'):
        raise DownloadError('ocr_failed')
    _offline_guard(attempts)
    from rapidocr import RapidOCR
    params = {component + '.model_path': path for component, path in models.items()}
    params.update({'Global.text_score': 0, 'Global.log_level': 'critical',
                   'EngineConfig.onnxruntime.use_cuda': False, 'EngineConfig.onnxruntime.use_dml': False,
                   'EngineConfig.onnxruntime.use_cann': False, 'EngineConfig.onnxruntime.use_coreml': False,
                   'EngineConfig.onnxruntime.intra_op_num_threads': 2,
                   'EngineConfig.onnxruntime.inter_op_num_threads': 1})
    engine = RapidOCR(params=params)
    for component in (engine.text_det, engine.text_cls, engine.text_rec):
        if component.session.session.get_providers() != ['CPUExecutionProvider']:
            raise DownloadError('ocr_failed')
    return engine


def _frame_text(engine, frame) -> _Text:
    result = engine(frame)
    if result.txts is None:
        return _Text('', None)
    texts = list(result.txts)
    if result.scores is None or len(texts) != len(result.scores) or len(texts) > 128:
        raise DownloadError('ocr_failed')
    parts, scores = [], []
    for text, score in zip(texts, result.scores):
        value = _Text(text, float(score))
        if value.text:
            parts.append(value.text)
            scores.append(value.confidence)
    return _Text('\n'.join(parts), min(scores) if scores else None)


def _decoder_spawn_options(platform: str | None = None) -> dict:
    # Do not break away from the parent's Windows Job or POSIX process group.
    return {'creationflags': 0x08000000} if (platform or os.name) == 'nt' else {}


def _sample_window(data: dict) -> tuple[int, int, bool]:
    start, end = data.get('start_ms', 0), data.get('end_ms', data['duration_ms'])
    if (type(start) is not int or type(end) is not int
            or not 0 <= start < end <= data['duration_ms']):
        raise DownloadError('media_invalid')
    first = ((start + 99) // 100) * 100
    # If the real clip fits entirely between grid ticks, decode its original
    # first frame once. Do not move it to zero or clone it into the leading gap.
    single = first >= end
    return start if single else first, end, single


def _frames(data: dict, diagnostics: dict):
    import numpy as np
    x, y, width, height = data['crop']
    out_width, out_height = data['size']
    first, end, single = _sample_window(data)
    sample_count = 1 if single else math.ceil((end - first) / 100)
    # Pad only the decoder tail; never copy a frame into the leading gap.
    # Samples at/after the real video end cannot extend subtitles.
    filters = ('' if single else f'tpad=stop_mode=clone:stop_duration=0.1,fps=10:start_time={first / 1000:.3f}:round=near,')
    filters += f'crop={width}:{height}:{x}:{y}:exact=1,scale={out_width}:{out_height}'
    args = [data['ffmpeg'], '-nostdin', '-hide_banner', '-v', 'error', '-copyts', '-start_at_zero',
            '-protocol_whitelist', 'file,pipe',
            '-f', 'mov', '-enable_drefs', '0', '-use_absolute_path', '0', '-i', data['video'],
            '-map', f"0:{data['stream']}", '-an', '-sn', '-dn', '-vf', filters,
            '-threads', '1', '-frames:v', str(sample_count), '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
    child = None
    try:
        # Inherit the worker's owned group/job. A new session here would escape it.
        child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, shell=False, close_fds=True, **_decoder_spawn_options())
        count, frame_bytes = 0, out_width * out_height * 3
        while True:
            buffer = bytearray()
            while len(buffer) < frame_bytes:
                chunk = child.stdout.read(frame_bytes - len(buffer))
                if not chunk:
                    break
                buffer.extend(chunk)
            if not buffer:
                break
            if len(buffer) != frame_bytes or count >= sample_count:
                raise DownloadError('media_invalid')
            at = first + count * 100
            count += 1
            if at < end:
                diagnostics['decoded_frames'] += 1
                buffered = 1 if count == 1 else (count - 2) % 5 + 1
                diagnostics['max_buffer_frames'] = max(diagnostics['max_buffer_frames'], buffered)
                yield at, np.frombuffer(buffer, dtype=np.uint8).reshape(out_height, out_width, 3)
        if child.wait() != 0:
            raise DownloadError('media_invalid')
    except (FileNotFoundError, PermissionError):
        raise DownloadError('media_tool_missing') from None
    except OSError:
        raise DownloadError('ocr_failed') from None
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()


def worker_main(argv: list[str] | None = None) -> int:
    """Return before Host initialization; errors are finite packets without paths."""
    try:
        arguments = sys.argv[1:] if argv is None else argv
        if len(arguments) != 1:
            raise DownloadError('ocr_failed')
        data = _payload(arguments[0])
        attempts = []
        # Keep engine/library diagnostics off the bounded JSON protocol stream.
        with redirect_stdout(sys.stderr):
            engine = _load_engine(data['models'], attempts)
        diagnostics = {'engine_load_count': 1, 'providers': ['CPUExecutionProvider'],
                       'rapidocr_version': '3.9.2', 'onnxruntime_version': '1.22.1',
                       'network_attempt_count': 0, 'decoded_frames': 0, 'ocr_calls': 0,
                       'max_buffer_frames': 0, 'base_sample_ms': 500, 'boundary_sample_ms': 100}
        def infer(frame):
            diagnostics['ocr_calls'] += 1
            with redirect_stdout(sys.stderr):
                return _frame_text(engine, frame)
        frames = _frames(data, diagnostics)
        first, end, _ = _sample_window(data)
        try:
            rows = _scan_timeline(frames, data['duration_ms'], infer,
                                   lambda p: _emit({'type': 'progress', 'current': p.current, 'total': p.total}),
                                   start_ms=first, end_ms=end)
        finally:
            frames.close()
        diagnostics['network_attempt_count'] = len(attempts)
        if attempts:
            raise DownloadError('ocr_failed')
        _emit({'type': 'result', 'origin': 'ocr', 'duration_ms': data['duration_ms'],
               'segments': [{'start_ms': r.start_ms, 'end_ms': r.end_ms, 'text': r.text, 'confidence': r.confidence}
                            for r in rows], 'diagnostics': diagnostics})
        return 0
    except DownloadError as error:
        _emit({'type': 'error', 'code': error.code if error.code in _WORKER_ERRORS else 'ocr_failed'})
    except Exception:
        _emit({'type': 'error', 'code': 'ocr_failed'})
    return 0


if __name__ == '__main__':
    raise SystemExit(worker_main())
