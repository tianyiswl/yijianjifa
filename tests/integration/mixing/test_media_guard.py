"""Process lifetime tests use owned subprocesses and real pipe EOF, never PID sweeps."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), 'owned subprocess did not reach the expected state'


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_normal_promotion_preserves_output_and_unrelated_attempt(tmp_path):
    from yijianjifa.os_integration.media_guard import guarded_media
    output = tmp_path / 'final.mp4'
    unrelated = tmp_path / '.p011-render-unrelated.mp4'
    unrelated.write_bytes(b'unrelated')
    command = [sys.executable, '-c',
               'import pathlib,sys;pathlib.Path(sys.argv[-1]).write_bytes(b"complete")', str(output)]
    with guarded_media(command, output) as temporary:
        assert temporary != output and temporary.parent == output.parent
        assert not output.exists()
        assert temporary.read_bytes() == b'complete'
        temporary.replace(output)
    assert output.read_bytes() == b'complete'
    assert unrelated.read_bytes() == b'unrelated'
    assert not temporary.exists()


def test_large_child_stderr_is_drained_and_failure_cleans_owned_attempt(tmp_path):
    from yijianjifa.os_integration.media_guard import guarded_media
    output = tmp_path / 'final.mp4'
    output.write_bytes(b'existing success')
    command = [sys.executable, '-c',
               'import os,sys;os.write(2,b"x"*2000000+b"last-error");sys.exit(7)', str(output)]
    with pytest.raises(subprocess.CalledProcessError) as raised:
        with guarded_media(command, output):
            pytest.fail('failed child must not be promoted')
    assert raised.value.returncode == 7
    assert raised.value.stderr.endswith(b'last-error')
    assert len(raised.value.stderr) <= 65536
    assert output.read_bytes() == b'existing success'
    assert not list(tmp_path.glob('.p011-render-*'))


def test_probe_exception_releases_owned_attempt_without_changing_existing_output(tmp_path):
    from yijianjifa.os_integration.media_guard import guarded_media
    output = tmp_path / 'final.mp4'
    output.write_bytes(b'existing success')
    command = [sys.executable, '-c',
               'import pathlib,sys;pathlib.Path(sys.argv[-1]).write_bytes(b"invalid media")', str(output)]
    with pytest.raises(ValueError, match='invalid media'):
        with guarded_media(command, output) as temporary:
            assert temporary.read_bytes() == b'invalid media'
            raise ValueError('invalid media')
    assert output.read_bytes() == b'existing success'
    assert not temporary.exists()


def test_failed_child_start_cleans_attempt_without_waiting_for_lease(tmp_path):
    from yijianjifa.os_integration.media_guard import guarded_media
    output = tmp_path / 'final.mp4'
    with pytest.raises(subprocess.CalledProcessError) as raised:
        with guarded_media([str(tmp_path / 'no-such-tool'), str(output)], output):
            pytest.fail('missing child cannot render')
    assert raised.value.returncode == 127
    assert b'No such file' in raised.value.stderr or b'WinError 2' in raised.value.stderr
    assert not list(tmp_path.glob('.p011-render-*'))


def test_missing_output_directory_reports_worker_cause_without_hanging(tmp_path):
    from yijianjifa.os_integration.media_guard import guarded_media
    output = tmp_path / 'missing-parent' / 'final.mp4'
    with pytest.raises(RuntimeError, match='No such file'):
        with guarded_media([sys.executable, '-c', 'pass', str(output)], output):
            pytest.fail('missing output directory cannot render')


def test_invalid_worker_request_exits_without_starting_gui(tmp_path):
    launcher = Path(__file__).resolve().parents[3] / 'packaging/macos/launcher.py'
    result = subprocess.run([sys.executable, str(launcher), '--media-worker'],
                            input=json.dumps({'command': [], 'output': 'relative.mp4'})+'\n',
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert json.loads(result.stdout)['returncode'] == 127
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS SIGKILL lifetime evidence')
@pytest.mark.parametrize('promoted', [False, True])
def test_parent_killed_after_render_cleans_lease_but_preserves_formal_output(tmp_path, promoted):
    output, ready = tmp_path / 'final.mp4', tmp_path / 'ready.json'
    output.write_bytes(b'existing success')
    script = '''
import json,pathlib,sys,time
from yijianjifa.os_integration.media_guard import guarded_media
output=pathlib.Path(sys.argv[1])
command=[sys.executable,'-c','import pathlib,sys;pathlib.Path(sys.argv[-1]).write_bytes(b"complete")',str(output)]
with guarded_media(command,output) as temporary:
    if sys.argv[3]=='promoted':
        temporary.replace(output)
    pathlib.Path(sys.argv[2]).write_text(json.dumps(str(temporary)))
    time.sleep(60)
    temporary.replace(output)
'''
    parent = subprocess.Popen([sys.executable, '-c', script, str(output), str(ready),
                               'promoted' if promoted else 'not-promoted'])
    try:
        wait_for(ready.exists)
        temporary = Path(json.loads(ready.read_text()))
        assert (output if promoted else temporary).read_bytes() == b'complete'
        parent.kill()
        assert parent.wait(timeout=5) == -signal.SIGKILL
        wait_for(lambda: not temporary.exists())
        assert output.read_bytes() == (b'complete' if promoted else b'existing success')
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS SIGKILL lifetime evidence')
def test_parent_eof_kills_only_owned_child_that_ignores_terminate(tmp_path):
    output, ready = tmp_path / 'final.mp4', tmp_path / 'child.json'
    unrelated_file = tmp_path / '.p011-render-unrelated.mp4'
    unrelated_file.write_bytes(b'unrelated')
    child_code = '''
import json,os,pathlib,signal,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pathlib.Path(sys.argv[-1]).write_bytes(b"partial")
pathlib.Path(sys.argv[-2]).write_text(json.dumps({'pid':os.getpid(),'path':sys.argv[-1]}))
time.sleep(60)
'''
    parent_code = '''
import pathlib,sys
from yijianjifa.os_integration.media_guard import guarded_media
with guarded_media([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]],pathlib.Path(sys.argv[3])):
    pass
'''
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    parent = subprocess.Popen([sys.executable, '-c', parent_code, child_code, str(ready), str(output)])
    child = None
    try:
        wait_for(ready.exists)
        child = json.loads(ready.read_text())
        parent.kill()
        assert parent.wait(timeout=5) == -signal.SIGKILL
        wait_for(lambda: not alive(child['pid']))
        assert not Path(child['path']).exists()
        assert unrelated.poll() is None
        assert unrelated_file.read_bytes() == b'unrelated'
        assert not output.exists()
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)
        if child and alive(child['pid']):
            os.kill(child['pid'], signal.SIGKILL)


def test_launcher_worker_entry_runs_without_gui_initialization(tmp_path):
    launcher = Path(__file__).resolve().parents[3] / 'packaging/macos/launcher.py'
    output = tmp_path / 'final.mp4'
    command = [sys.executable, '-c',
               'import pathlib,sys;pathlib.Path(sys.argv[-1]).write_bytes(b"worker")', str(output)]
    worker = subprocess.Popen([sys.executable, str(launcher), '--media-worker'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        worker.stdin.write((json.dumps({'command': command, 'output': str(output)})+'\n').encode())
        worker.stdin.flush()
        ready = json.loads(worker.stdout.readline())
        result = json.loads(worker.stdout.readline())
        assert result['returncode'] == 0
        temporary = Path(ready['path'])
        assert temporary.read_bytes() == b'worker'
        worker.stdin.close()
        worker.wait(timeout=5)
        assert worker.returncode == 0
        assert not temporary.exists()
        assert not output.exists()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
