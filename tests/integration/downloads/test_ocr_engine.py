"""Actual CPU OCR of encoded subtitles, confined to isolated developer fixtures.

Missing optional engines/models/fonts cause explicit skips, never acceptance.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from fixtures import NeverCancel, digest, make_video, probe
from yijianjifa.features.downloads.contracts import DownloadError, Region
from yijianjifa.features.downloads.ocr import DEFAULT_OCR_MODEL_IDS, OcrEngine
from yijianjifa.features.downloads.models import ModelStore
from yijianjifa.os_integration.resources import ffmpeg_path

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / 'build/download-probe/task-2-bounded-native/7686432680413629742.mp4'
CONTROLLED_CLIP = ROOT / 'build/download-client-acceptance/clip.mp4'
CONTROLLED_CLIP_SHA256 = 'd411dfed12d6c486c91f050a3aa13616fb3592fa0e117d1bd3ed280de437244e'


@pytest.fixture
def actual_engine():
    if importlib.util.find_spec('rapidocr') is None or importlib.util.find_spec('onnxruntime') is None:
        pytest.skip('optional recognition runtime absent: not real-engine acceptance')
    model_root = Path(os.getenv('YJJF_OCR_MODEL_DIR', str(ROOT / 'build/recognition-preparation/assets')))
    store = ModelStore(model_root)
    try:
        for model_id in DEFAULT_OCR_MODEL_IDS:
            store.require(model_id)
    except DownloadError:
        pytest.skip('explicit verified OCR models absent: not real-engine acceptance')
    return OcrEngine(store, timeout=120)


def burned_video(root, *, portrait=False):
    from PIL import Image, ImageDraw, ImageFont
    font_file = Path(os.getenv('YJJF_OCR_TEST_FONT', '/System/Library/Fonts/STHeiti Medium.ttc'))
    if not font_file.is_file():
        pytest.skip('local CJK test font absent; no font file is distributed')
    root.mkdir(parents=True, exist_ok=True)
    width, height = (480, 800) if portrait else (800, 450)
    font = ImageFont.truetype(str(font_file), 40)
    top_font = ImageFont.truetype(str(font_file), 28)
    labels = ['', '中文识别测试', 'Hello world 123', 'short']
    images = {}
    for text in labels:
        image = Image.new('RGB', (width, height), '#16364a')
        draw = ImageDraw.Draw(image)
        draw.text((20, 30), 'TOP NICKNAME', font=top_font, fill='white')
        if text:
            box = draw.textbbox((0, 0), text, font=font)
            draw.text(((width - box[2]) / 2, height * .78), text, font=font, fill='white',
                      stroke_width=1, stroke_fill='black')
        images[text] = image.tobytes()
    output = root / ('portrait-burned.mp4' if portrait else 'landscape-burned.mp4')
    args = [str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
            '-s', f'{width}x{height}', '-r', '25', '-i', 'pipe:0', '-an', '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p', str(output)]
    child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for frame in range(80):
            at = frame * 40
            text = ('中文识别测试' if 120 <= at < 800 or 1520 <= at < 2120 else
                    'Hello world 123' if 800 <= at < 1520 else 'short' if 2680 <= at < 2880 else '')
            child.stdin.write(images[text])
        child.stdin.close()
        assert child.wait(timeout=30) == 0, child.stderr.read().decode('utf-8', 'replace')
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stderr.close()
    return output


def evidence(name, video, rows, engine, **extra):
    folder = os.getenv('YJJF_OCR_EVIDENCE_DIR')
    if folder:
        root = Path(folder)
        root.mkdir(parents=True, exist_ok=True)
        target = root / (name + '.mp4')
        shutil.copyfile(video, target)
        report = {'source': str(target), 'sha256': digest(video), 'probe': probe(video),
                  'segments': [vars(row) for row in rows], 'diagnostics': engine.last_diagnostics, **extra}
        (root / (name + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


@pytest.mark.parametrize('portrait', [False, True])
def test_actual_chinese_english_burned_subtitles_and_default_region(actual_engine, tmp_path, portrait):
    video = burned_video(tmp_path, portrait=portrait)
    before = digest(video)
    progress = []
    rows = actual_engine.recognize(video, region=Region(), model_ids=DEFAULT_OCR_MODEL_IDS,
                                   cancel=NeverCancel(), progress=progress.append)
    expected = [(120, 800, '中文识别测试'), (800, 1520, 'Hello world 123'), (1520, 2120, '中文识别测试')]
    assert [r.text for r in rows] == [r[2] for r in expected]
    for actual, (start, end, _) in zip(rows, expected):
        assert abs(actual.start_ms - start) <= 150 and abs(actual.end_ms - end) <= 150
        assert actual.confidence is not None and 0 <= actual.confidence <= 1
    assert all('TOP' not in r.text and 'short' not in r.text for r in rows)
    assert actual_engine.last_diagnostics['providers'] == ['CPUExecutionProvider']
    assert actual_engine.last_diagnostics['engine_load_count'] == 1
    assert actual_engine.last_diagnostics['network_attempt_count'] == 0
    assert actual_engine.last_diagnostics['max_buffer_frames'] <= 5
    assert actual_engine.last_diagnostics['decoded_frames'] == 32
    assert progress[-1].current == progress[-1].total == 3200
    assert digest(video) == before
    evidence('burned-portrait' if portrait else 'burned-landscape', video, rows, actual_engine,
             expected=expected, known_missed_short_subtitle=[2680, 2880, 'short'],
             font='local system font used without copying/distributing font bytes')


def test_actual_full_frame_selection_includes_top_nickname(actual_engine, tmp_path):
    video = burned_video(tmp_path)
    rows = actual_engine.recognize(video, region=Region(0, 0, 1, 1), model_ids=DEFAULT_OCR_MODEL_IDS,
                                   cancel=NeverCancel(), progress=lambda _: None)
    assert any('TOP NICKNAME' in r.text for r in rows)
    assert any('中文识别测试' in r.text for r in rows)
    evidence('burned-full-frame', video, rows, actual_engine)


def test_actual_blank_video_returns_no_text_without_removing_video(actual_engine, tmp_path):
    video = make_video(tmp_path, audio=False)
    original = digest(video)
    with pytest.raises(DownloadError) as error:
        actual_engine.recognize(video, region=Region(), model_ids=DEFAULT_OCR_MODEL_IDS,
                                cancel=NeverCancel(), progress=lambda _: None)
    assert error.value.code == 'no_text' and digest(video) == original


def test_actual_one_frame_clip_has_a_valid_positive_timeline(actual_engine, tmp_path):
    original = burned_video(tmp_path)
    video = tmp_path / 'one-frame.mp4'
    subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y', '-ss', '0.4', '-i', str(original),
                    '-t', '0.04', '-an', '-c:v', 'libx264', str(video)], check=True, capture_output=True, timeout=20)
    rows = actual_engine.recognize(video, region=Region(), model_ids=DEFAULT_OCR_MODEL_IDS,
                                   cancel=NeverCancel(), progress=lambda _: None)
    assert [(r.start_ms, r.end_ms, r.text) for r in rows] == [(0, 40, '中文识别测试')]
    assert actual_engine.last_diagnostics['decoded_frames'] == 1
    assert actual_engine.last_diagnostics['max_buffer_frames'] == 1


@pytest.mark.skipif(os.name == 'nt', reason='POSIX group receipt; native Windows Job execution remains pending')
@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
def test_actual_running_ocr_is_killable_and_leaves_no_frames(actual_engine, tmp_path, monkeypatch, stop):
    from yijianjifa.features.downloads import media
    video = burned_video(tmp_path)
    original = digest(video)
    started = []
    popen = media.subprocess.Popen
    def launch(*args, **kwargs):
        child = popen(*args, **kwargs)
        started.append(child)
        return child
    monkeypatch.setattr(media.subprocess, 'Popen', launch)
    class Cancel:
        value = False
        def cancelled(self):
            return self.value
    cancel = Cancel()
    actual_engine.timeout = .15 if stop == 'timeout' else 120
    def progress(event):
        if event.current >= 500:
            cancel.value = True
    with pytest.raises(DownloadError) as error:
        actual_engine.recognize(video, region=Region(), model_ids=DEFAULT_OCR_MODEL_IDS,
                                cancel=cancel, progress=progress)
    assert error.value.code == ('timeout' if stop == 'timeout' else 'cancelled')
    assert started and all(child.poll() is not None for child in started)
    for child in started:
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, 0)
    assert sorted(p.name for p in tmp_path.iterdir()) == [video.name]
    assert digest(video) == original


def test_actual_authorized_local_subtitle_sample(actual_engine):
    if not SAMPLE.is_file():
        pytest.skip('authorized local sample absent; do not fetch a replacement')
    original = digest(SAMPLE)
    actual_engine.timeout = 600
    started = time.monotonic()
    rows = actual_engine.recognize(SAMPLE, region=Region(), model_ids=DEFAULT_OCR_MODEL_IDS,
                                   cancel=NeverCancel(), progress=lambda _: None)
    assert rows and all(r.text.strip() and r.end_ms > r.start_ms >= 0 for r in rows)
    assert actual_engine.last_diagnostics['network_attempt_count'] == 0
    assert actual_engine.last_diagnostics['engine_load_count'] == 1
    assert digest(SAMPLE) == original
    evidence('authorized-local-sample', SAMPLE, rows, actual_engine, elapsed_seconds=time.monotonic() - started,
             accuracy='actual output only; manually inspected excerpt errors are recorded in Task8 report')


def delayed_blue_video(root, *, offset='0.5', length='1.4', audio_duration='1.9'):
    output = root / 'delayed-blue.mp4'
    subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y', '-itsoffset', offset,
                    '-f', 'lavfi', '-i', f'color=c=blue:s=160x96:r=25:d={length}',
                    '-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:duration={audio_duration}',
                    '-c:v', 'libx264', '-fps_mode', 'passthrough', '-c:a', 'aac', str(output)],
                   check=True, capture_output=True, timeout=20)
    return output


def test_offset_probe_preserves_container_playback_duration(tmp_path):
    video = delayed_blue_video(tmp_path)
    engine = OcrEngine(object())
    duration = engine._probe(video, NeverCancel(), time.monotonic() + 15)[0]
    metadata = probe(video)
    assert abs(float(metadata['streams'][0]['start_time']) - .5) <= .03
    assert duration == round(float(metadata['format']['duration']) * 1000)
    assert 1900 <= duration <= 1930


def test_offset_decoder_never_copies_picture_before_video_start(tmp_path):
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    from yijianjifa.features.downloads.ocr_worker import _frames
    video = delayed_blue_video(tmp_path)
    metadata = probe(video)
    selected = next(row for row in metadata['streams'] if row['codec_type'] == 'video')
    duration = round(float(metadata['format']['duration']) * 1000)
    start = round(float(selected['start_time']) * 1000)
    end = round((float(selected['start_time']) + float(selected['duration'])) * 1000)
    first = ((start + 99) // 100) * 100
    payload = {'video': str(video), 'ffmpeg': str(ffmpeg_path().resolve()), 'crop': [0, 0, 160, 96],
               'size': [160, 96], 'stream': selected['index'], 'duration_ms': duration,
               'start_ms': start, 'end_ms': end}
    diagnostics = {'decoded_frames': 0, 'max_buffer_frames': 0}
    frames = list(_frames(payload, diagnostics))
    assert [at for at, _ in frames] == list(range(first, end, 100))
    assert all(at >= start for at, _ in frames)
    rows = _scan_timeline(iter(frames), duration, lambda _: _Text('real picture', .9), lambda _: None,
                          start_ms=first, end_ms=end)
    assert [(r.start_ms, r.end_ms) for r in rows] == [(first, end)]


def test_offset_short_clip_between_grid_ticks_keeps_its_real_first_frame(tmp_path):
    from yijianjifa.features.downloads.ocr import _Text, _scan_timeline
    from yijianjifa.features.downloads.ocr_worker import _frames
    video = delayed_blue_video(tmp_path, offset='0.53', length='0.04', audio_duration='0.57')
    metadata = probe(video)
    selected = next(row for row in metadata['streams'] if row['codec_type'] == 'video')
    duration = round(float(metadata['format']['duration']) * 1000)
    start = round(float(selected['start_time']) * 1000)
    end = round((float(selected['start_time']) + float(selected['duration'])) * 1000)
    payload = {'video': str(video), 'ffmpeg': str(ffmpeg_path().resolve()), 'crop': [0, 0, 160, 96],
               'size': [160, 96], 'stream': selected['index'], 'duration_ms': duration,
               'start_ms': start, 'end_ms': end}
    diagnostics = {'decoded_frames': 0, 'max_buffer_frames': 0}
    frames = list(_frames(payload, diagnostics))
    assert [at for at, _ in frames] == [start]
    rows = _scan_timeline(iter(frames), duration, lambda _: _Text('single', .9), lambda _: None,
                          start_ms=start, end_ms=end)
    assert [(r.start_ms, r.end_ms) for r in rows] == [(start, end)]


def test_actual_offset_chinese_english_subtitles_keep_player_timestamps(actual_engine, tmp_path):
    original = burned_video(tmp_path)
    video = tmp_path / 'delayed-burned.mp4'
    subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y', '-itsoffset', '0.5', '-i', str(original),
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=3.7',
                    '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', str(video)],
                   check=True, capture_output=True, timeout=20)
    before = digest(video)
    progress = []
    rows = actual_engine.recognize(video, region=Region(), model_ids=DEFAULT_OCR_MODEL_IDS,
                                   cancel=NeverCancel(), progress=progress.append)
    expected = [(620, 1300, '中文识别测试'), (1300, 2020, 'Hello world 123'), (2020, 2620, '中文识别测试')]
    assert [r.text for r in rows] == [r[2] for r in expected]
    assert all(r.start_ms >= 500 for r in rows)
    for actual, (start, end, _) in zip(rows, expected):
        assert abs(actual.start_ms - start) <= 100 and abs(actual.end_ms - end) <= 100
    assert progress[-1].current == progress[-1].total == 3700
    assert actual_engine.last_diagnostics['engine_load_count'] == 1
    assert actual_engine.last_diagnostics['network_attempt_count'] == 0
    assert actual_engine.last_diagnostics['providers'] == ['CPUExecutionProvider']
    assert digest(video) == before
    evidence('fix1-offset-burned', video, rows, actual_engine, expected=expected,
             no_picture_interval_ms=[0, 500], subtitle_times='container/player timeline, not reset per video stream')


def controlled_clip():
    if not CONTROLLED_CLIP.is_file():
        pytest.skip('authorized controlled local clip absent; do not fetch a replacement')
    assert digest(CONTROLLED_CLIP) == CONTROLLED_CLIP_SHA256
    return CONTROLLED_CLIP


def test_controlled_clip_decoder_stops_at_metadata_window_despite_late_source_pts():
    from yijianjifa.features.downloads.ocr import _Text, _crop_box, _scan_timeline
    from yijianjifa.features.downloads.ocr_worker import _frames
    video = controlled_clip()
    engine = OcrEngine(object())
    duration, stream, width, height, start, end = engine._probe(video, NeverCancel(), time.monotonic() + 15)
    assert (duration, start, end) == (30067, 0, 30067)
    crop = _crop_box(width, height, Region())
    payload = {'video': str(video), 'ffmpeg': str(engine.ffmpeg), 'crop': crop, 'size': list(crop[2:]),
               'stream': stream, 'duration_ms': duration, 'start_ms': start, 'end_ms': end}
    diagnostics = {'decoded_frames': 0, 'max_buffer_frames': 0}
    frames = _frames(payload, diagnostics)
    try:
        rows = _scan_timeline(frames, duration, lambda _: _Text('sentinel', .9), lambda _: None,
                              start_ms=start, end_ms=end)
    finally:
        frames.close()
    assert [(row.start_ms, row.end_ms) for row in rows] == [(0, 30067)]
    assert diagnostics['decoded_frames'] == 301
    assert digest(video) == CONTROLLED_CLIP_SHA256


def test_actual_controlled_clip_ocr_keeps_container_window(actual_engine):
    video = controlled_clip()
    started = time.monotonic()
    progress = []
    rows = actual_engine.recognize(video, region=Region(), model_ids=DEFAULT_OCR_MODEL_IDS,
                                   cancel=NeverCancel(), progress=progress.append)
    assert rows and all(0 <= row.start_ms < row.end_ms <= 30067 and row.text.strip() for row in rows)
    assert progress[-1].current == progress[-1].total == 30067
    assert actual_engine.last_diagnostics['decoded_frames'] == 301
    assert actual_engine.last_diagnostics['engine_load_count'] == 1
    assert actual_engine.last_diagnostics['network_attempt_count'] == 0
    assert actual_engine.last_diagnostics['providers'] == ['CPUExecutionProvider']
    assert digest(video) == CONTROLLED_CLIP_SHA256
    evidence('fix2-controlled-clip', video, rows, actual_engine, elapsed_seconds=time.monotonic() - started,
             accuracy='actual output only; no complete human truth or overall accuracy claimed',
             boundary='metadata video end 30067ms; no subtitle from later decoded source PTS')
