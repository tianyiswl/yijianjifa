"""Local CPU Whisper recognition of a backend-owned ASR WAV.

The Runtime owns the input and shared local lane. Returned segments have ASR
provenance; Runtime/Repository creates a new immutable original and draft for
every run. This engine does not read candidate titles/descriptions or persist DB.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
from typing import Callable
import wave

from yijianjifa.os_integration.resources import resource_path
from .contracts import CancelToken, DownloadError, Progress, SubtitleSegment
from .media import _check_cancel, _run_process
from .models import ModelStore

MAX_JSON_BYTES = 32 * 1024 * 1024
_MODEL_ID = 'whisper-small-multilingual'
_LANGUAGES = {'auto', 'zh', 'en'}
_NON_SPEECH = re.compile(r'^\s*(?:\[(?:music|blank_audio|silence|noise|no speech|音乐|静音|噪音)\]|\((?:music|音乐|静音)\)|[♪♫\s])+\s*$', re.I)
_SPECIAL = re.compile(r'<\|[^|]*\|>')
_TIMESTAMP = re.compile(r'^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})$')
_MIN_USABLE_TOKEN_MEAN = .6  # conservative document gate, not speech probability/accuracy
_END_PADDING_MS = 500  # native boundary padding; emitted times never exceed the audio


def _whisper_tool() -> Path:
    # No PATH lookup or implicit development/model preparation fallback.
    name = 'whisper-cli.exe' if sys.platform.startswith('win') else 'whisper-cli'
    return resource_path(Path('bin') / name)


@dataclass(frozen=True)
class _Audio:
    duration_ms: int
    sha256: str
    signature: tuple
    rms: float


def _identity(path: Path) -> tuple:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise DownloadError('media_invalid')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _inspect_audio(path: Path, cancel: CancelToken, deadline: float) -> _Audio:
    try:
        before = _identity(path)
        digest = hashlib.sha256()
        with path.open('rb') as source:
            while chunk := source.read(1024 * 1024):
                _check_cancel(cancel, deadline)
                digest.update(chunk)
        with wave.open(str(path), 'rb') as source:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (16000, 1, 2, 'NONE'):
                raise DownloadError('media_invalid')
            count = source.getnframes()
            if not 0 < count <= 16000 * 6 * 3600:
                raise DownloadError('media_invalid')
            total, squares = 0, 0
            while chunk := source.readframes(16000):
                _check_cancel(cancel, deadline)
                if len(chunk) % 2:
                    raise DownloadError('media_invalid')
                samples = array('h', chunk)
                if sys.byteorder != 'little':
                    samples.byteswap()
                total += len(samples)
                squares += sum(value * value for value in samples)
            if total != count:
                raise DownloadError('media_invalid')
        _check_cancel(cancel, deadline)
        if _identity(path) != before:
            raise DownloadError('identity_mismatch')
        return _Audio(round(count * 1000 / 16000), digest.hexdigest(), before,
                      math.sqrt(squares / count) / 32768)
    except FileNotFoundError:
        raise DownloadError('source_missing') from None
    except (OSError, EOFError, wave.Error):
        raise DownloadError('media_invalid') from None


def _timestamp(value) -> int:
    if type(value) is not str or not (match := _TIMESTAMP.fullmatch(value)):
        raise ValueError
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _invalid_constant(_):
    raise ValueError


def _parse(raw: bytes, audio: _Audio, language: str) -> tuple[SubtitleSegment, ...]:
    try:
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError
        data = json.loads(raw.decode('utf-8', errors='strict'), parse_constant=_invalid_constant)
        if (type(data) is not dict or data['model']['type'] != 'small'
                or data['model']['multilingual'] is not True
                or data['params']['language'] != language or data['params']['translate'] is not False
                or type(data['result']['language']) is not str or not data['result']['language']):
            raise ValueError
        rows = data['transcription']
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError
        if audio.rms < .001:
            raise DownloadError('no_speech')
        result, previous_end, usable_probabilities = [], 0, []
        for index, row in enumerate(rows):
            start, end = row['offsets']['from'], row['offsets']['to']
            if (type(start) is not int or type(end) is not int or not previous_end <= start < end <= audio.duration_ms + _END_PADDING_MS
                    or _timestamp(row['timestamps']['from']) != start or _timestamp(row['timestamps']['to']) != end):
                raise ValueError
            previous_end = end
            bounded_end = min(end, audio.duration_ms)
            if start >= bounded_end:
                raise ValueError
            text = row['text']
            if type(text) is not str or len(text) > 10000:
                raise ValueError
            text.encode('utf-8', errors='strict')
            if any(ord(char) < 32 and char not in '\n\t\r' for char in text):
                raise ValueError
            text = text.strip()
            tokens = row['tokens']
            if not isinstance(tokens, list) or len(tokens) > 10000:
                raise ValueError
            probabilities = []
            for token in tokens:
                token_text, probability = token['text'], token['p']
                if (type(token_text) is not str or type(probability) not in (int, float)
                        or not math.isfinite(probability) or not 0 <= probability <= 1):
                    raise ValueError
                token_text.encode('utf-8', errors='strict')
                lexical = _SPECIAL.sub('', token_text).strip()
                if lexical and any(char.isalnum() for char in lexical):
                    probabilities.append(probability)
            # These are conservative usable-output gates, not calibrated speech
            # detection or accuracy scores. Whisper's native no-speech gate also
            # runs. Explicit non-speech markers never become invented subtitles.
            confidence = sum(probabilities) / len(probabilities) if probabilities else 0
            if (not text or _NON_SPEECH.fullmatch(text) or not any(char.isalnum() for char in _SPECIAL.sub('', text))
                    or not probabilities):
                continue
            usable_probabilities.extend(probabilities)
            identity = hashlib.sha256(json.dumps([audio.sha256, index, start, bounded_end, text], ensure_ascii=False,
                                                 separators=(',', ':')).encode('utf-8')).hexdigest()
            result.append(SubtitleSegment(identity, start, bounded_end, text, confidence))
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise DownloadError('subtitle_invalid') from None
    # Gate a whole unusable document; preserve low-confidence words/segments in
    # accepted documents for human editing rather than silently dropping them.
    if (not result or sum(usable_probabilities) / len(usable_probabilities) < _MIN_USABLE_TOKEN_MEAN):
        raise DownloadError('no_speech')
    return tuple(result)


class _OutputGuard:
    def __init__(self, cancel: CancelToken, output: Path):
        self.cancel = cancel
        self.output = output
    def cancelled(self) -> bool:
        if self.cancel.cancelled():
            return True
        try:
            info = self.output.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
                raise DownloadError('subtitle_invalid')
        except FileNotFoundError:
            pass
        except OSError:
            raise DownloadError('subtitle_invalid') from None
        return False


class AsrEngine:
    origin = 'asr'

    def __init__(self, models: ModelStore, *, tool: Path | None = None, scratch_root: Path | None = None,
                 timeout: float = 3600, threads: int = 4, terminate_grace: float = .2):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(threads) is not int or not 1 <= threads <= 16
                or type(terminate_grace) not in (int, float) or not math.isfinite(terminate_grace)
                or not 0 <= terminate_grace <= 2):
            raise DownloadError('input_limit')
        self.models = models
        self.tool = Path(tool or _whisper_tool()).absolute()
        self.scratch_root = scratch_root
        self.timeout, self.threads, self.grace = timeout, threads, terminate_grace
        self._pending_cleanup: set[Path] = set()
        self._cleanup_lock = threading.Lock()

    def pending_cleanup(self) -> tuple[Path, ...]:
        """Backend-only exact paths; Runtime rechecks identity before retrying."""
        with self._cleanup_lock:
            return tuple(sorted(self._pending_cleanup))

    def transcribe(self, audio: Path, *, model_id: str, language: str, cancel: CancelToken,
                   progress: Callable[[Progress], None]) -> tuple[SubtitleSegment, ...]:
        if type(language) is not str or language not in _LANGUAGES:
            raise DownloadError('input_limit')
        if type(model_id) is not str or model_id != _MODEL_ID:
            raise DownloadError('model_unknown')
        deadline = time.monotonic() + self.timeout
        _check_cancel(cancel, deadline)
        model = self.models.require(model_id)
        _check_cancel(cancel, deadline)
        audio = Path(audio).absolute()
        original = _inspect_audio(audio, cancel, deadline)
        progress(Progress('asr', 0, original.duration_ms))
        temporary = None
        output = None
        try:
            if self.scratch_root is not None:
                self.scratch_root.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix='owned-asr-', dir=self.scratch_root))
            prefix = temporary / 'transcription'
            output = prefix.with_suffix('.json')
            arguments = ('--model', str(model), '--file', str(audio), '--language', language,
                         '--threads', str(self.threads), '--processors', '1', '--no-gpu',
                         '--output-json-full', '--output-file', str(prefix), '--no-prints',
                         '--suppress-nst', '--no-speech-thold', '0.6', '--no-fallback')
            _run_process(self.tool, arguments, cancel=_OutputGuard(cancel, output), deadline=deadline,
                         grace=self.grace, output_limit=65536)
            _check_cancel(cancel, deadline)
            after = _inspect_audio(audio, cancel, deadline)
            if (after.signature, after.sha256) != (original.signature, original.sha256):
                raise DownloadError('identity_mismatch')
            try:
                info = output.lstat()
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_JSON_BYTES:
                    raise DownloadError('subtitle_invalid')
                with output.open('rb') as stream:
                    raw = stream.read(MAX_JSON_BYTES + 1)
            except OSError:
                raise DownloadError('subtitle_invalid') from None
            result = _parse(raw, original, language)
            _check_cancel(cancel, deadline)
            progress(Progress('asr', original.duration_ms, original.duration_ms))
            return result
        except DownloadError:
            raise
        except OSError:
            raise DownloadError('media_failed') from None
        finally:
            # Only this run's known JSON and empty directory; never scan/delete
            # the stage WAV, model, unrelated diagnostics or previous results.
            if temporary is not None:
                for path, is_dir in ((output, False), (temporary, True)):
                    if path is None:
                        continue
                    try:
                        path.rmdir() if is_dir else path.unlink(missing_ok=True)
                    except OSError:
                        with self._cleanup_lock:
                            self._pending_cleanup.add(path)
