import pytest
import json
from pathlib import Path
import sys

from yijianjifa.features.downloads.contracts import DownloadError, Region


def test_ocr_retains_nonadjacent_repeats():
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    frames = ((time, 'A' if time < 700 or time >= 1400 else 'B') for time in range(0, 2200, 100))
    rows = _scan_timeline(frames, 2200, lambda text: _Text(text, .9), lambda _: None)
    assert [(row.start_ms, row.end_ms, row.text) for row in rows] == [
        (0, 700, 'A'), (700, 1400, 'B'), (1400, 2200, 'A')]


@pytest.mark.parametrize('size,region,expected', [
    ((320, 180), Region(), (0, 108, 320, 72)),
    ((360, 640), Region(), (0, 384, 360, 256)),
    ((320, 180), Region(0, 0, 1, 1), (0, 0, 320, 180)),
    ((321, 181), Region(.1, .2, .5, .5), (32, 36, 161, 91)),
])
def test_region_is_normalized_per_display_size(size, region, expected):
    from yijianjifa.features.downloads.ocr import _crop_box
    assert _crop_box(*size, region) == expected


@pytest.mark.parametrize('dimensions', [(0, 180), (320, -1), (20000, 100), (True, 10)])
def test_region_rejects_invalid_frame_dimensions(dimensions):
    from yijianjifa.features.downloads.ocr import _crop_box
    with pytest.raises(DownloadError) as error:
        _crop_box(*dimensions, Region())
    assert error.value.code == 'media_invalid'


def test_ocr_merges_only_adjacent_text_and_retains_low_confidence():
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    frames = ((time, time) for time in range(0, 1900, 100))
    def infer(time):
        return _Text(' hello  world ' if time < 700 or time >= 1300 else '', .12 if time == 500 else .95)
    rows = _scan_timeline(frames, 1900, infer, lambda _: None)
    assert [(row.start_ms, row.end_ms, row.text) for row in rows] == [
        (0, 700, 'hello world'), (1300, 1900, 'hello world')]
    assert rows[0].confidence == .12
    assert all(row.end_ms > row.start_ms for row in rows)


def test_base_is_500ms_and_changed_interval_refines_at_100ms():
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    seen, progress = [], []
    def infer(time):
        seen.append(time)
        return _Text('A' if time < 700 else 'B', .8)
    rows = _scan_timeline(((t, t) for t in range(0, 1700, 100)), 1700, infer, progress.append)
    assert [(r.start_ms, r.end_ms) for r in rows] == [(0, 700), (700, 1700)]
    assert set(seen) == {0, 500, 600, 700, 800, 900, 1000, 1500, 1600}
    assert len(seen) == len(set(seen))
    assert all(p.stage == 'ocr' and 0 <= p.current <= p.total == 1700 for p in progress)
    assert [p.current for p in progress] == sorted(p.current for p in progress)


def test_sub500ms_text_between_equal_base_samples_can_be_missed():
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    # Explicitly preserve this algorithm's limitation; do not report 100ms accuracy.
    rows = _scan_timeline(((t, t) for t in range(0, 1000, 100)), 1000,
                          lambda t: _Text('short' if 200 <= t < 400 else 'A', .9), lambda _: None)
    assert [r.text for r in rows] == ['A']


def test_blank_video_is_finite_no_text():
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    with pytest.raises(DownloadError) as error:
        _scan_timeline(((t, None) for t in range(0, 1300, 100)), 1300,
                       lambda _: _Text(' \n ', None), lambda _: None)
    assert error.value.code == 'no_text'


@pytest.mark.parametrize('frames', [[], [(100, 'A')], [(0, 'A'), (0, 'B')], [(0, 'A'), (200, 'B')]])
def test_incomplete_or_out_of_order_frame_stream_is_invalid(frames):
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    with pytest.raises(DownloadError) as error:
        _scan_timeline(iter(frames), 1000, lambda x: _Text(x, .9), lambda _: None)
    assert error.value.code == 'media_invalid'


@pytest.mark.parametrize('score', [-.01, 1.01, float('nan'), True])
def test_invalid_recognition_confidence_is_finite(score):
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    with pytest.raises(DownloadError) as error:
        _scan_timeline(((t, None) for t in range(0, 500, 100)), 500,
                       lambda _: _Text('A', score), lambda _: None)
    assert error.value.code == 'ocr_failed'


class NeverCancel:
    def cancelled(self):
        return False


