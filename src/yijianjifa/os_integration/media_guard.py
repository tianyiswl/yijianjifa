"""Keep one render child and its temporary file tied to the caller's pipe lease.

The helper is a separate interpreter, not a fork of the GUI's threaded process.
It owns the temporary name and its Popen child; it never searches for processes
or sweeps output directories. The caller holds the lease through probe/promotion.
"""
from contextlib import contextmanager
import base64
import json
import os
from pathlib import Path
from yijianjifa.os_integration.background_process import run_hidden, hidden_process_options
import subprocess
import sys
import tempfile
import threading


_STDERR_LIMIT = 65536


def _worker_command():
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--media-worker']
    return [sys.executable, '-m', 'yijianjifa.os_integration.media_guard']


@contextmanager
def guarded_media(command: list[str], output: Path):
    """Yield a successfully rendered private path; release it after promotion.

Closing stdin is the lease release. A SIGKILL also closes that writer, so the
helper can reap FFmpeg and unlink its private path even when this finally cannot
run. The lease deliberately remains open while the caller probes and replaces.
"""
    output = Path(output).absolute()
    # Windows's frozen launcher has no worker entry yet. Preserve its existing
    # behavior until that platform receives its own package/runtime verification.
    if getattr(sys, 'frozen', False) and sys.platform != 'darwin':
        fd, name = tempfile.mkstemp(prefix='.p011-render-', suffix=output.suffix, dir=output.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            run_hidden([*command[:-1], str(temporary)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            yield temporary
        finally:
            temporary.unlink(missing_ok=True)
        return

    env = os.environ.copy()
    if getattr(sys, 'frozen', False):
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    else:
        source_root = str(Path(__file__).resolve().parents[2])
        env['PYTHONPATH'] = source_root + os.pathsep + env.get('PYTHONPATH', '')
    worker = subprocess.Popen(_worker_command(), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              close_fds=True, env=env, **hidden_process_options())
    try:
        payload = {'command': [str(value) for value in command], 'output': str(output)}
        worker.stdin.write((json.dumps(payload)+'\n').encode())
        worker.stdin.flush()
        ready = _read_message(worker.stdout)
        if 'path' not in ready:
            cause = base64.b64decode(ready.get('stderr', '')).decode(errors='replace')
            raise RuntimeError('媒体工作进程无法准备临时文件：' + cause)
        temporary = Path(ready['path'])
        result = _read_message(worker.stdout)
        if result['returncode']:
            raise subprocess.CalledProcessError(result['returncode'], command,
                                                stderr=base64.b64decode(result['stderr']))
        yield temporary
    finally:
        try:
            worker.stdin.close()
        except BrokenPipeError:
            pass
        # EOF lets the helper terminate, escalate and reap its own exact child.
        # Do not kill the helper here: it owns that cleanup responsibility.
        try:
            worker.wait(timeout=8)
        finally:
            worker.stdout.close()


def _read_message(stream):
    line = stream.readline(131072)
    if not line or not line.endswith(b'\n'):
        raise RuntimeError('媒体工作进程提前退出，未确认渲染结果。')
    return json.loads(line)


def _stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


def worker_main():
    """Hidden CLI entry, including windowed PyInstaller with sys.std* = None."""
    # Use inherited OS pipes directly; a windowed frozen app may have no Python
    # sys.stdin/stdout objects despite valid Popen pipe descriptors.
    incoming = os.fdopen(os.dup(0), 'rb')
    outgoing = os.fdopen(os.dup(1), 'wb')
    lost_parent = threading.Event()
    temporary = None
    child = None
    tail = bytearray()
    drainer = None

    def send(message):
        try:
            outgoing.write((json.dumps(message)+'\n').encode())
            outgoing.flush()
            return True
        except (BrokenPipeError, OSError):
            lost_parent.set()
            return False

    def watch_lease():
        try:
            # The parent sends exactly one request and no more bytes. Any data
            # after it is not a new command and cannot extend a lost lease.
            while os.read(incoming.fileno(), 1):
                pass
        except OSError:
            pass
        finally:
            lost_parent.set()

    def drain_errors():
        while True:
            chunk = child.stderr.read(8192)
            if not chunk:
                break
            tail.extend(chunk)
            if len(tail) > _STDERR_LIMIT:
                del tail[:-_STDERR_LIMIT]

    try:
        payload = _read_message(incoming)
        command, output = payload['command'], Path(payload['output'])
        if not command or any(type(value) is not str for value in command) or not output.is_absolute():
            raise ValueError('invalid media worker request')
        threading.Thread(target=watch_lease, daemon=True).start()
        fd, name = tempfile.mkstemp(prefix='.p011-render-', suffix=output.suffix, dir=output.parent)
        os.close(fd)
        temporary = Path(name)
        if not send({'path': str(temporary)}) or lost_parent.is_set():
            return 0
        child = subprocess.Popen([*command[:-1], str(temporary)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, close_fds=True, **hidden_process_options())
        drainer = threading.Thread(target=drain_errors, daemon=True)
        drainer.start()
        while child.poll() is None:
            if lost_parent.wait(.05):
                _stop(child)
                return 0
        drainer.join()
        if send({'returncode': child.returncode, 'stderr': base64.b64encode(tail).decode()}):
            # FFmpeg is done, but the parent still owns probe and atomic rename.
            lost_parent.wait()
        return 0
    except Exception as exc:
        send({'returncode': 127, 'stderr': base64.b64encode(str(exc).encode()[-_STDERR_LIMIT:]).decode()})
        return 1
    finally:
        if child is not None:
            _stop(child)
            if drainer is not None:
                drainer.join(timeout=2)
            child.stderr.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        outgoing.close()
        incoming.close()


if __name__ == '__main__':
    raise SystemExit(worker_main())
