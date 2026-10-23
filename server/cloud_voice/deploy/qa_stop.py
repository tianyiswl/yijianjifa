#!/usr/bin/env python3
"""One isolated in-flight SIGTERM probe. No production DB, secrets or payment."""
import http.client
import importlib.util
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time


PORT = 18770


def child():
    from yijianjifa_cloud_voice import wsgi
    original = wsgi.create_from_environment
    marker = Path(os.environ['YJJF_QA_STOP_MARKER'])
    def factory():
        app = original()
        actual = app.app
        def delayed(environ, start_response):
            if environ.get('PATH_INFO') == '/qa-stop':
                marker.write_text('active', encoding='ascii')
                time.sleep(1)
                marker.write_text('completed', encoding='ascii')
            return actual(environ, start_response)
        app.app = delayed
        return app
    wsgi.create_from_environment = factory
    path = Path(__file__).with_name('production.py')
    spec = importlib.util.spec_from_file_location('production_stop_qa', path)
    production = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(production)
    real_build = production.build_server
    production.build_server = lambda: real_build(port=PORT)
    sys.argv = [str(path)]
    return production.main()


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('probe timed out')


def ready():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=.2):
            return True
    except OSError:
        return False


def main():
    from yijianjifa_cloud_voice.process_lock import ServiceProcessLock
    with socket.socket() as guard:
        guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        guard.bind(('127.0.0.1', PORT))
    with tempfile.TemporaryDirectory(prefix='yjjf-stop-qa-') as directory:
        os.chmod(directory, 0o700)
        database = Path(directory) / 'qa.db'
        marker = Path(directory) / 'request-state'
        environment = {'PATH': os.defpath,
                       'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
                       'PYTHONDONTWRITEBYTECODE': '1',
                       'YJJF_CLOUD_DATABASE': str(database),
                       'YJJF_QA_STOP_MARKER': str(marker)}
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child'],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client = None
        try:
            wait_for(ready)
            def call():
                connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=5)
                try:
                    connection.request('GET', '/qa-stop', headers={'Connection': 'close'})
                    response = connection.getresponse()
                    response.read(8192)
                except (OSError, http.client.HTTPException):
                    # This probe checks safe draining/locking, not delivery of a
                    # response after the service event loop has been stopped.
                    pass
                finally:
                    connection.close()
            client = threading.Thread(target=call, daemon=True)
            client.start()
            wait_for(lambda: marker.exists() and marker.read_text() == 'active')
            process.send_signal(signal.SIGTERM)
            try:
                lock = ServiceProcessLock(database)
            except RuntimeError:
                pass
            else:
                lock.close()
                raise AssertionError('database lock released before request drained')
            stdout, stderr = process.communicate(timeout=10)
            if process.returncode != 0:
                raise AssertionError('SIGTERM exit was not clean')
            if b'ERROR' in stderr or b'Traceback' in stderr or b'Bad file descriptor' in stderr:
                raise AssertionError('in-flight shutdown emitted an error')
            if marker.read_text() != 'completed':
                raise AssertionError('in-flight request did not drain')
            lock = ServiceProcessLock(database)
            lock.close()
        finally:
            if process.poll() is None:
                process.kill()  # Only our own temporary probe child.
                process.communicate(timeout=5)
            if client is not None:
                client.join(timeout=5)
    print('PASS: in-flight SIGTERM drained without error; lock held then released.')
    return 0


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--child']:
            raise SystemExit(child())
        if sys.argv[1:]:
            raise AssertionError('probe accepts no arguments')
        raise SystemExit(main())
    except AssertionError as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print('FAIL: stop probe infrastructure (' + type(exc).__name__ + ').', file=sys.stderr)
        raise SystemExit(1)
