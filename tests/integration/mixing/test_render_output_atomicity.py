from pathlib import Path
from contextlib import contextmanager
import os
import signal
import subprocess
import sys
import time
import tempfile

import pytest

from yijianjifa.features.mixing import ffmpeg_renderer as module
from yijianjifa.os_integration.resources import ffmpeg_path


def request_for(tmp_path):
    return module.RenderRequest(
        segments=(module.RenderSegment(tmp_path / 'source.mp4', 0, 1000),),
        output_path=tmp_path / '001.mp4',
        keep_original_audio=False,
    )


def replace_media_tool(monkeypatch, render):
    """Isolate probe/promotion from the process boundary tested in media_guard."""
    @contextmanager
    def attempt(command, output):
        fd, name = tempfile.mkstemp(prefix='.p011-render-', suffix=output.suffix, dir=output.parent)
        os.close(fd)
        path = Path(name)
        try:
            render([*command[:-1], str(path)])
            yield path
        finally:
            path.unlink(missing_ok=True)
    monkeypatch.setattr(module, 'guarded_media', attempt)


def test_formal_output_appears_only_after_render_and_probe_succeed(tmp_path, monkeypatch):
    request = request_for(tmp_path)
    written = []

    def render(command, **kwargs):
        temporary = Path(command[-1])
        assert temporary != request.output_path
        assert temporary.parent == request.output_path.parent
        assert not request.output_path.exists()
        temporary.write_bytes(b'complete media')
        written.append(temporary)

    def probe(path):
        assert path == written[0]
        assert not request.output_path.exists()
        return {'format': {'duration': '1'}}

    replace_media_tool(monkeypatch, render)
    monkeypatch.setattr(module, '_probe', probe)
    result = module.FFmpegRenderer().render(request)

    assert result.output_path == request.output_path
    assert result.duration_ms == 1000
    assert request.output_path.read_bytes() == b'complete media'
    assert not written[0].exists()


@pytest.mark.parametrize('failure_stage', ['render', 'probe'])
def test_failed_attempt_removes_only_its_partial_and_preserves_existing_output(
    tmp_path, monkeypatch, failure_stage
):
    request = request_for(tmp_path)
    request.output_path.write_bytes(b'existing completed media')
    unrelated = tmp_path / '.p011-render-unrelated.mp4'
    unrelated.write_bytes(b'other attempt')
    written = []

    def render(command, **kwargs):
        temporary = Path(command[-1])
        temporary.write_bytes(b'partial media')
        written.append(temporary)
        if failure_stage == 'render':
            raise subprocess.CalledProcessError(1, command, stderr=b'failure')

    def probe(path):
        raise ValueError('invalid rendered media')

    replace_media_tool(monkeypatch, render)
    monkeypatch.setattr(module, '_probe', probe)
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        module.FFmpegRenderer().render(request)

    assert request.output_path.read_bytes() == b'existing completed media'
    assert not written[0].exists()
    assert unrelated.read_bytes() == b'other attempt'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permits unlinking an open orphan output')
def test_retry_does_not_share_output_inode_with_an_older_writer(tmp_path, monkeypatch):
    request = request_for(tmp_path)
    attempts = []
    older_writer = None

    def render(command, **kwargs):
        nonlocal older_writer
        temporary = Path(command[-1])
        attempts.append(temporary)
        if len(attempts) == 1:
            older_writer = temporary.open('w+b')
            older_writer.write(b'old incomplete media')
            older_writer.flush()
            raise subprocess.CalledProcessError(1, command)
        temporary.write_bytes(b'new completed media')

    replace_media_tool(monkeypatch, render)
    monkeypatch.setattr(module, '_probe', lambda path: {'format': {'duration': '1'}})
    try:
        with pytest.raises(subprocess.CalledProcessError):
            module.FFmpegRenderer().render(request)
        module.FFmpegRenderer().render(request)
        older_writer.seek(0)
        older_writer.write(b'old writer still alive')
        older_writer.flush()
        assert request.output_path.read_bytes() == b'new completed media'
        assert attempts[0] != attempts[1]
    finally:
        if older_writer is not None:
            older_writer.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='isolated macOS force-quit regression')
def test_killing_render_parent_reaps_its_ffmpeg_and_removes_only_its_partial(tmp_path):
    source, output = tmp_path / 'source.mp4', tmp_path / '001.mp4'
    subprocess.run(
        [str(ffmpeg_path()), '-y', '-v', 'error', '-f', 'lavfi', '-i',
         'color=c=red:s=64x64:r=25:d=30', '-c:v', 'libx264', str(source)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    script = '''
from pathlib import Path
import sys
from yijianjifa.features.mixing.ffmpeg_renderer import FFmpegRenderer, RenderRequest, RenderSegment
FFmpegRenderer().render(RenderRequest(
    segments=(RenderSegment(Path(sys.argv[1]), 0, 30000),),
    output_path=Path(sys.argv[2]), keep_original_audio=False,
))
'''
    process = subprocess.Popen(
        [sys.executable, '-c', script, str(source), str(output)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    child_pid, child_command = None, None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            rows = subprocess.check_output(
                ['ps', '-axo', 'pid=,ppid=,comm='], text=True
            ).splitlines()
            descendants = {process.pid}
            parsed = [row.strip().split(None, 2) for row in rows]
            for _ in range(4):
                descendants.update(int(f[0]) for f in parsed if len(f) == 3 and int(f[1]) in descendants)
            for row in rows:
                fields = row.strip().split(None, 2)
                if len(fields) == 3 and int(fields[0]) in descendants and 'ffmpeg' in fields[2]:
                    candidate = int(fields[0])
                    command = subprocess.run(
                        ['ps', '-p', str(candidate), '-o', 'command='], capture_output=True, text=True
                    ).stdout.strip()
                    # Binary discovery can briefly invoke ffmpeg -version first.
                    if str(tmp_path) not in command:
                        continue
                    child_pid, child_command = candidate, command
                    break
            if child_pid is not None and any(
                path.stat().st_size > 0
                for path in tmp_path.glob('*.mp4') if path != source
            ):
                break
            time.sleep(.01)
        assert child_pid is not None, 'render child was not observed'
        assert process.poll() is None, 'render finished before the force-quit boundary'
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert not output.exists(), 'force quit exposed incomplete bytes as a formal MP4'
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = subprocess.run(
                ['ps', '-p', str(child_pid), '-o', 'command='], capture_output=True, text=True
            ).stdout.strip()
            if not current and not list(tmp_path.glob('.p011-render-*.mp4')):
                break
            time.sleep(.02)
        assert not current, 'force quit left this render FFmpeg running'
        assert not list(tmp_path.glob('.p011-render-*.mp4')), 'force quit left this attempt partial behind'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child_pid is not None:
            current = subprocess.run(
                ['ps', '-p', str(child_pid), '-o', 'command='], capture_output=True, text=True
            ).stdout.strip()
            # Only reap this test's recorded process, never a name or process group.
            if current and current == child_command:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        process.communicate(timeout=5)
