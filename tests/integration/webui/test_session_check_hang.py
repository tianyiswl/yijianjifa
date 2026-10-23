"""Real isolated workers, real DB/profiles, and synthetic Chromium SDK calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from test_session_check import check_fixture, check

def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

def source_environment():
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[3] / 'src')
    environment['PYTHONPATH'] = source + os.pathsep + environment.get('PYTHONPATH', '')
    return environment

def wait_for(predicate, seconds=3):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate(): return
        time.sleep(.01)
    assert predicate(), 'fake worker did not reach its expected boundary'

def real_worker(f, monkeypatch, tmp_path, stage='none'):
    from yijianjifa.webui import session_check, session_check_process
    original = session_check_process._command
    fake = Path(__file__).with_name('session_check_fake_worker.py')
    monkeypatch.setattr(session_check_process, '_command',
                        lambda kind='worker': [sys.executable, str(fake), stage] if kind == 'worker' else original(kind))
    monkeypatch.setattr(session_check, '_execute_check', lambda request, cancel, probe: session_check_process.run_check(request, cancel))
    trace = tmp_path / 'private-fake-events.jsonl'
    monkeypatch.setenv('P011_FAKE_CHECK_TRACE', str(trace))
    return trace

def assert_released(f, trace):
    from yijianjifa.webui.chromium_publish import account_lock
    assert not list(f.store.root.glob('.' + f.platform + '-check-*'))
    lock = account_lock(f.account.id)
    assert lock.acquire(blocking=False)
    lock.release()
    assert (f.source / 'Default' / 'Cookies').read_bytes() == b'original-private-cookie-database'
    assert f.service.get(f.account.id).session_ref == f.ref
    assert f.service.get(f.account.id).login_state == 'valid'
    for pid in {row['pid'] for row in records(trace)}:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, timeout=1)
        assert not result.stdout.strip() or result.stdout.lstrip().startswith('Z')

@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
@pytest.mark.parametrize('stuck_stage', ['evaluate', 'close', 'stop'])
def test_check_deadline_and_cancel_bound_a_stuck_host_sdk_call(check_fixture, monkeypatch, tmp_path, stuck_stage):
    from yijianjifa.webui import session_check
    f = check_fixture
    monkeypatch.setattr(session_check, 'CHECK_SECONDS', 3)
    trace = real_worker(f, monkeypatch, tmp_path, stuck_stage)
    results = []
    worker = threading.Thread(target=lambda: results.append(check(f)))
    worker.start()
    wait_for(lambda: any(row['event'] == stuck_stage for row in records(trace)))
    f.cancel.set()
    worker.join(3)
    assert not worker.is_alive(), f'{stuck_stage}: owned worker outlived cancellation'
    assert results == [{'state': 'unavailable', 'reason': 'cancelled'}]
    assert_released(f, trace)

@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_host_deadline_reaps_a_never_returning_sdk_without_marking_expired(check_fixture, monkeypatch, tmp_path):
    from yijianjifa.webui import session_check
    f = check_fixture
    monkeypatch.setattr(session_check, 'CHECK_SECONDS', .8)
    trace = real_worker(f, monkeypatch, tmp_path, 'evaluate')
    started = time.monotonic()
    assert check(f) == {'state': 'unavailable', 'reason': 'timeout'}
    assert time.monotonic() - started < 3
    assert any(row['event'] == 'evaluate' for row in records(trace))
    assert_released(f, trace)

def test_fresh_worker_loads_real_provider_restore_and_two_identity_checks(check_fixture, monkeypatch, tmp_path):
    f = check_fixture
    trace = real_worker(f, monkeypatch, tmp_path)
    assert check(f) == {'state': 'valid', 'platform_user_id': f.uid}
    names = [row['event'] for row in records(trace)]
    assert names.count('visit') == 2 and names.count('evaluate') == 4
    assert names.count('restore.cookies') == 1 and names[-2:] == ['close', 'stop']
    assert_released(f, trace)

@pytest.mark.skipif(os.name == 'nt', reason='POSIX detached process-group ownership')
@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
@pytest.mark.parametrize('stage', ['none', 'evaluate'])
def test_registered_detached_browser_and_protocol_fds_are_reaped_without_touching_unrelated_process(
        check_fixture, monkeypatch, tmp_path, stage):
    f = check_fixture
    trace = real_worker(f, monkeypatch, tmp_path, stage)
    monkeypatch.setenv('P011_FAKE_DETACHED', '1')
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    results = []
    worker = threading.Thread(target=lambda: results.append(check(f)))
    try:
        worker.start()
        wait_for(lambda: any(row['event'] == 'detached' for row in records(trace)))
        if stage != 'none': f.cancel.set()
        worker.join(5)
        assert not worker.is_alive()
        expected = 'valid' if stage == 'none' else 'unavailable'
        assert results[0]['state'] == expected
        row = next(row for row in records(trace) if row['event'] == 'detached')
        assert row['pid'] == row['pgid']
        assert unrelated.poll() is None
        assert_released(f, trace)
    finally:
        unrelated.kill(); unrelated.wait(timeout=2)
        f.cancel.set(); worker.join(3)

@pytest.mark.parametrize('host', ['macos', 'windows'])
def test_frozen_launchers_dispatch_check_before_gui_even_without_console(tmp_path, host):
    launcher = Path(__file__).resolve().parents[3] / 'packaging' / host / 'launcher.py'
    invalid = tmp_path / 'request.json'
    invalid.write_text('{}')
    runner = 'import runpy,sys;sys.stdin=sys.stdout=sys.stderr=None;sys.argv=[sys.argv[1],"--session-check-worker",sys.argv[2]];runpy.run_path(sys.argv[0],run_name="__main__")'
    result = subprocess.run([sys.executable, '-c', runner, str(launcher), str(invalid)],
                            env=source_environment(), timeout=3)
    assert result.returncode == 2


def test_registration_is_invisible_until_its_json_is_complete(tmp_path, monkeypatch):
    from yijianjifa.webui import session_check_process as owned
    gates = tmp_path / 'gates'; gates.mkdir()
    writing = threading.Event(); release = threading.Event()
    dump = owned.json.dump
    def slow_dump(value, output, **options):
        writing.set()
        assert release.wait(2)
        return dump(value, output, **options)
    monkeypatch.setattr(owned.json, 'dump', slow_dump)
    path = gates / '12345.json'
    thread = threading.Thread(target=lambda: owned._write_json(path, {'pid': 12345, 'parent': 123}))
    thread.start()
    try:
        assert writing.wait(1)
        owned._acknowledge(tmp_path, type('Process', (), {'pid': 123})(), set())
        assert not path.exists() and not list(gates.glob('*.ack'))
    finally:
        release.set(); thread.join(2)
    assert json.loads(path.read_text()) == {'pid': 12345, 'parent': 123}


@pytest.mark.skipif(os.name == 'nt', reason='POSIX parent lease EOF; Windows Job kills on handle close')
@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_parent_termination_closes_lease_and_reaps_owned_sdk_and_detached_browser(check_fixture, tmp_path):
    f = check_fixture
    trace = tmp_path / 'orphan-proof.jsonl'
    directory = f.store.root / '.douyin-check-parent-death'
    directory.mkdir(mode=0o700)
    request = dict(version=1, platform=f.platform, data_dir=str(f.store.root.parent),
                   account={key: getattr(f.account, key) for key in ('id', 'platform', 'platform_user_id', 'session_ref')},
                   directory=str(directory), deadline=time.monotonic() + 20)
    request['account']['session_ref'] = f.ref
    request_file = tmp_path / 'parent-request.json'; request_file.write_text(json.dumps(request))
    fake = Path(__file__).with_name('session_check_fake_worker.py')
    script = '''import json,sys,threading
from pathlib import Path
from yijianjifa.webui import session_check_process as owned
original=owned._command
owned._command=lambda kind='worker':[sys.executable,sys.argv[2],'evaluate'] if kind=='worker' else original(kind)
owned.run_check(json.loads(Path(sys.argv[1]).read_text()),threading.Event())
'''
    environment = source_environment()
    environment.update(P011_FAKE_CHECK_TRACE=str(trace), P011_FAKE_DETACHED='1')
    parent = subprocess.Popen([sys.executable, '-c', script, str(request_file), str(fake)], env=environment)
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    try:
        wait_for(lambda: any(row['event'] == 'detached' for row in records(trace)))
        parent.terminate(); parent.wait(timeout=2)
        wait_for(lambda: not directory.exists(), seconds=5)
        assert_released(f, trace)
        assert unrelated.poll() is None
    finally:
        if parent.poll() is None: parent.kill()
        parent.wait(timeout=2)
        unrelated.kill(); unrelated.wait(timeout=2)


@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_worker_identity_mismatch_is_never_accepted_as_valid(check_fixture, monkeypatch, tmp_path):
    f = check_fixture
    trace = real_worker(f, monkeypatch, tmp_path)
    monkeypatch.setenv('P011_FAKE_UID', '456')
    assert check(f) == {'state': 'identity_mismatch', 'platform_user_id': '456'}
    assert_released(f, trace)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX Node preload gate')
@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_node_gate_supports_chinese_and_spaces_in_private_profile_path(check_fixture, monkeypatch, tmp_path):
    from yijianjifa.webui import session_check
    f = check_fixture
    trace = real_worker(f, monkeypatch, tmp_path)
    monkeypatch.setenv('P011_FAKE_DETACHED', '1')
    create = session_check.tempfile.mkdtemp
    def chinese_directory(*args, **options):
        directory = Path(create(*args, **options))
        if options.get('prefix') == '.douyin-check-':
            renamed = directory.with_name(directory.name + '-中文 空格')
            directory.rename(renamed)
            return str(renamed)
        return str(directory)
    monkeypatch.setattr(session_check.tempfile, 'mkdtemp', chinese_directory)
    assert check(f) == {'state': 'valid', 'platform_user_id': f.uid}
    assert any(row['event'] == 'detached' for row in records(trace))
    assert_released(f, trace)


def test_windows_job_resume_failure_closes_job_and_reaps_suspended_worker(monkeypatch):
    from yijianjifa.webui import session_check_process as owned
    from yijianjifa.features.downloads import media
    events = []
    class Job:
        def __init__(self, process): events.append('assign')
        def stop(self): events.append('stop-job')
        def close(self): events.append('close-job')
    class Process:
        def poll(self): return None
        def kill(self): events.append('kill-worker')
        def wait(self, timeout): events.append('reap-worker')
    def failed_resume(process):
        events.append('resume')
        raise RuntimeError('synthetic native failure')
    monkeypatch.setattr(media, '_WindowsJob', Job)
    monkeypatch.setattr(media, '_resume_windows_process', failed_resume)
    with pytest.raises(OSError, match='could not be started'):
        owned._WindowsCheckJob(Process())
    assert events == ['assign', 'resume', 'stop-job', 'close-job', 'kill-worker', 'reap-worker']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX owned group cleanup')
def test_failed_process_scan_still_kills_and_reaps_the_known_owned_group(tmp_path, monkeypatch):
    from yijianjifa.webui import session_check_process as owned
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    def failed_scan(): raise OSError('synthetic process enumeration failure')
    monkeypatch.setattr(owned, '_processes', failed_scan)
    try:
        with pytest.raises(OSError, match='enumeration'):
            owned._stop_posix(child, set(), tmp_path)
        assert child.poll() is not None
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=2)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX native crash handler executable ownership')
def test_private_crash_handler_is_reaped_by_exact_pid_without_killing_its_shared_group(tmp_path):
    from yijianjifa.webui import session_check_process as owned
    compiler = shutil.which('cc')
    if not compiler: pytest.skip('native fixture compiler is unavailable')
    executable = tmp_path / 'chrome_crashpad_handler'
    source = '#include <unistd.h>\n#include <signal.h>\nint main(){signal(SIGTERM,SIG_IGN);for(;;)pause();}\n'
    subprocess.run([compiler, '-x', 'c', '-', '-o', str(executable)], input=source, text=True,
                   capture_output=True, timeout=10, check=True)
    database = Path('/tmp') / ('p011-check-test-' + str(os.getpid()))
    handler = subprocess.Popen([str(executable), '--database=' + str(database)])
    unrelated = subprocess.Popen([str(executable), '--database=' + str(database) + '-other'])
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    try:
        assert os.getpgid(handler.pid) == os.getpgrp()
        assert owned._crashpad_executable(handler.pid)
        assert owned._stop_posix(child, set(), database)
        handler.wait(timeout=2)
        assert unrelated.poll() is None
    finally:
        for process in (handler, unrelated, child):
            if process.poll() is None: process.kill()
            process.wait(timeout=2)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX browser gate before ACK')
@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_cancel_before_gate_ack_never_launches_an_unregistered_detached_browser(check_fixture, monkeypatch, tmp_path):
    from yijianjifa.webui import session_check_process as owned
    f = check_fixture
    trace = real_worker(f, monkeypatch, tmp_path)
    monkeypatch.setenv('P011_FAKE_DETACHED', '1')
    monkeypatch.setattr(owned, '_acknowledge', lambda *args: None)
    results = []
    thread = threading.Thread(target=lambda: results.append(check(f)))
    thread.start()
    try:
        wait_for(lambda: bool(list(f.store.root.glob('.douyin-check-*/gates/*.json'))))
        f.cancel.set(); thread.join(3)
        assert not thread.is_alive()
        assert results == [{'state': 'unavailable', 'reason': 'cancelled'}]
        assert not any(row['event'] == 'detached' for row in records(trace))
        assert_released(f, trace)
    finally:
        f.cancel.set(); thread.join(3)
