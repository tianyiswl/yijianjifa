"""Real FFmpeg/FFprobe acceptance; all files and process fixtures are isolated."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from yijianjifa.features.downloads.contracts import DownloadError
from fixtures import NeverCancel, TestOwnedFiles, digest, make_video, prefix_mp4_free_atom, probe


def processor(owner, **kwargs):
    assert importlib.util.find_spec('yijianjifa.features.downloads.media') is not None, 'MediaProcessor is missing'
    from yijianjifa.features.downloads.media import MediaProcessor
    return MediaProcessor(owner, **kwargs)


@pytest.mark.parametrize('portrait', [False, True])
def test_media_outputs_have_expected_streams(tmp_path, portrait):
    owner = TestOwnedFiles(tmp_path)
    source_path = make_video(tmp_path, portrait=portrait)
    source = owner.register(source_path)
    before = digest(source_path)
    original = probe(source_path)
    results = {}
    progress = []
    for kind in ('mp3', 'wav', 'silent', 'asr_wav'):
        output = processor(owner).run(source, kind, cancel=NeverCancel(), progress=progress.append)
        assert output.exists() and output.stat().st_size > 0
        results[kind] = probe(output)
        assert abs(float(results[kind]['format']['duration']) - float(original['format']['duration'])) <= .1
    assert digest(source_path) == before
    assert [stream['codec_type'] for stream in results['mp3']['streams']] == ['audio']
    assert results['mp3']['streams'][0]['codec_name'] == 'mp3'
    assert results['wav']['streams'][0]['codec_name'] == 'pcm_s16le'
    assert int(results['wav']['streams'][0]['sample_rate']) == 48000
    assert results['wav']['streams'][0]['channels'] == 2
    assert [stream['codec_type'] for stream in results['silent']['streams']] == ['video']
    assert results['silent']['streams'][0]['codec_name'] == original['streams'][0]['codec_name']
    for field in ('width', 'height'):
        assert results['silent']['streams'][0][field] == original['streams'][0][field]
    asr = results['asr_wav']['streams'][0]
    assert asr['codec_name'] == 'pcm_s16le' and int(asr['sample_rate']) == 16000 and asr['channels'] == 1
    assert {event.stage for event in progress} == {'audio', 'silent', 'asr'}
    assert all(0 <= event.current <= event.total for event in progress)
    assert all(path.exists() for path in owner.allocated)


@pytest.mark.parametrize('kind', ['mp3', 'wav', 'asr_wav'])
def test_no_audio_is_a_finite_stage_error_and_silent_still_works(tmp_path, kind):
    owner = TestOwnedFiles(tmp_path)
    source_path = make_video(tmp_path, audio=False)
    source = owner.register(source_path)
    media = processor(owner)
    with pytest.raises(DownloadError) as failure:
        media.run(source, kind, cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'no_audio'
    from yijianjifa.features.downloads.media import MEDIA_ERROR_MESSAGES, stage_result_for_error
    result = stage_result_for_error(kind, failure.value)
    assert result.stage == ('asr' if kind == 'asr_wav' else 'audio')
    assert result.state == 'failed' and result.resource_id is None and result.error_code == 'no_audio'
    assert MEDIA_ERROR_MESSAGES['no_audio'] == '原视频没有音轨，无法提取音频'
    assert not owner.allocated
    silent = media.run(source, 'silent', cancel=NeverCancel(), progress=lambda _: None)
    assert [row['codec_type'] for row in probe(silent)['streams']] == ['video']
    assert digest(source_path) == source.sha256


def test_incompatible_video_falls_back_to_valid_portrait_mp4(tmp_path):
    owner = TestOwnedFiles(tmp_path)
    source = owner.register(make_video(tmp_path, portrait=True, codec='vp9'))
    output = processor(owner).run(source, 'silent', cancel=NeverCancel(), progress=lambda _: None)
    result = probe(output)
    assert [row['codec_type'] for row in result['streams']] == ['video']
    assert result['streams'][0]['codec_name'] == 'h264'
    assert (result['streams'][0]['width'], result['streams'][0]['height']) == (96, 160)


def test_cancel_before_start_preserves_previous_outputs(tmp_path):
    owner = TestOwnedFiles(tmp_path)
    source = owner.register(make_video(tmp_path))
    media = processor(owner)
    finished = media.run(source, 'mp3', cancel=NeverCancel(), progress=lambda _: None)
    class Cancelled:
        def cancelled(self):
            return True
    with pytest.raises(DownloadError) as failure:
        media.run(source, 'wav', cancel=Cancelled(), progress=lambda _: None)
    assert failure.value.code == 'cancelled'
    assert finished.exists() and owner.path_for(source.id).exists()
    assert owner.allocated == [finished]


def make_failing_tool(tmp_path):
    tool = tmp_path / 'failing-ffmpeg'
    tool.write_text('#!' + sys.executable + '\nimport pathlib, sys\npathlib.Path(sys.argv[-1]).write_bytes(b"partial")\nsys.stderr.write("private/path token=SECRET")\nsys.exit(7)\n')
    tool.chmod(0o700)
    return tool


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable fixture; Windows real-tool acceptance is separate')
def test_tool_exit_cleans_only_its_temporary_output_and_redacts_stderr(tmp_path):
    owner = TestOwnedFiles(tmp_path)
    source = owner.register(make_video(tmp_path))
    finished = processor(owner).run(source, 'mp3', cancel=NeverCancel(), progress=lambda _: None)
    media = processor(owner, ffmpeg=make_failing_tool(tmp_path))
    with pytest.raises(DownloadError) as failure:
        media.run(source, 'wav', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'media_failed' and 'SECRET' not in str(failure.value)
    assert not owner.allocated[-1].exists()
    assert finished.exists() and owner.path_for(source.id).exists()


def make_blocking_tool(tmp_path, *, touch_output=True):
    """Both group members ignore TERM; the child must be killed and reaped."""
    child_pid = tmp_path / 'child.pid'
    tool_pid = tmp_path / 'tool.pid'
    marker = tmp_path / 'started'
    tool = tmp_path / 'blocking-ffmpeg'
    script = f'''#!{sys.executable}
import pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path({str(tool_pid)!r}).write_text(str(__import__('os').getpid()))
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))
{ "pathlib.Path(sys.argv[-1]).write_bytes(b'partial')" if touch_output else '' }
pathlib.Path({str(marker)!r}).touch()
while True:
    time.sleep(.05)
'''
    tool.write_text(script)
    tool.chmod(0o700)
    return tool, marker, tool_pid, child_pid


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group fixture; Windows real-tool acceptance is separate')
@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
def test_running_process_group_stops_and_temporary_output_is_cleaned(tmp_path, stop):
    owner = TestOwnedFiles(tmp_path)
    source = owner.register(make_video(tmp_path))
    finished = processor(owner).run(source, 'mp3', cancel=NeverCancel(), progress=lambda _: None)
    tool, marker, tool_pid, child_pid = make_blocking_tool(tmp_path)
    class AfterStarted:
        def cancelled(self):
            return stop == 'cancel' and marker.exists()
    # The budget also covers source probing. Allow startup so this case tests
    # stopping the running conversion group, rather than a preflight timeout.
    media = processor(owner, ffmpeg=tool, timeout=2 if stop == 'timeout' else 5, terminate_grace=.1)
    with pytest.raises(DownloadError) as failure:
        media.run(source, 'wav', cancel=AfterStarted(), progress=lambda _: None)
    assert failure.value.code == ('timeout' if stop == 'timeout' else 'cancelled')
    assert marker.exists(), 'tool never started'
    deadline = time.monotonic() + 3
    pids = [int(path.read_text()) for path in (tool_pid, child_pid)]
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not any(alive(pid) for pid in pids), 'owned tool process group is still present'
    assert not owner.allocated[-1].exists()
    assert finished.exists() and owner.path_for(source.id).exists()


def test_missing_tool_and_corrupt_media_have_finite_errors(tmp_path):
    owner = TestOwnedFiles(tmp_path)
    source = owner.register(make_video(tmp_path))
    with pytest.raises(DownloadError) as failure:
        processor(owner, ffmpeg=tmp_path / 'absent').run(source, 'wav', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'media_tool_missing'
    assert not any(path.exists() for path in owner.allocated)
    bad_path = tmp_path / 'corrupt.mp4'
    bad_path.write_bytes(b'not media')
    corrupted = owner.register(bad_path, identity='corrupted')
    with pytest.raises(DownloadError) as failure:
        processor(owner).run(corrupted, 'silent', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'media_invalid'


def test_invalid_kind_and_replaced_source_reject_before_tool_output(tmp_path):
    owner = TestOwnedFiles(tmp_path)
    source_path = make_video(tmp_path)
    source = owner.register(source_path)
    with pytest.raises(DownloadError) as failure:
        processor(owner).run(source, 'front-end-command', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'input_limit'
    source_path.write_bytes(b'replaced')
    with pytest.raises(DownloadError) as failure:
        processor(owner).run(source, 'wav', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'identity_mismatch'
    assert not owner.allocated


def test_windows_launch_policy_is_no_window_and_owns_a_process_group():
    assert importlib.util.find_spec('yijianjifa.features.downloads.media') is not None
    from yijianjifa.features.downloads.media import _spawn_options
    flags = _spawn_options('nt')['creationflags']
    assert flags & 0x08000000  # CREATE_NO_WINDOW
    assert flags & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    assert flags & 0x00000004  # CREATE_SUSPENDED: cannot execute before job assignment
    assert _spawn_options('posix') == {'start_new_session': True}


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable fixture; Windows real-tool acceptance is separate')
def test_probe_is_cancelled_as_an_owned_process_group(tmp_path):
    owner = TestOwnedFiles(tmp_path)
    source = owner.register(make_video(tmp_path))
    tool, marker, tool_pid, child_pid = make_blocking_tool(tmp_path, touch_output=False)
    class AfterStarted:
        def cancelled(self):
            return marker.exists()
    with pytest.raises(DownloadError) as failure:
        processor(owner, ffprobe=tool, terminate_grace=.1).run(source, 'wav', cancel=AfterStarted(), progress=lambda _: None)
    assert failure.value.code == 'cancelled'
    assert not owner.allocated and owner.path_for(source.id).exists()
    pids = [int(path.read_text()) for path in (tool_pid, child_pid)]
    deadline = time.monotonic() + 3
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not any(alive(pid) for pid in pids)


def test_disguised_playlist_rejects_without_starting_a_tool(tmp_path, monkeypatch):
    owner = TestOwnedFiles(tmp_path)
    path = tmp_path / 'disguised.mp4'
    path.write_text('#EXTM3U\n#EXTINF:1\nhttps://invalid.example/private.mp4\n')
    source = owner.register(path)
    def forbidden(*_, **__):
        pytest.fail('a disguised playlist must reject before subprocess launch')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(DownloadError) as failure:
        processor(owner).run(source, 'wav', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'media_invalid' and not owner.allocated


def test_both_tools_are_local_only_and_receive_argument_arrays(tmp_path, monkeypatch):
    owner = TestOwnedFiles(tmp_path)
    source = owner.register(make_video(tmp_path))
    actual_popen = subprocess.Popen
    calls = []
    def record(args, **kwargs):
        calls.append((args, kwargs))
        return actual_popen(args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', record)
    output = processor(owner).run(source, 'wav', cancel=NeverCancel(), progress=lambda _: None)
    assert output.exists() and len(calls) == 3
    for args, kwargs in calls:
        assert isinstance(args, list) and kwargs['shell'] is False
        assert args[args.index('-protocol_whitelist') + 1] == 'file,pipe'
    for args, _ in calls[:2]:
        assert args[args.index('-enable_drefs') + 1] == '0'
        assert args[args.index('-use_absolute_path') + 1] == '0'


def test_source_hash_is_checked_after_conversion_and_output_is_removed(tmp_path):
    owner = TestOwnedFiles(tmp_path)
    source_path = make_video(tmp_path)
    source = owner.register(source_path)
    changed = False
    def change_source(event):
        nonlocal changed
        if event.current > 0 and not changed:
            with source_path.open('ab') as stream:
                stream.write(b'changed-after-extract')
            changed = True
    with pytest.raises(DownloadError) as failure:
        processor(owner).run(source, 'wav', cancel=NeverCancel(), progress=change_source)
    assert changed and failure.value.code == 'identity_mismatch'
    assert not owner.allocated[-1].exists() and source_path.exists()


@pytest.mark.parametrize('codec', ['h264', 'vp9'])
def test_silent_video_preserves_display_rotation(tmp_path, codec):
    from yijianjifa.os_integration.resources import ffmpeg_path
    owner = TestOwnedFiles(tmp_path)
    original = make_video(tmp_path, codec=codec)
    rotated = tmp_path / 'rotated.mp4'
    subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-display_rotation:v:0', '90', '-i', str(original),
                    '-c', 'copy', str(rotated)],
                   capture_output=True, check=True, timeout=15)
    source = owner.register(rotated)
    before = probe(rotated)['streams'][0]
    output = processor(owner).run(source, 'silent', cancel=NeverCancel(), progress=lambda _: None)
    after = probe(output)['streams'][0]
    assert before['side_data_list'][0]['rotation'] == after['side_data_list'][0]['rotation'] == 90
    assert (before['width'], before['height']) == (after['width'], after['height'])


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable fixture; Windows real-tool acceptance is separate')
@pytest.mark.parametrize('corruption', ['invalid_bytes', 'shortened_audio'])
def test_success_exit_is_not_enough_to_accept_an_output(tmp_path, corruption):
    from yijianjifa.os_integration.resources import ffmpeg_path
    owner = TestOwnedFiles(tmp_path)
    source = owner.register(make_video(tmp_path))
    tool = tmp_path / 'unverified-ffmpeg'
    if corruption == 'invalid_bytes':
        script = 'import pathlib,sys\npathlib.Path(sys.argv[-1]).write_bytes(b"invalid WAV")\n'
    else:
        script = f'import subprocess,sys\nsubprocess.run([{str(ffmpeg_path())!r}, *sys.argv[1:-1], "-t", "0.3", sys.argv[-1]], check=True)\n'
    tool.write_text('#!' + sys.executable + '\n' + script)
    tool.chmod(0o700)
    with pytest.raises(DownloadError) as failure:
        processor(owner, ffmpeg=tool).run(source, 'wav', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'media_invalid'
    assert not owner.allocated[-1].exists() and owner.path_for(source.id).exists()


@pytest.mark.parametrize('extended_size', [False, True])
def test_valid_mp4_with_eight_kib_free_prefix_still_extracts_full_audio(tmp_path, extended_size):
    owner = TestOwnedFiles(tmp_path)
    padded = prefix_mp4_free_atom(make_video(tmp_path), extended_size=extended_size)
    source = owner.register(padded)
    assert float(probe(padded)['format']['duration']) == 1.4
    output = processor(owner).run(source, 'wav', cancel=NeverCancel(), progress=lambda _: None)
    assert abs(float(probe(output)['format']['duration']) - 1.4) <= .1
    assert digest(padded) == source.sha256


@pytest.mark.parametrize('prefix', [
    b'\x00\x00\x00\x04free',  # atom shorter than its header
    b'\xff\xff\xff\xfffree',  # outside the file boundary
    b'\x00\x00\x00\x00free',  # size-to-EOF has no later ftyp
    b'\x00\x00\x00\x01free\x00\x00\x00\x00\x00\x00\x00\x08',  # invalid extended size
    b'<html>#EXTM3U\n',
])
def test_bad_prefix_cannot_disguise_media_or_trigger_tool_launch(tmp_path, monkeypatch, prefix):
    owner = TestOwnedFiles(tmp_path)
    path = tmp_path / 'invalid-prefix.mp4'
    path.write_bytes(prefix + make_video(tmp_path).read_bytes())
    source = owner.register(path)
    monkeypatch.setattr(subprocess, 'Popen', lambda *_, **__: pytest.fail('invalid atom started a tool'))
    with pytest.raises(DownloadError) as failure:
        processor(owner).run(source, 'wav', cancel=NeverCancel(), progress=lambda _: None)
    assert failure.value.code == 'media_invalid'


@pytest.mark.parametrize('start_failure', [None, 'assignment', 'resume'])
def test_windows_control_flow_assigns_suspended_process_before_resuming(monkeypatch, start_failure):
    from yijianjifa.features.downloads import media
    events = []
    class Process:
        pid = 123
        returncode = 0
        stdout = io.BytesIO(b'complete')
        stderr = io.BytesIO()
        killed = False
        def poll(self):
            return None if start_failure == 'assignment' and not self.killed else 0
        def kill(self):
            self.killed = True
            events.append('kill_suspended')
        def wait(self):
            events.append('wait')
            return 0
    def create(args, **kwargs):
        flags = kwargs['creationflags']
        events.append('create_suspended' if flags & 4 else 'create_running')
        return Process()
    class Job:
        def __init__(self, process):
            events.append('assign_job')
            if start_failure == 'assignment':
                raise DownloadError('media_failed')
        def stop(self):
            events.append('stop_job')
        def close(self):
            events.append('close_job')
    monkeypatch.setattr(media, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(media.subprocess, 'Popen', create)
    monkeypatch.setattr(media, '_WindowsJob', Job)
    def resume(_):
        events.append('resume')
        if start_failure == 'resume':
            raise DownloadError('media_failed')
    monkeypatch.setattr(media, '_resume_windows_process', resume, raising=False)
    if start_failure == 'assignment':
        with pytest.raises(DownloadError):
            media._run_process(Path('backend-tool'), (), cancel=NeverCancel(), deadline=time.monotonic() + 3, grace=0)
        assert events[:3] == ['create_suspended', 'assign_job', 'kill_suspended']
        assert 'resume' not in events and 'wait' in events
        assert Process.stdout.closed and Process.stderr.closed
    elif start_failure == 'resume':
        with pytest.raises(DownloadError):
            media._run_process(Path('backend-tool'), (), cancel=NeverCancel(), deadline=time.monotonic() + 3, grace=0)
        assert events[:4] == ['create_suspended', 'assign_job', 'resume', 'stop_job']
        assert 'wait' in events and 'close_job' in events
        assert Process.stdout.closed and Process.stderr.closed
    else:
        result = media._run_process(Path('backend-tool'), (), cancel=NeverCancel(), deadline=time.monotonic() + 3, grace=0)
        assert result == b'complete'
        assert events[:3] == ['create_suspended', 'assign_job', 'resume']


@pytest.mark.skipif(os.name != 'nt', reason='target Windows integration requires Windows host')
@pytest.mark.parametrize('stop', ['cancel', 'timeout', 'abnormal', 'success'])
def test_windows_immediate_child_is_owned_and_cannot_survive_tool_exit(tmp_path, stop):
    from yijianjifa.features.downloads.media import _run_process
    child_pid = tmp_path / 'windows-child.pid'
    marker = tmp_path / 'windows-started'
    script = f'''
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))
pathlib.Path({str(marker)!r}).touch()
if {stop!r} in ('success', 'abnormal'):
    sys.exit(0 if {stop!r} == 'success' else 7)
time.sleep(60)
'''
    class AfterStarted:
        def cancelled(self):
            return stop == 'cancel' and marker.exists()
    deadline = time.monotonic() + (1 if stop == 'timeout' else 5)
    if stop == 'success':
        _run_process(Path(sys.executable), ('-c', script), cancel=AfterStarted(), deadline=deadline, grace=.1)
    else:
        with pytest.raises(DownloadError) as failure:
            _run_process(Path(sys.executable), ('-c', script), cancel=AfterStarted(), deadline=deadline, grace=.1)
        assert failure.value.code == {'cancel': 'cancelled', 'timeout': 'timeout', 'abnormal': 'media_failed'}[stop]
    assert marker.exists()
    child = int(child_pid.read_text())
    deadline = time.monotonic() + 3
    while windows_process_running(child) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not windows_process_running(child), 'Windows child survived the owned job'


def windows_process_running(pid):
    """Read-only process wait status; Windows os.kill(pid, 0) is not a safe probe."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            return False
        pytest.fail('Windows process status could not be verified')
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        assert result in (0, 0x102), 'process status query failed'
        return result == 0x102  # WAIT_TIMEOUT: still running
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != 'nt', reason='target Windows integration requires Windows host')
def test_windows_assignment_failure_kills_tool_before_it_can_execute(tmp_path, monkeypatch):
    from yijianjifa.features.downloads import media
    marker = tmp_path / 'must-not-execute'
    processes = []
    actual_popen = subprocess.Popen
    def capture(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        processes.append(process)
        return process
    def reject_assignment(_):
        raise DownloadError('media_failed')
    monkeypatch.setattr(media, '_WindowsJob', reject_assignment)
    monkeypatch.setattr(media.subprocess, 'Popen', capture)
    script = f'import pathlib; pathlib.Path({str(marker)!r}).touch()'
    with pytest.raises(DownloadError) as failure:
        media._run_process(Path(sys.executable), ('-c', script), cancel=NeverCancel(), deadline=time.monotonic() + 5, grace=.1)
    assert failure.value.code == 'media_failed' and not marker.exists()
    assert len(processes) == 1 and processes[0].returncode is not None


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signal permission regression')
@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
def test_group_probe_permission_race_keeps_primary_and_reaps_group(tmp_path, monkeypatch, stop):
    from yijianjifa.features.downloads import media
    import signal
    tool, marker, tool_pid, child_pid = make_blocking_tool(tmp_path, touch_output=False)
    real_killpg = os.killpg
    processes = []
    real_popen = subprocess.Popen
    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        # This case tests signalling an already-running process group, not
        # whether two fresh Python interpreters start within 400ms on the host.
        ready_until = time.monotonic() + 5
        while not marker.exists() and process.poll() is None and time.monotonic() < ready_until:
            time.sleep(.005)
        assert marker.exists(), 'fault process group did not become ready'
        return process
    def deny_probe(pid, value):
        if value == 0:
            raise PermissionError(1, 'injected probe race')
        return real_killpg(pid, value)
    monkeypatch.setattr(media.subprocess, 'Popen', launch)
    monkeypatch.setattr(media.os, 'killpg', deny_probe)
    class Cancel:
        def cancelled(self):
            return stop == 'cancel' and marker.exists()
    try:
        with pytest.raises(DownloadError) as error:
            media._run_process(tool, (), cancel=Cancel(),
                               deadline=time.monotonic() + (.4 if stop == 'timeout' else 5), grace=.05)
        assert error.value.code == ('timeout' if stop == 'timeout' else 'cancelled')
        assert marker.exists()
        assert processes[0].returncode is not None
        deadline = time.monotonic() + 3
        pids = [int(path.read_text()) for path in (tool_pid, child_pid)]
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not any(alive(pid) for pid in pids)
    finally:
        # RED must not leave the intentionally uncooperative test group alive.
        for process in processes:
            try:
                real_killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX signal permission regression')
@pytest.mark.parametrize('stop', ['cancel', 'timeout', 'success'])
def test_denied_group_signals_are_visible_and_do_not_replace_primary(tmp_path, monkeypatch, caplog, stop):
    from yijianjifa.features.downloads import media
    marker = tmp_path / 'started'
    script = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep({0 if stop == 'success' else 60})"
    processes = []
    real_popen = subprocess.Popen
    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process
    def deny_signal(pid, value):
        raise PermissionError(1, 'injected group signal denial')
    monkeypatch.setattr(media.subprocess, 'Popen', launch)
    monkeypatch.setattr(media.os, 'killpg', deny_signal)
    class Cancel:
        def cancelled(self):
            return stop == 'cancel' and marker.exists()
    try:
        with pytest.raises(DownloadError) as error:
            media._run_process(Path(sys.executable), ('-c', script), cancel=Cancel(),
                               deadline=time.monotonic() + (.4 if stop == 'timeout' else 5), grace=.01)
        assert error.value.code == {'cancel': 'cancelled', 'timeout': 'timeout', 'success': 'media_failed'}[stop]
        assert marker.exists()
        assert processes[0].returncode is not None
        assert 'media_cleanup_group_unverified' in caplog.text
        assert 'injected group signal denial' not in str(error.value)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