class Models:
    def __init__(self, root, error=None):
        self.root, self.error, self.calls = root, error, []
    def require(self, model_id):
        self.calls.append(model_id)
        if self.error:
            raise DownloadError(self.error)
        return self.root / (model_id + '.onnx')


def prepare(tmp_path, monkeypatch, *, result=None, model_error=None):
    from yijianjifa.features.downloads import ocr
    video = tmp_path / 'private-source.mp4'
    video.write_bytes(b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2')
    models = Models(tmp_path, model_error)
    engine = ocr.OcrEngine(models, ffprobe=tmp_path / 'ffprobe', ffmpeg=tmp_path / 'ffmpeg')
    calls = []
    packet = result or {'type': 'result', 'origin': 'ocr', 'duration_ms': 1500,
                       'segments': [{'start_ms': 100, 'end_ms': 1200, 'text': 'hello', 'confidence': .3}],
                       'diagnostics': {'engine_load_count': 1, 'providers': ['CPUExecutionProvider']}}
    def run(tool, arguments, **kwargs):
        calls.append((tool, arguments, kwargs))
        if tool.name == 'ffprobe':
            return json.dumps({'format': {'duration': '1.5'}, 'streams': [
                {'index': 0, 'codec_type': 'video', 'width': 320, 'height': 180, 'duration': '1.5'}]}).encode()
        line = json.dumps({'type': 'progress', 'current': 1000, 'total': 1500}).encode() + b'\n'
        kwargs['on_line'](line)
        return line + json.dumps(packet).encode() + b'\n'
    monkeypatch.setattr(ocr, '_run_process', run)
    return ocr, engine, models, video, calls


def test_engine_requires_three_explicit_models_and_one_worker(tmp_path, monkeypatch):
    ocr, engine, models, video, calls = prepare(tmp_path, monkeypatch)
    assert not models.calls and not calls
    progress = []
    rows = engine.recognize(video, region=Region(), model_ids=ocr.DEFAULT_OCR_MODEL_IDS,
                            cancel=NeverCancel(), progress=progress.append)
    assert models.calls == list(ocr.DEFAULT_OCR_MODEL_IDS)
    assert len(calls) == 2 and calls[0][0].name == 'ffprobe'
    command = calls[1][1]
    payload = json.loads(command[-1])
    assert set(payload['models']) == {'Det', 'Cls', 'Rec'}
    assert all(str(tmp_path) in path for path in payload['models'].values())
    assert payload['crop'] == [0, 108, 320, 72]
    assert rows[0].confidence == .3 and rows[0].text == 'hello'
    assert engine.origin == 'ocr' and len({r.id for r in rows}) == len(rows)
    assert progress[-1].stage == 'ocr' and progress[-1].current == progress[-1].total == 1500
    assert engine.last_diagnostics['engine_load_count'] == 1


def test_missing_model_pauses_only_ocr_and_never_spawns(tmp_path, monkeypatch):
    ocr, engine, models, video, calls = prepare(tmp_path, monkeypatch, model_error='model_required')
    original = video.read_bytes()
    with pytest.raises(DownloadError) as error:
        engine.recognize(video, region=Region(), model_ids=ocr.DEFAULT_OCR_MODEL_IDS,
                         cancel=NeverCancel(), progress=lambda _: None)
    assert error.value.code == 'model_required' and calls == []
    assert video.read_bytes() == original


@pytest.mark.parametrize('model_ids,code', [((), 'model_required'), (('wrong',), 'model_unknown'),
                                         (('wrong', 'wrong', 'wrong'), 'input_limit')])
def test_model_selection_is_explicit_and_bounded(tmp_path, monkeypatch, model_ids, code):
    _, engine, _, video, calls = prepare(tmp_path, monkeypatch)
    with pytest.raises(DownloadError) as error:
        engine.recognize(video, region=Region(), model_ids=model_ids, cancel=NeverCancel(), progress=lambda _: None)
    assert error.value.code == code and not calls


@pytest.mark.parametrize('segments', [[], [{'start_ms': 100, 'end_ms': 200, 'text': ' ', 'confidence': .9}],
                                     [{'start_ms': 100, 'end_ms': 2000, 'text': 'A', 'confidence': .9}],
                                     [{'start_ms': 100, 'end_ms': 200, 'text': 'A', 'confidence': True}]])
def test_malformed_or_empty_worker_result_is_not_success(tmp_path, monkeypatch, segments):
    packet = {'type': 'result', 'origin': 'ocr', 'duration_ms': 1500, 'segments': segments, 'diagnostics': {}}
    ocr, engine, _, video, _ = prepare(tmp_path, monkeypatch, result=packet)
    with pytest.raises(DownloadError) as error:
        engine.recognize(video, region=Region(), model_ids=ocr.DEFAULT_OCR_MODEL_IDS,
                         cancel=NeverCancel(), progress=lambda _: None)
    assert error.value.code == ('no_text' if not segments else 'ocr_failed')


def test_frozen_worker_command_uses_current_executable_early_dispatch(tmp_path, monkeypatch):
    from yijianjifa.features.downloads.ocr import _worker_command
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    tool, args = _worker_command({'video': 'backend-only'}, None)
    assert tool == Path(sys.executable).absolute()
    assert args[0] == '--yjjf-ocr-worker' and '-m' not in args
    with pytest.raises(DownloadError) as error:
        _worker_command({}, tmp_path / 'system-python')
    assert error.value.code == 'ocr_failed'


def test_worker_failure_packet_never_exposes_private_details(tmp_path, monkeypatch):
    ocr, engine, _, video, _ = prepare(tmp_path, monkeypatch, result={
        'type': 'error', 'code': 'SECRET private/path'})
    with pytest.raises(DownloadError) as error:
        engine.recognize(video, region=Region(), model_ids=ocr.DEFAULT_OCR_MODEL_IDS,
                         cancel=NeverCancel(), progress=lambda _: None)
    assert error.value.code == 'ocr_failed' and 'SECRET' not in str(error.value)


@pytest.mark.parametrize('missing,code', [('video', 'source_missing'), ('ffmpeg', 'media_tool_missing'),
                                        ('model', 'model_required')])
def test_worker_missing_local_dependency_is_finite(tmp_path, missing, code):
    from yijianjifa.features.downloads.ocr_worker import _payload
    video, tool, model = [tmp_path / name for name in ('private-video', 'private-tool', 'private-model')]
    for path in (video, tool, model):
        path.write_bytes(b'local')
    {'video': video, 'ffmpeg': tool, 'model': model}[missing].unlink()
    payload = {'video': str(video), 'ffmpeg': str(tool), 'models': dict.fromkeys(('Det', 'Cls', 'Rec'), str(model)),
               'duration_ms': 1000, 'stream': 0, 'crop': [0, 0, 100, 100], 'size': [100, 100]}
    with pytest.raises(DownloadError) as error:
        _payload(json.dumps(payload))
    assert error.value.code == code and str(tmp_path) not in str(error.value)


@pytest.mark.parametrize('providers', [['CPUExecutionProvider'], ['CUDAExecutionProvider', 'CPUExecutionProvider']])
def test_worker_requires_explicit_model_paths_and_cpu_without_fallback(monkeypatch, providers):
    from types import ModuleType, SimpleNamespace
    from yijianjifa.features.downloads import ocr_worker
    seen = []
    class Session:
        def request(self):
            raise AssertionError('not called')
    requests = ModuleType('requests')
    requests.sessions = SimpleNamespace(Session=Session)
    monkeypatch.setitem(sys.modules, 'requests', requests)
    rapidocr = ModuleType('rapidocr')
    component = SimpleNamespace(session=SimpleNamespace(session=SimpleNamespace(get_providers=lambda: providers)))
    def load(*, params):
        seen.append(params)
        return SimpleNamespace(text_det=component, text_cls=component, text_rec=component)
    rapidocr.RapidOCR = load
    monkeypatch.setitem(sys.modules, 'rapidocr', rapidocr)
    download = ModuleType('rapidocr.utils.download_file')
    download.DownloadFile = SimpleNamespace(run=lambda _: None)
    monkeypatch.setitem(sys.modules, 'rapidocr.utils.download_file', download)
    monkeypatch.setattr(ocr_worker.importlib.metadata, 'version', lambda package: '3.9.2' if package == 'rapidocr' else '1.22.1')
    # Register restoration before the dedicated worker guard replaces these APIs.
    import socket
    import urllib.request
    for target, name in [(socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                         (socket, 'create_connection'), (urllib.request, 'urlopen')]:
        monkeypatch.setattr(target, name, getattr(target, name))
    attempts = []
    models = {'Det': '/local/det.onnx', 'Cls': '/local/cls.onnx', 'Rec': '/local/rec.onnx'}
    if providers == ['CPUExecutionProvider']:
        ocr_worker._load_engine(models, attempts)
    else:
        with pytest.raises(DownloadError) as error:
            ocr_worker._load_engine(models, attempts)
        assert error.value.code == 'ocr_failed'
    assert {k: seen[0][k + '.model_path'] for k in models} == models
    assert seen[0]['Global.text_score'] == 0
    assert all(seen[0]['EngineConfig.onnxruntime.use_' + name] is False for name in ('cuda', 'dml', 'cann', 'coreml'))
    assert attempts == []
    with pytest.raises(DownloadError):
        socket.create_connection(('example.invalid', 443))
    assert attempts == [True]


def test_decoder_stays_in_parent_group_job_without_a_windows_console():
    from yijianjifa.features.downloads.ocr_worker import _decoder_spawn_options
    windows = _decoder_spawn_options('nt')
    assert windows.get('creationflags', 0) & 0x08000000  # CREATE_NO_WINDOW
    assert not windows.get('creationflags', 0) & (0x01000000 | 0x00000200 | 0x00000004)
    assert not windows.get('start_new_session', False)
    assert not _decoder_spawn_options('posix').get('start_new_session', False)


def test_offset_video_window_does_not_create_leading_or_trailing_subtitles():
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    progress = []
    rows = _scan_timeline(((at, 'visible') for at in range(500, 1500, 100)), 1900,
                          lambda text: _Text(text, .2), progress.append, start_ms=500, end_ms=1500)
    assert [(r.start_ms, r.end_ms, r.text, r.confidence) for r in rows] == [(500, 1500, 'visible', .2)]
    assert progress[-1].current == progress[-1].total == 1900


@pytest.mark.parametrize('start,end', [(-1, 1000), (True, 1000), (500, 500), (500, 2000)])
def test_offset_timeline_window_is_finite_and_inside_playback(start, end):
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    with pytest.raises(DownloadError) as error:
        _scan_timeline(iter(()), 1900, lambda _: _Text('A', .9), lambda _: None, start_ms=start, end_ms=end)
    assert error.value.code == 'media_invalid'


@pytest.mark.parametrize('changes', [
    {'format': {'duration': 'nan', 'start_time': '0'}},
    {'format': {'duration': '1.9', 'start_time': 'inf'}},
    {'start_time': '2.0'}, {'duration': '-1'}, {'start_time': 'nan'},
])
def test_offset_probe_rejects_nonfinite_or_inconsistent_metadata(tmp_path, monkeypatch, changes):
    ocr, engine, _, video, _ = prepare(tmp_path, monkeypatch)
    data = {'format': {'duration': '1.9', 'start_time': '0'}, 'streams': [
        {'index': 0, 'codec_type': 'video', 'width': 320, 'height': 180, 'duration': '1.4', 'start_time': '.5'}]}
    if 'format' in changes:
        data['format'] = changes['format']
    else:
        data['streams'][0].update(changes)
    monkeypatch.setattr(ocr, '_run_process', lambda *a, **k: json.dumps(data).encode())
    with pytest.raises(DownloadError) as error:
        engine._probe(video, NeverCancel(), float('inf'))
    assert error.value.code == 'media_invalid'


@pytest.mark.parametrize('start,end', [(0, 1000), (500, 1700)])
def test_offset_worker_result_cannot_escape_real_video_window(tmp_path, monkeypatch, start, end):
    _, engine, _, _, _ = prepare(tmp_path, monkeypatch)
    packet = {'type': 'result', 'origin': 'ocr', 'duration_ms': 1900, 'segments': [
        {'start_ms': start, 'end_ms': end, 'text': 'not outside video', 'confidence': .9}], 'diagnostics': {}}
    with pytest.raises(DownloadError) as error:
        engine._result(json.dumps(packet).encode(), 1900, start_ms=500, end_ms=1500)
    assert error.value.code == 'ocr_failed'


def test_offset_probe_uses_shared_container_origin_not_per_video_origin(tmp_path, monkeypatch):
    ocr, engine, _, video, _ = prepare(tmp_path, monkeypatch)
    data = {'format': {'duration': '1.9', 'start_time': '10.0'}, 'streams': [
        {'index': 0, 'codec_type': 'video', 'width': 320, 'height': 180, 'duration': '1.4', 'start_time': '10.5'}]}
    monkeypatch.setattr(ocr, '_run_process', lambda *a, **k: json.dumps(data).encode())
    assert engine._probe(video, NeverCancel(), float('inf')) == (1900, 0, 320, 180, 500, 1900)
