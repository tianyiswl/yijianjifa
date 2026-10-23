from pathlib import Path
import subprocess
import sys

import pytest

from yijianjifa.os_integration import background_process, media_guard


def test_windows_tool_keeps_pipes_and_existing_creation_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, 'platform', 'win32')
    sentinel = subprocess.CompletedProcess(['ffmpeg'], 0, b'audio', b'')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: (calls.append((a, k)), sentinel)[1])
    assert background_process.run_hidden(['ffmpeg'], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, creationflags=0x200) is sentinel
    assert calls[0][1] == {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE,
                           'creationflags': 0x08000200}


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_other_platforms_keep_original_process_options(monkeypatch, platform):
    monkeypatch.setattr(sys, 'platform', platform)
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: calls.append(k))
    background_process.run_hidden(['ffmpeg'], timeout=30)
    assert calls == [{'timeout': 30}]


@pytest.mark.parametrize('fail', [False, True])
def test_frozen_windows_render_hides_console_and_preserves_cleanup(monkeypatch, tmp_path, fail):
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    output = tmp_path / 'result.mp4'
    output.write_bytes(b'previous')
    attempts = []

    def render(command, **kwargs):
        assert kwargs['creationflags'] & 0x08000000
        assert kwargs['stdout'] is subprocess.PIPE and kwargs['stderr'] is subprocess.PIPE
        temporary = Path(command[-1])
        assert temporary != output
        attempts.append(temporary)
        temporary.write_bytes(b'new')
        if fail:
            raise subprocess.CalledProcessError(1, command, stderr=b'failure')

    monkeypatch.setattr(subprocess, 'run', render)
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            with media_guard.guarded_media(['ffmpeg', str(output)], output):
                pytest.fail('failed output must not be promoted')
        assert output.read_bytes() == b'previous'
    else:
        with media_guard.guarded_media(['ffmpeg', str(output)], output) as temporary:
            temporary.replace(output)
        assert output.read_bytes() == b'new'
    assert attempts and all(not path.exists() for path in attempts)


@pytest.mark.skipif(sys.platform != 'win32', reason='requires actual Windows console APIs')
def test_real_windows_child_has_no_console_window():
    result = background_process.run_hidden(
        [sys.executable, '-c', 'import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())'],
        check=True, capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == '0'
