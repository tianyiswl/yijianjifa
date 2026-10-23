"""Actual CPU recognition. Missing assets are explicit skips, never acceptance."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import wave
from unittest.mock import patch

import pytest

from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.downloads.models import ModelStore
from yijianjifa.os_integration.resources import ffmpeg_path


ROOT = Path(__file__).resolve().parents[3]
PREP = ROOT / 'build' / 'recognition-preparation'
MODEL = 'whisper-small-multilingual'
REFERENCE = '今天我们测试本地语音识别。原始视频会保留，识别文字需要人工核对。没有声音时，不应该生成字幕。'


class NeverCancel:
    def cancelled(self):
        return False


@pytest.fixture(scope='module')
def actual_models():
    if not (PREP / 'assets' / 'ggml-small.bin').is_file():
        pytest.skip('actual multilingual small model is missing; real ASR acceptance pending')
    store = ModelStore(PREP / 'assets')
    store.require(MODEL)
    return store


def actual_engine(actual_models, tmp_path):
    from yijianjifa.features.downloads.asr import AsrEngine
    cli = PREP / 'macos-whisper-cpu-build' / 'bin' / 'whisper-cli'
    if not cli.is_file() or sys.platform != 'darwin':
        pytest.skip('prepared Mac CPU CLI unavailable; target platform acceptance pending')
    return AsrEngine(actual_models, tool=cli, scratch_root=tmp_path / 'scratch', timeout=180)


def convert(source, target, *extra):
    subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y', *extra, '-i', str(source),
                    '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(target)],
                   capture_output=True, check=True, timeout=30)


def persist(evidence):
    destination = PREP / 'metadata' / 'task7-real-asr-acceptance.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(destination.read_text()) if destination.exists() else {}
    existing[evidence['case']] = evidence
    destination.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')


def transcribe_record(media, audio, *, case, reference=None, reference_kind=None, expect=None, language='zh', source=None):
    before = hashlib.sha256(audio.read_bytes()).hexdigest()
    started = time.monotonic()
    evidence = {'case': case, 'model_id': MODEL, 'language': language, 'engine_cpu': True,
                'reference': reference, 'reference_kind': reference_kind,
                'source_audio_sha256': before, 'source_video': source,
                'scope': 'local unfrozen CPU acceptance; not target Windows/frozen/runtime/platform or corpus accuracy'}
    from yijianjifa.features.downloads import asr
    original_runner = asr._run_process
    def record_actual_tool(tool, arguments, **kwargs):
        # Always execute the real tool; capture native output before its owned
        # scratch is removed. This does not stub or replace recognition output.
        result = original_runner(tool, arguments, **kwargs)
        output = Path(arguments[arguments.index('--output-file') + 1]).with_suffix('.json')
        raw = json.loads(output.read_bytes())
        evidence['native_text'] = ''.join(row['text'] for row in raw['transcription'])
        evidence['native_offsets'] = [row['offsets'] for row in raw['transcription']]
        evidence['native_model'] = raw['model']
        evidence['native_systeminfo'] = raw['systeminfo']
        return result
    try:
        with patch.object(asr, '_run_process', record_actual_tool):
            segments = media.transcribe(audio, model_id=MODEL, language=language, cancel=NeverCancel(), progress=lambda _: None)
        evidence.update(state='succeeded', segments=[asdict(row) for row in segments], text=''.join(row.text for row in segments))
        if reference is not None:
            expected = ''.join(char for char in reference if char.isalnum())
            actual = ''.join(char for char in evidence['text'] if char.isalnum())
            evidence['reference_words_match_ignoring_punctuation'] = expected == actual
    except DownloadError as error:
        evidence.update(state='failed', error_code=error.code)
        segments = None
        if error.code != expect:
            evidence['unexpected_failure'] = True
            raise
    finally:
        evidence['elapsed_seconds'] = time.monotonic() - started
        evidence['source_unchanged'] = hashlib.sha256(audio.read_bytes()).hexdigest() == before
        with wave.open(str(audio), 'rb') as input_audio:
            evidence['duration_ms'] = round(input_audio.getnframes() * 1000 / input_audio.getframerate())
        persist(evidence)
    assert evidence['source_unchanged']
    if expect:
        assert evidence.get('error_code') == expect, 'non-speech control produced a usable caption'
    return segments


def test_actual_cpu_chinese_speech_synthesized_reference(actual_models, tmp_path):
    if sys.platform != 'darwin' or not shutil.which('say'):
        pytest.skip('local authorized Chinese synthesis unavailable; real reference pending')
    speech = tmp_path / 'reference.aiff'
    subprocess.run(['/usr/bin/say', '-v', 'Tingting', '-r', '155', '-o', str(speech), REFERENCE],
                   capture_output=True, check=True, timeout=30)
    audio = tmp_path / 'reference.wav'
    convert(speech, audio)
    rows = transcribe_record(actual_engine(actual_models, tmp_path), audio, case='synthetic_chinese', reference=REFERENCE,
                            reference_kind='macOS Tingting synthesized speech; not human voice')
    assert rows and ''.join(char for row in rows for char in row.text if char.isalnum()) == ''.join(char for char in REFERENCE if char.isalnum())
    assert all(0 <= row.start_ms < row.end_ms for row in rows)


@pytest.mark.parametrize('control', ['silence', 'synthesized_music'])
def test_actual_cpu_non_speech_controls(actual_models, tmp_path, control):
    audio = tmp_path / (control + '.wav')
    if control == 'silence':
        with wave.open(str(audio), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\0\0' * 16000 * 6)
    else:
        synthesis = 'aevalsrc=0.06*(sin(2*PI*220*t)+sin(2*PI*330*t)+sin(2*PI*440*t))*(0.7+0.3*sin(2*PI*2*t)):s=16000:d=6'
        subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', synthesis,
                        '-ac', '1', '-c:a', 'pcm_s16le', str(audio)], capture_output=True, check=True, timeout=30)
    transcribe_record(actual_engine(actual_models, tmp_path), audio, case=control, reference='',
                      reference_kind='zero PCM' if control == 'silence' else 'synthesized wordless harmonic music', expect='no_speech')


def test_actual_cpu_authorized_human_video_excerpt(actual_models, tmp_path):
    source = ROOT / 'build/download-probe/task-2-bounded-native/7686432680413629742.mp4'
    if not source.is_file():
        pytest.skip('authorized local real-video sample missing; human sample acceptance pending')
    audio = tmp_path / 'human-excerpt.wav'
    # Sample 0-30 s of the authorized 241.1 s video, not a complete-video accuracy claim.
    subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-i', str(source), '-t', '30',
                    '-map', '0:a:0', '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(audio)],
                   capture_output=True, check=True, timeout=30)
    rows = transcribe_record(actual_engine(actual_models, tmp_path), audio, case='authorized_human_excerpt_0_30s',
                            reference_kind='actual human audio; no exact independent human transcription',
                            source='authorized local 7686432680413629742.mp4; first 30 seconds')
    assert rows and len(re.findall(r'[\u3400-\u9fff]', ''.join(row.text for row in rows))) >= 10


@pytest.mark.parametrize('stop', ['cancelled', 'timeout'])
def test_actual_cpu_cli_started_cancellation_reaps_and_cleans_scratch(actual_models, tmp_path, monkeypatch, stop):
    media = actual_engine(actual_models, tmp_path)
    if stop == 'timeout':
        media.timeout = .3
    input_audio = PREP / 'source' / 'jfk-mono-16000.wav'
    if not input_audio.is_file():
        pytest.skip('prepared public speech WAV unavailable; real cancellation acceptance pending')
    audio = tmp_path / 'cancel-input.wav'
    shutil.copyfile(input_audio, audio)
    initial = hashlib.sha256(audio.read_bytes()).hexdigest()
    began, processes = [], []
    original_popen = subprocess.Popen
    def capture(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, 'Popen', capture)
    class AfterStart:
        def cancelled(self):
            return stop == 'cancelled' and bool(began) and time.monotonic() - began[0] >= .2
    with pytest.raises(DownloadError) as failure:
        media.transcribe(audio, model_id=MODEL, language='en', cancel=AfterStart(), progress=lambda _: began.append(time.monotonic()))
    assert failure.value.code == stop
    assert len(processes) == 1 and processes[0].poll() is not None
    assert list((tmp_path / 'scratch').iterdir()) == []
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == initial
    persist({'case': 'actual_cli_started_' + stop, 'state': stop, 'error_code': stop,
             'process_reaped': processes[0].returncode is not None, 'own_scratch_empty': True, 'source_unchanged': True,
             'scope': 'actual CPU CLI started cancellation/timeout, may occur during model loading; not full inference progress proof'})
