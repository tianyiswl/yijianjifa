"""Parser/process contract tests; these are not real recognition acceptance."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import wave

import pytest

from yijianjifa.features.downloads.contracts import DownloadError


MODEL = 'whisper-small-multilingual'


class NeverCancel:
    def cancelled(self):
        return False


class Models:
    def __init__(self, root):
        self.path = root / 'explicit-small.bin'
        self.path.write_bytes(b'test-model')
        self.calls = []
    def require(self, model_id):
        self.calls.append(model_id)
        return self.path


def wav(root, *, silence=False, rate=16000, channels=1):
    path = root / 'audio.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        sample = b'\0\0' if silence else b'\xe8\x03'
        out.writeframes(sample * rate * channels)
    return path


def document(text='中文识别结果', *, end=1000, probability=.9):
    return {'model': {'type': 'small', 'multilingual': True},
            'params': {'language': 'zh', 'translate': False},
            'result': {'language': 'zh'},
            'transcription': [{'offsets': {'from': 0, 'to': end},
                               'timestamps': {'from': '00:00:00,000', 'to': '00:00:01,000'},
                               'text': text, 'tokens': [{'text': text, 'p': probability, 'id': 100}]}]}


def engine(root, **kwargs):
    assert importlib.util.find_spec('yijianjifa.features.downloads.asr') is not None, 'AsrEngine is missing'
    from yijianjifa.features.downloads.asr import AsrEngine
    return AsrEngine(Models(root), scratch_root=root / 'scratch', **kwargs)


def output_runner(monkeypatch, payload):
    from yijianjifa.features.downloads import asr
    calls = []
    def run(tool, arguments, **kwargs):
        calls.append((tool, arguments, kwargs))
        prefix = Path(arguments[arguments.index('--output-file') + 1])
        prefix.with_suffix('.json').write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        return b''
    monkeypatch.setattr(asr, '_run_process', run)
    return calls


def test_asr_does_not_use_description(tmp_path, monkeypatch):
    media = engine(tmp_path, tool=tmp_path / 'backend-whisper-cli')
    calls = output_runner(monkeypatch, document())
    audio = wav(tmp_path)
    first = media.transcribe(audio, model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    second = media.transcribe(audio, model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert first == second and first[0].text == '中文识别结果'
    assert first[0].start_ms == 0 and first[0].end_ms == 1000
    assert first[0].id == second[0].id and len(first[0].id) == 64
    assert media.origin == 'asr' and media.models.calls == [MODEL, MODEL]
    args = calls[0][1]
    assert isinstance(args, tuple) and '--no-gpu' in args and '--output-json-full' in args
    assert '--prompt' not in args and '--translate' not in args
    assert args[args.index('--model') + 1] == str(media.models.path)
    assert args[args.index('--file') + 1] == str(audio)
    assert not any(word in str(args) for word in ('title', 'description', '中文识别结果'))
    assert list((tmp_path / 'scratch').iterdir()) == []
    with pytest.raises(TypeError):
        media.transcribe(audio, model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None, description='不可当字幕')


@pytest.mark.parametrize('language', ['auto', 'zh', 'en'])
def test_finite_languages_are_forwarded_without_translation(tmp_path, monkeypatch, language):
    media = engine(tmp_path)
    payload = document('test')
    payload['params']['language'] = language
    calls = output_runner(monkeypatch, payload)
    events = []
    media.transcribe(wav(tmp_path), model_id=MODEL, language=language, cancel=NeverCancel(), progress=events.append)
    assert calls[0][1][calls[0][1].index('--language') + 1] == language
    assert all(event.stage == 'asr' and 0 <= event.current <= event.total for event in events)


@pytest.mark.parametrize('payload', [b'{broken', b'\xff', b'[]', b'{"model":{"type":"small","multilingual":true},"transcription":NaN}', b'[' * 10000 + b'0' + b']' * 10000], ids=['broken', 'invalid_utf8', 'wrong_root', 'nan', 'deep_nesting'])
def test_corrupt_output_has_finite_error_and_cleans_json(tmp_path, monkeypatch, payload):
    media = engine(tmp_path)
    output_runner(monkeypatch, payload)
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'subtitle_invalid'
    assert list((tmp_path / 'scratch').iterdir()) == []


@pytest.mark.parametrize('change', [
    ('start', -1), ('start', True), ('end', 0), ('end', 100000), ('end', float('inf')),
    ('timestamp', '00:99:00,000'), ('timestamp', '00:00:00,999'),
    ('probability', -1), ('probability', float('nan')), ('text', '\ud800'),
    ('model', False), ('translation', True),
])
def test_invalid_times_utf8_probabilities_and_wrong_model_are_rejected(tmp_path, monkeypatch, change):
    media = engine(tmp_path)
    payload = document()
    field, value = change
    row = payload['transcription'][0]
    if field in ('start', 'end'):
        row['offsets']['from' if field == 'start' else 'to'] = value
    elif field == 'timestamp':
        row['timestamps']['from'] = value
    elif field == 'probability':
        row['tokens'][0]['p'] = value
    elif field == 'text':
        row['text'] = value
    elif field == 'model':
        payload['model']['multilingual'] = value
    elif field == 'translation':
        payload['params']['translate'] = value
    # ensure_ascii preserves an intentionally invalid Unicode escape for parser testing
    output_runner(monkeypatch, json.dumps(payload, ensure_ascii=True).encode())
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'subtitle_invalid'


def test_overlapping_segments_are_rejected(tmp_path, monkeypatch):
    media = engine(tmp_path)
    payload = document()
    payload['transcription'].append(copy.deepcopy(payload['transcription'][0]))
    output_runner(monkeypatch, payload)
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'subtitle_invalid'


@pytest.mark.parametrize('case', ['silence', 'music_marker', 'empty', 'low_evidence'])
def test_no_usable_speech_is_finite_without_inventing_subtitles(tmp_path, monkeypatch, case):
    media = engine(tmp_path)
    payload = document('[Music]' if case == 'music_marker' else '幻觉文本', probability=.01 if case == 'low_evidence' else .9)
    if case == 'empty':
        payload['transcription'] = []
    output_runner(monkeypatch, payload)
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path, silence=case == 'silence'), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'no_speech'


@pytest.mark.parametrize('language', ['fr', '--prompt', '', None])
def test_invalid_language_rejects_before_model_or_tool(tmp_path, monkeypatch, language):
    media = engine(tmp_path)
    calls = output_runner(monkeypatch, document())
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path), model_id=MODEL, language=language, cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'input_limit' and not media.models.calls and not calls


def test_missing_model_does_not_download_or_run_a_tool(tmp_path, monkeypatch):
    media = engine(tmp_path)
    def missing(_):
        raise DownloadError('model_required')
    media.models.require = missing
    calls = output_runner(monkeypatch, document())
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'model_required' and not calls


@pytest.mark.parametrize('model_id', ['ggml-small.en', 'rapidocr-det-ppocrv6-small', 'unknown'])
def test_only_explicit_multilingual_small_model_is_supported(tmp_path, monkeypatch, model_id):
    media = engine(tmp_path)
    calls = output_runner(monkeypatch, document())
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path), model_id=model_id, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'model_unknown' and not media.models.calls and not calls


@pytest.mark.parametrize('rate,channels', [(8000, 1), (16000, 2)])
def test_audio_must_be_owned_stage_pcm_mono_sixteen_khz(tmp_path, monkeypatch, rate, channels):
    media = engine(tmp_path)
    calls = output_runner(monkeypatch, document())
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path, rate=rate, channels=channels), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'media_invalid' and not calls


def test_audio_is_preserved_and_change_during_recognition_is_rejected(tmp_path, monkeypatch):
    media = engine(tmp_path)
    audio = wav(tmp_path)
    calls = output_runner(monkeypatch, document())
    from yijianjifa.features.downloads import asr
    normal_run = asr._run_process
    def mutate(*args, **kwargs):
        result = normal_run(*args, **kwargs)
        audio.write_bytes(audio.read_bytes() + b'changed')
        return result
    monkeypatch.setattr(asr, '_run_process', mutate)
    with pytest.raises(DownloadError) as failure:
        media.transcribe(audio, model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'identity_mismatch' and calls and audio.exists()


def test_tool_json_size_is_bounded(tmp_path, monkeypatch):
    media = engine(tmp_path)
    from yijianjifa.features.downloads import asr
    monkeypatch.setattr(asr, 'MAX_JSON_BYTES', 256)
    output_runner(monkeypatch, document('大' * 300))
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'subtitle_invalid'


def test_small_native_end_padding_is_bounded_to_audio_end(tmp_path, monkeypatch):
    media = engine(tmp_path)
    payload = document(end=1160)
    payload['transcription'][0]['timestamps']['to'] = '00:00:01,160'
    output_runner(monkeypatch, payload)
    rows = media.transcribe(wav(tmp_path), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert rows[0].end_ms == 1000 and rows[0].text == '中文识别结果'


def test_accepted_document_keeps_low_confidence_words_for_human_edits(tmp_path, monkeypatch):
    media = engine(tmp_path)
    payload = document('高证据', end=500, probability=.95)
    payload['transcription'][0]['timestamps']['to'] = '00:00:00,500'
    second = document('待核对', probability=.3)['transcription'][0]
    second['offsets']['from'] = 500
    second['timestamps']['from'] = '00:00:00,500'
    payload['transcription'].append(second)
    output_runner(monkeypatch, payload)
    rows = media.transcribe(wav(tmp_path), model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert [row.text for row in rows] == ['高证据', '待核对']


def test_cancelled_before_model_read(tmp_path):
    media = engine(tmp_path)
    class Cancelled:
        def cancelled(self):
            return True
    with pytest.raises(DownloadError) as failure:
        media.transcribe(wav(tmp_path), model_id=MODEL, language='zh', cancel=Cancelled(), progress=lambda _: None)
    assert failure.value.code == 'cancelled' and not media.models.calls


def test_default_tool_resolution_never_falls_back_to_path_or_preparation(tmp_path, monkeypatch):
    media = engine(tmp_path)
    from yijianjifa.features.downloads import asr
    monkeypatch.setattr(asr.sys, '_MEIPASS', str(tmp_path / 'bundle'), raising=False)
    resolved = asr._whisper_tool()
    assert resolved == tmp_path / 'bundle' / 'bin' / ('whisper-cli.exe' if sys.platform.startswith('win') else 'whisper-cli')


@pytest.mark.skipif(sys.platform.startswith('win'), reason='POSIX fault executable; target Windows runner tests are in Task5')
def test_running_asr_timeout_terminates_and_cleans_only_own_scratch(tmp_path):
    media = engine(tmp_path, timeout=2, terminate_grace=.05)
    audio = wav(tmp_path)
    script = tmp_path / 'blocking-asr'
    marker = tmp_path / 'started'
    with script.open('w') as executable:
        executable.write('#!' + sys.executable + '\nimport pathlib,signal,sys,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\npathlib.Path(' + repr(str(marker)) + ').touch()\nprefix=sys.argv[sys.argv.index("--output-file")+1]\npathlib.Path(prefix+".json").write_text("partial")\ntime.sleep(60)\n')
        executable.flush()
        os.fsync(executable.fileno())
    script.chmod(0o700)
    media.tool = script
    with pytest.raises(DownloadError) as failure:
        media.transcribe(audio, model_id=MODEL, language='zh', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'timeout'
    assert marker.exists(), 'fault executable did not start'
    assert audio.exists() and list((tmp_path / 'scratch').iterdir()) == []
