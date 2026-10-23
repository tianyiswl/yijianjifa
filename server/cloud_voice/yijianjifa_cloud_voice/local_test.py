"""Explicit, loopback-only source testing with a local speech simulation.

This entry point never reads deployment or supplier environment configuration.
Its isolated account/credit/job services are real; speech is macOS system speech,
not a paid model. Test codes are private fixtures, not payment receipts.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from wsgiref.simple_server import make_server

from .__main__ import _SilentHandler, _ThreadedServer
from .api import create_app
from .credits import CreditService
from .jobs import JobService
from .process_lock import LockedApplication, ServiceProcessLock
from .provider import MAX_AUDIO_BYTES, ProviderFailure, inspect_audio
from .store import Store

KIND = 'yjjf-local-cloud-voice-test-v1'
LOCAL_VOICE_ID = 'doubao:zh_female_vv_uranus_bigtts'
LOCAL_VOICE_NAME = '本地测试音色（系统语音模拟）'
_MARKER = 'local-test.json'


def _private_json(path: Path, payload, *, exclusive=False):
    """Write credentials/metadata without exposing partial contents or raw codes."""
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    temporary = path.with_name(path.name + '.partial-' + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Never replace the first issue's private code file or test marker.
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _test_directory(data_dir) -> Path:
    path = Path(data_dir)
    if not path.is_absolute():
        raise ValueError('本地测试数据目录必须使用绝对路径。')
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('本地测试目录不能使用符号链接。')
    path = path.resolve()
    if path.exists() and (not path.is_dir() or not (path / _MARKER).is_file()):
        # Empty, newly selected folders are fine. Existing application/service
        # data is never adopted merely because it happens to contain voice.db.
        if not path.is_dir() or any(path.iterdir()):
            raise ValueError('请选择新的专用测试目录，不能使用现有软件或正式服务数据。')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    managed = (_MARKER, 'voice.db', 'voice.db-wal', 'voice.db-shm',
               'voice.db.process.lock', 'audio', 'test-codes.json', 'service.json')
    if any((path / name).is_symlink() for name in managed):
        raise ValueError('本地测试数据文件不能使用符号链接。')
    marker = path / _MARKER
    expected = {'kind':KIND, 'local_test':True, 'data_dir':str(path)}
    if marker.exists():
        try:
            if json.loads(marker.read_text(encoding='utf-8')) != expected:
                raise ValueError()
        except (OSError, ValueError):
            raise ValueError('此目录不是本服务创建的专用测试目录。') from None
    else:
        _private_json(marker, expected, exclusive=True)
    os.chmod(path, 0o700)
    return path


def _tool_path(value) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('本地语音与音频转换工具必须是已安装的可执行文件绝对路径。')
    return str(path.resolve())


class SaySimulationProvider:
    """Audible local PCM, deliberately labelled as a non-model simulation."""
    version = 'local-test-macos-system-simulation-v1'
    max_daily_words = 200000

    def __init__(self, ffmpeg_path, say_path='/usr/bin/say', *, runner=None):
        self.ffmpeg_path = _tool_path(ffmpeg_path)
        self.say_path = _tool_path(say_path)
        self.runner = runner or subprocess.run

    def synthesize(self, text, voice, speed, request_id):
        if (not isinstance(text, str) or not text.strip() or len(text) > 4000
                or voice != LOCAL_VOICE_ID or not isinstance(speed, (int, float))
                or isinstance(speed, bool) or not math.isfinite(speed) or not .5 <= speed <= 2):
            raise ProviderFailure('invalid_request')
        # No text in process arguments or logs, no shell, no inherited supplier
        # credentials, and no executable, voice, URL or model chosen by a client.
        environment = {'PATH':'/usr/bin:/bin', 'LANG':'en_US.UTF-8'}
        with tempfile.TemporaryDirectory(prefix='yjjf-local-voice-') as folder:
            source, target = Path(folder) / 'speech.aiff', Path(folder) / 'speech.wav'
            try:
                self.runner([self.say_path, '-v', 'Tingting', '-r', str(round(175 * speed)),
                             '-o', str(source)], input=text.encode('utf-8'), check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                            env=environment)
                self.runner([self.ffmpeg_path, '-nostdin', '-hide_banner', '-loglevel', 'error',
                             '-y', '-i', str(source), '-ac', '1', '-ar', '24000',
                             '-c:a', 'pcm_s16le', str(target)], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                            env=environment)
                if not 0 < target.stat().st_size <= MAX_AUDIO_BYTES:
                    raise ProviderFailure('invalid_audio')
                audio = target.read_bytes()
                duration, mime = inspect_audio(audio)
                if mime != 'audio/wav':
                    raise ProviderFailure('invalid_audio')
                return audio, duration
            except (subprocess.SubprocessError, OSError, UnicodeError):
                raise ProviderFailure('local_simulation_failed') from None


@dataclass
class LocalTestRuntime:
    data_dir: Path
    app: LockedApplication
    store: Store
    credits: CreditService

    @property
    def codes_file(self):
        return self.data_dir / 'test-codes.json'

    @property
    def service_file(self):
        return self.data_dir / 'service.json'

    def close(self):
        self.app.close()


def _seed_codes(path, store, credits):
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            if (payload['kind'] != KIND or payload['local_test'] is not True
                    or len(payload['codes']) != 2
                    or any(credits.code_status(row['id'])['words'] != 10000 for row in payload['codes'])):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError('测试码文件无法核对，服务未启动；不会重新发码。') from None
        os.chmod(path, 0o600)
        return
    with store.connect() as connection:
        if connection.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0]:
            raise RuntimeError('原测试码文件已丢失，服务未启动；不会重新发码。')
    codes = credits.issue(2, order_ref='LOCAL-TEST-FIXTURES-NOT-A-PAYMENT')
    _private_json(path, {'kind':KIND, 'local_test':True,
                        'notice':'仅本机测试，不代表收款；每码增加10000测试字数。',
                        'codes':codes}, exclusive=True)


def create_local_test(data_dir, *, ffmpeg_path, say_path='/usr/bin/say'):
    if getattr(sys, 'frozen', False):
        raise RuntimeError('本地云配音测试服务仅供源码运行。')
    # Check tools before creating any new directory or issuing test fixtures.
    provider = SaySimulationProvider(ffmpeg_path, say_path)
    path = _test_directory(data_dir)
    lock = ServiceProcessLock(path / 'voice.db')
    try:
        store = Store(path / 'voice.db')
        credits = CreditService(store)
        _seed_codes(path / 'test-codes.json', store, credits)
        jobs = JobService(store, path / 'audio', provider,
                          [{'id':LOCAL_VOICE_ID, 'name':LOCAL_VOICE_NAME}])
        return LocalTestRuntime(path, LockedApplication(create_app(store, credits=credits, jobs=jobs), lock),
                                store, credits)
    except BaseException:
        lock.close()
        raise


@contextmanager
def create_local_server(runtime, *, port=0):
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError('测试服务端口需为0至65535。')
    with make_server('127.0.0.1', port, runtime.app,
                     server_class=_ThreadedServer, handler_class=_SilentHandler) as server:
        descriptor = {'kind':KIND, 'local_test':True, 'endpoint':f'http://127.0.0.1:{server.server_port}',
                      'pid':os.getpid(), 'data_dir':str(runtime.data_dir),
                      'database':str(runtime.store.path), 'codes_file':str(runtime.codes_file),
                      'voice_id':LOCAL_VOICE_ID, 'audio_mode':'macos-system-simulation',
                      'notice':'仅本机测试，不连接大模型、不产生真实费用。'}
        _private_json(runtime.service_file, descriptor)
        try:
            yield server
        finally:
            runtime.service_file.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='源码专用本地云配音流程测试；仅系统语音模拟，无真实费用。')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True)
    parser.add_argument('--say', type=Path, default=Path('/usr/bin/say'))
    parser.add_argument('--port', type=int, default=0)
    arguments = parser.parse_args(argv)
    runtime = create_local_test(arguments.data_dir, ffmpeg_path=arguments.ffmpeg, say_path=arguments.say)
    try:
        with create_local_server(runtime, port=arguments.port) as server:
            # Only file locations and endpoint are printed; never codes, text or sessions.
            print(f'本机流程测试已启动：http://127.0.0.1:{server.server_port}', flush=True)
            print(f'服务信息：{runtime.service_file}', flush=True)
            print(f'私有测试码文件：{runtime.codes_file}', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
