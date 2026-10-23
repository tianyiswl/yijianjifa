import io
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from test_api import call


def test_local_service_rejects_unmarked_existing_data_and_relative_paths(tmp_path):
    from yijianjifa_cloud_voice.local_test import create_local_test
    with pytest.raises(ValueError, match='绝对路径'):
        create_local_test(Path('relative'), ffmpeg_path=sys.executable, say_path=sys.executable)
    target = tmp_path / 'production'
    target.mkdir()
    database = target / 'voice.db'
    database.write_bytes(b'keep-production-data')
    with pytest.raises(ValueError, match='专用测试目录'):
        create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
    assert database.read_bytes() == b'keep-production-data'
    assert list(target.iterdir()) == [database]


def test_local_service_seeds_two_private_codes_once_and_ignores_supplier_env(tmp_path, monkeypatch):
    from yijianjifa_cloud_voice.local_test import create_local_test
    monkeypatch.setenv('YJJF_CLOUD_DATABASE', str(tmp_path / 'must-not-open.db'))
    monkeypatch.setenv('YJJF_VOLC_ACCESS_KEY', 'must-not-read')
    target = tmp_path / 'local-test'
    first = create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
    codes_path = first.codes_file
    saved = codes_path.read_bytes()
    codes = json.loads(saved)['codes']
    assert len(codes) == 2
    assert all(code['words'] == 10000 for code in codes)
    assert os.stat(codes_path).st_mode & 0o777 == 0o600
    assert not (tmp_path / 'must-not-open.db').exists()
    first.close()
    second = create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
    try:
        assert codes_path.read_bytes() == saved
        with second.store.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 2
    finally:
        second.close()


def test_local_service_cannot_duplicate_lost_codes_or_follow_managed_symlinks(tmp_path):
    from yijianjifa_cloud_voice.local_test import create_local_test
    target = tmp_path / 'local-test'
    first = create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
    first.close()
    first.codes_file.unlink()
    with pytest.raises(RuntimeError, match='不会重新发码'):
        create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    linked = tmp_path / 'linked'
    linked.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        create_local_test(linked, ffmpeg_path=sys.executable, say_path=sys.executable)


def _pcm_wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b'\x01\x00' * 2400)
    return output.getvalue()


def test_say_provider_uses_stdin_and_validates_local_pcm_without_network(tmp_path):
    from yijianjifa_cloud_voice.local_test import LOCAL_VOICE_ID, SaySimulationProvider
    from yijianjifa_cloud_voice.provider import ProviderFailure
    commands = []
    def runner(command, **options):
        commands.append((command, options))
        if len(commands) == 1:
            Path(command[command.index('-o') + 1]).write_bytes(b'fake-aiff')
        else:
            Path(command[-1]).write_bytes(_pcm_wav())
        return subprocess.CompletedProcess(command, 0, b'', b'')
    provider = SaySimulationProvider(sys.executable, sys.executable, runner=runner)
    text = '本地配音测试，不应出现在命令参数中。'
    audio, duration = provider.synthesize(text, LOCAL_VOICE_ID, 1.2, 'request-1')
    assert audio == _pcm_wav() and duration == 100
    assert commands[0][1]['input'] == text.encode('utf-8')
    assert all(text not in ' '.join(command) for command, _ in commands)
    assert all(options['check'] and options['timeout'] <= 120 for _, options in commands)
    with pytest.raises(ProviderFailure, match='invalid_request'):
        provider.synthesize(text, 'doubao:other-voice', 1, 'request-2')
    assert len(commands) == 2


