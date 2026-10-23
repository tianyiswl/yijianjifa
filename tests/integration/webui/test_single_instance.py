"""Real process locks must prevent a second writer and survive stale lock files."""
import subprocess
import sys
import pytest

if sys.platform == "win32":
    pytest.skip("POSIX/macOS startup uses fcntl; Windows uses its Qt instance lock", allow_module_level=True)

from yijianjifa.webui import mac_app


def child_attempt(directory):
    return subprocess.run([sys.executable, '-c', '''
import sys
from pathlib import Path
from yijianjifa.webui.single_instance import InstanceLock, AlreadyRunning
try:
    with InstanceLock(Path(sys.argv[1])):
        print('acquired')
except AlreadyRunning:
    print('busy')
''', str(directory)], capture_output=True, text=True, check=True).stdout.strip()


def test_same_data_directory_rejects_second_process_but_other_directory_is_independent(tmp_path):
    assert hasattr(mac_app, 'InstanceLock'), 'Mac startup has no single-instance guard'
    with mac_app.InstanceLock(tmp_path / 'one'):
        assert child_attempt(tmp_path / 'one') == 'busy'
        assert child_attempt(tmp_path / 'two') == 'acquired'
    assert child_attempt(tmp_path / 'one') == 'acquired'


def test_process_crash_releases_lock_without_removing_lock_file(tmp_path):
    assert hasattr(mac_app, 'InstanceLock'), 'Mac startup has no single-instance guard'
    process = subprocess.Popen([sys.executable, '-u', '-c', '''
import sys,time
from pathlib import Path
from yijianjifa.webui.single_instance import InstanceLock
with InstanceLock(Path(sys.argv[1])):
    print('locked',flush=True)
    time.sleep(30)
''', str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'locked'
        assert child_attempt(tmp_path) == 'busy'
        process.kill()
        process.wait(timeout=5)
        assert child_attempt(tmp_path) == 'acquired'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_second_app_exits_before_backup_or_database_migration(tmp_path):
    assert hasattr(mac_app, 'InstanceLock'), 'Mac startup has no single-instance guard'
    db = tmp_path / 'app.db'
    db.write_bytes(b'untouched sentinel database')
    with mac_app.InstanceLock(tmp_path):
        result = subprocess.run([sys.executable, '-m', 'yijianjifa.webui.mac_app', '--data-dir', str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert '已经运行' in result.stdout
    assert db.read_bytes() == b'untouched sentinel database'
    assert not (tmp_path / 'backups').exists()