def test_local_test_complete_api_uses_real_ledger_and_preserves_unspent_user_codes(tmp_path, monkeypatch):
    from yijianjifa_cloud_voice.local_test import LOCAL_VOICE_ID, SaySimulationProvider, create_local_test
    calls = []
    def synthesize(self, text, voice, speed, request_id):
        calls.append(request_id)
        return _pcm_wav(), 100
    monkeypatch.setattr(SaySimulationProvider, 'synthesize', synthesize)
    runtime = create_local_test(tmp_path / 'full', ffmpeg_path=sys.executable, say_path=sys.executable)
    try:
        app = runtime.app
        token = call(app, 'POST', '/v1/accounts', {'username':'local_qa', 'password':'local test password'})['body']['access_token']
        qa_code = runtime.credits.issue(1, order_ref='LOCAL-TEST-QA-NOT-A-PAYMENT')[0]['code']
        request = {'code':qa_code, 'idempotency_key':'local-test-qa-redeem'}
        assert call(app, 'POST', '/v1/redemptions', request, token)['body']['added_words'] == 10000
        assert call(app, 'POST', '/v1/redemptions', request, token)['body']['replayed'] is True
        text = '测试配音。'
        batch = call(app, 'POST', '/v1/batches', {'idempotency_key':'local-test-qa-batch', 'items':[
            {'key':'a'*32,'voice':LOCAL_VOICE_ID,'text':text,'speed':1,'purpose':'本地测试'}]}, token)['body']
        assert call(app, 'GET', '/v1/me', token=token)['body']['reserved_words'] == len(text)
        route = f'/v1/batches/{batch["id"]}/jobs/{"a"*32}/start'
        result = call(app, 'POST', route, {}, token)['body']
        assert result['state'] == 'succeeded'
        assert call(app, 'POST', route, {}, token)['body']['id'] == result['id']
        assert call(app, 'GET', f'/v1/jobs/{result["id"]}/audio', token=token)['body'] == _pcm_wav()
        balance = call(app, 'GET', '/v1/me', token=token)['body']
        assert (balance['available_words'],balance['used_words'],balance['reserved_words']) == (10000-len(text),len(text),0)
        assert call(app,'GET','/v1/usage',token=token)['body']['total'] == 2
        with runtime.store.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM redemption_codes WHERE redeemed_by IS NULL').fetchone()[0] == 2
        assert len(calls) == 1
    finally:
        runtime.close()


def test_server_descriptor_reports_bound_loopback_ephemeral_port_and_cleans_up(tmp_path):
    from yijianjifa_cloud_voice.local_test import create_local_test, create_local_server
    runtime = create_local_test(tmp_path / 'server', ffmpeg_path=sys.executable, say_path=sys.executable)
    try:
        with create_local_server(runtime, port=0) as server:
            info = json.loads(runtime.service_file.read_text())
            assert server.server_address[0] == '127.0.0.1'
            assert info['kind'] == 'yjjf-local-cloud-voice-test-v1'
            assert info['local_test'] is True
            assert info['endpoint'] == f'http://127.0.0.1:{server.server_port}'
            assert info['pid'] == os.getpid()
            assert os.stat(runtime.service_file).st_mode & 0o777 == 0o600
        assert not runtime.service_file.exists()
    finally:
        runtime.close()


def test_local_service_rejects_second_instance_and_packaged_runtime(tmp_path, monkeypatch):
    from yijianjifa_cloud_voice.local_test import create_local_test
    target = tmp_path / 'single-instance'
    runtime = create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
    original_codes = runtime.codes_file.read_bytes()
    try:
        with pytest.raises(RuntimeError, match='已由另一服务进程使用'):
            create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
        assert runtime.codes_file.read_bytes() == original_codes
    finally:
        runtime.close()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    with pytest.raises(RuntimeError, match='仅供源码'):
        create_local_test(tmp_path / 'packaged', ffmpeg_path=sys.executable, say_path=sys.executable)
    assert not (tmp_path / 'packaged').exists()


def test_local_service_rejects_tampered_marker_and_linked_database(tmp_path):
    from yijianjifa_cloud_voice.local_test import create_local_test
    target = tmp_path / 'guard'
    runtime = create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
    runtime.close()
    database = target / 'voice.db'
    database.unlink()
    protected = tmp_path / 'protected.db'
    protected.write_bytes(b'unchanged')
    database.symlink_to(protected)
    with pytest.raises(ValueError, match='符号链接'):
        create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
    assert protected.read_bytes() == b'unchanged'
    database.unlink()
    (target / 'local-test.json').write_text('{"local_test":false}')
    with pytest.raises(ValueError, match='专用测试目录'):
        create_local_test(target, ffmpeg_path=sys.executable, say_path=sys.executable)
