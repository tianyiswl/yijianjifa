import io
import os
from contextlib import contextmanager
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess
from yijianjifa.features.mixing.tts.hosted import HostedTTS, VoicePaused
from yijianjifa.features.mixing.queue import MixQueue


class Vault:
    def __init__(self): self.values = {}
    def get_password(self, service, key): return self.values.get((service, key))
    def set_password(self, service, key, value): self.values[service, key] = value
    def delete_password(self, service, key): self.values.pop((service, key), None)


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.headers = {'Content-Type': 'audio/mpeg' if isinstance(value, bytes) else 'application/json'}


def paths(tmp_path): return SimpleNamespace(data_dir=tmp_path, cache_dir=tmp_path / 'cache')


def client(tmp_path, opener):
    access = CloudVoiceAccess(paths(tmp_path), endpoint='http://127.0.0.1:12345', opener=opener, vault=Vault())
    access._save_session(dict(account_id='account-a', username='tester', access_token='ACCESS-SECRET', refresh_token='REFRESH-SECRET'))
    return access


def test_unconfigured_cloud_is_disabled_and_never_requests(tmp_path):
    opener = Mock()
    service = CloudVoiceAccess(paths(tmp_path), endpoint='', opener=opener, vault=Vault())
    status = service.read()
    assert status['enabled'] is False and status['authenticated'] is False
    assert status['pack'] == {'price_fen': 1990, 'words': 10000}
    opener.assert_not_called()


def test_public_status_and_disk_never_contain_session_tokens(tmp_path):
    service = client(tmp_path, lambda request, **kwargs: Response(dict(account_id='account-a', username='tester', available_words=1234, reserved_words=10, used_words=20)))
    status = service.read()
    assert status['available_words'] == 1234
    assert 'SECRET' not in json.dumps(status)
    assert all('SECRET' not in p.read_text() for p in tmp_path.rglob('*.json'))


def test_redemption_reuses_durable_key_after_lost_response(tmp_path):
    calls = []
    def opener(request, **kwargs):
        calls.append(json.loads(request.data))
        if len(calls) == 1: raise URLError('transport lost')
        return Response(dict(added_words=10000, available_words=10000))
    service = client(tmp_path, opener)
    with pytest.raises(VoicePaused): service.redeem('ABCD-EFGH-IJKL-MNOP')
    service.redeem('ABCD-EFGH-IJKL-MNOP')
    assert calls[0]['idempotency_key'] == calls[1]['idempotency_key']
    assert 'ABCD-EFGH' not in ''.join(p.read_text() for p in tmp_path.rglob('*.json'))


def test_remote_insecure_endpoint_rejected(tmp_path):
    with pytest.raises(ValueError, match='HTTPS'):
        CloudVoiceAccess(paths(tmp_path), endpoint='http://example.com', vault=Vault())


def test_recovery_file_is_private_and_no_session_secret(tmp_path):
    def opener(request, **kwargs): return Response(dict(account_id='account-a', recovery_secret='RECOVERY-SECRET'))
    service = client(tmp_path, opener)
    target = tmp_path / 'recovery.json'
    assert service.export_recovery('long-password', target) == {'exported': True}
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o600
    assert 'ACCESS-SECRET' not in target.read_text()
    with pytest.raises(ValueError): service.export_recovery('long-password', target)


def test_batch_plan_counts_actual_unicode_sentences_and_deduplicates(tmp_path):
    service = client(tmp_path, Mock())
    adapter = HostedTTS(service, tmp_path / 'cache')
    frozen = adapter.freeze(dict(copies=[{'voice': '  中文A😀！\n 重复。 '}, {'voice': '重复。'}], options={'voice': 'doubao:test', 'speed': 1}))
    assert frozen['account_id'] == 'account-a'
    items = adapter.plan_items(dict(copies=[{'voice': '  中文A😀！\n 重复。 '}, {'voice': '重复。'}], options={'voice': 'doubao:test', 'speed': 1}))
    assert sum(len(item['text']) for item in items) == 8
    assert len(items) == 2 and all(len(item['key']) == 32 for item in items)


def test_generation_response_loss_only_queries_original_job(tmp_path):
    calls = []
    def opener(request, **kwargs):
        calls.append((request.method, request.full_url))
        if request.full_url.endswith('/v1/batches'):
            key = json.loads(request.data)['items'][0]['key']
            return Response(dict(id='batch-1', jobs=[dict(key=key, id='job-1', state='pending', words=2)]))
        if request.full_url.endswith('/start'): raise URLError('response lost')
        if request.full_url.endswith('/audio'): return Response(b'valid-audio')
        if request.full_url.endswith('/job-1'): return Response(dict(id='job-1', state='succeeded', duration_ms=1000))
        if request.full_url.endswith('/lease'): return Response({})
        raise AssertionError(request.full_url)
    service = client(tmp_path, opener)
    adapter = HostedTTS(service, tmp_path / 'cache', probe=lambda path: 1000)
    result = adapter.synthesize('测试', 'doubao:test', 1, tmp_path / 'out.aiff')
    assert result.duration_ms == 1000
    assert sum(url.endswith('/start') for _, url in calls) == 1
    assert sum(url.endswith('/job-1') for _, url in calls) == 1


def test_continuation_requires_original_account_without_network(tmp_path):
    opener = Mock()
    service = client(tmp_path, opener)
    adapter = HostedTTS(service, tmp_path / 'cache')
    snapshot = dict(copies=[{'voice': '测试'}], options={'voice': 'doubao:test'}, voice_identity={'source': 'hosted', 'account_id': 'other-account'})
    with pytest.raises(VoicePaused, match='原配音账号'):
        adapter.prepare_batch('task-1', snapshot)
    opener.assert_not_called()


def test_voice_pause_preserves_current_item_and_all_counts(db_engine):
    executor = Mock()
    executor.render_item.side_effect = VoicePaused('insufficient_credit', '本批需要 20 字，剩余 10 字。')
    queue = MixQueue(db_engine, executor)
    task = queue.enqueue({'quantity': 3, 'planned_publish_times': [100, 200, 300]})
    queue.run_once_until_idle()
    record = queue.get(task.id)
    assert record.state == 'paused'
    assert (record.current_index, record.completed_count, record.failure_count, record.success_count) == (0, 0, 0, 0)
    assert record.planned_publish_times == [100, 200, 300]
    assert record.last_error == '本批需要 20 字，剩余 10 字。'
    assert executor.render_item.call_count == 1


def test_whole_batch_insufficient_before_any_material_probe(db_engine, app_paths):
    from yijianjifa.features.mixing.desktop_service import DesktopMixService
    seen = []
    class Adapter:
        @contextmanager
        def batch_context(self, task_id, snapshot):
            seen.extend(copy['voice'] for copy in snapshot['copies'])
            raise VoicePaused('insufficient_credit', '本批需要 30 字，剩余 10 字。')
            yield self
        def release_batch(self, *args): pass
    probe = Mock()
    service = DesktopMixService(db_engine, app_paths, tts=Adapter(), probe=probe)
    task = service.queue.enqueue(dict(quantity=3, copies=[{'voice': text} for text in ['第一条。', '第二条。', '第三条。']]))
    service.queue.run_once_until_idle()
    record = service.queue.get(task.id)
    assert seen == ['第一条。', '第二条。', '第三条。']
    probe.assert_not_called()
    assert (record.state, record.current_index, record.failure_count) == ('paused', 0, 0)


def test_source_change_cannot_reroute_existing_batch(tmp_path):
    from yijianjifa.features.mixing.tts.voice_services import VoiceSourceConfig, VoiceSourceTTS
    service = client(tmp_path, lambda request, **kwargs: Response({'voices': [{'id': 'doubao:test', 'name': '测试'}]}))
    own_config = Mock(); own_config.status.return_value = {'configured': True, 'app_id': '123'}
    source = VoiceSourceTTS(VoiceSourceConfig(own_config, service), Mock(), HostedTTS(service, tmp_path / 'cache'), Mock())
    snapshot = dict(copies=[{'voice': '测试。'}], options={'voice': 'doubao:test'})
    snapshot['voice_identity'] = source.freeze(snapshot)
    # Historical releases could persist this source; new releases cannot select it.
    service.mode_path.write_text(json.dumps({'mode': 'own'}))
    with pytest.raises(VoicePaused, match='恢复原批次'):
        with source.batch_context('batch-source-change', snapshot): pass
    source.own.synthesize.assert_not_called()
    source.system.synthesize.assert_not_called()


def test_expired_cloud_does_not_hide_bundled_samples(tmp_path, synthetic_mp3):
    from yijianjifa.features.mixing.tts.voice_services import VoiceSourceConfig
    from yijianjifa.app_services.voice_catalog import VoiceCatalog
    service = client(tmp_path, Mock(side_effect=URLError('offline')))
    catalog = VoiceCatalog(paths(tmp_path), SimpleNamespace(registry=Mock(), mime_types={}), VoiceSourceConfig(Mock(), service))
    from yijianjifa.features.mixing.tts.volcengine import MANIFEST
    catalog.samples = tmp_path/'local-synthetic-samples'
    sample = catalog.samples/next(item['sample_file'] for item in MANIFEST if item.get('sample_file'))
    sample.parent.mkdir(parents=True)
    sample.write_bytes(synthetic_mp3.read_bytes())
    result = catalog.read()
    assert result['items']
    assert not any(item['available'] for item in result['items'])
    assert any(item['standard_sample'] for item in result['items'])


def test_native_resume_authorizes_retry_but_background_tick_does_not(db_engine):
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    executor = Mock()
    executor.render_item.side_effect = VoicePaused('job_unknown', '原结果待确认。')
    queue = MixQueue(db_engine, executor)
    task = queue.enqueue({'quantity': 2})
    queue.run_once_until_idle()
    queue.run_once_until_idle()
    with session_scope(db_engine) as session:
        assert session.get(Setting, 'mix-voice-retry:' + task.id) is None
    queue.resume(task.id)
    with session_scope(db_engine) as session:
        value = json.loads(session.get(Setting, 'mix-voice-retry:' + task.id).value_json)
        assert len(value['authorization']) == 32
    assert queue.get(task.id).current_index == 0


def test_explicit_audition_retries_known_failure_once_and_keeps_job_identity(tmp_path):
    starts, retries, job_key = [], [], []
    state = {'value': 'pending'}
    def job(): return dict(id='original-job', state=state['value'], duration_ms=1000, words=2)
    def batch(): return dict(id='original-batch', jobs=[dict(key=job_key[0], **job())])
    def opener(request, **kwargs):
        path = request.full_url
        if path.endswith('/v1/batches'):
            job_key.append(json.loads(request.data)['items'][0]['key'])
            return Response(batch())
        if path.endswith('/lease'): return Response({})
        if path.endswith('/original-batch'): return Response(batch())
        if path.endswith('/retry'):
            retries.append(json.loads(request.data)['idempotency_key'])
            state['value'] = 'pending'
            return Response(job())
        if path.endswith('/start'):
            starts.append(path)
            state['value'] = 'failed' if len(starts) == 1 else 'succeeded'
            return Response(job())
        if path.endswith('/audio'): return Response(b'valid-audio')
        if path.endswith('/original-job'): return Response(job())
        raise AssertionError(path)
    adapter = HostedTTS(client(tmp_path, opener), tmp_path / 'cache', probe=lambda path: 1000)
    with pytest.raises(VoicePaused, match='未成功'):
        adapter.synthesize('测试', 'doubao:test', 1, tmp_path / 'first.aiff')
    assert len(starts) == 1 and retries == []
    assert adapter.synthesize('测试', 'doubao:test', 1, tmp_path / 'second.aiff').duration_ms == 1000
    assert len(starts) == 2 and len(retries) == 1
    assert adapter.synthesize('测试', 'doubao:test', 1, tmp_path / 'third.aiff').duration_ms == 1000
    assert len(starts) == 2 and len(retries) == 1


def test_parallel_expired_requests_refresh_once_and_retain_rotated_session(tmp_path):
    refreshes, headers = [], []
    def opener(request, **kwargs):
        authorization = request.get_header('Authorization')
        if request.full_url.endswith('/refresh'):
            refreshes.append(json.loads(request.data)['refresh_token'])
            time.sleep(.03)
            return Response(dict(account_id='account-a', username='tester', access_token='ROTATED-ACCESS', refresh_token='ROTATED-REFRESH'))
        if authorization == 'Bearer ACCESS-SECRET':
            raise HTTPError(request.full_url, 401, 'expired', {}, io.BytesIO(b'{"error":{"code":"session_expired"}}'))
        headers.append(authorization)
        if request.full_url.endswith('/voices'):
            return Response({'voices': [{'id': 'doubao:test', 'name': '测试'}]})
        return Response(dict(available_words=10000, reserved_words=0, used_words=0))
    service = client(tmp_path, opener)
    start, errors = threading.Barrier(2), []
    def run(fn):
        try:
            start.wait(timeout=2)
            fn()
        except Exception as error: errors.append(error)
    threads = [threading.Thread(target=run, args=(fn,)) for fn in (service.read, service.available_voices)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=3)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert refreshes == ['REFRESH-SECRET']
    assert headers == ['Bearer ROTATED-ACCESS', 'Bearer ROTATED-ACCESS']
    assert service._session()['refresh_token'] == 'ROTATED-REFRESH'


def test_broken_local_audio_refetches_original_success_without_regeneration(tmp_path):
    calls, key = [], []
    def job(): return dict(id='cached-job', state='succeeded', duration_ms=1000)
    def batch(): return dict(id='cached-batch', jobs=[dict(key=key[0], **job())])
    def opener(request, **kwargs):
        path = request.full_url
        calls.append(path)
        if path.endswith('/v1/batches'):
            key.append(json.loads(request.data)['items'][0]['key'])
            return Response(batch())
        if path.endswith('/lease'): return Response({})
        if path.endswith('/cached-batch'): return Response(batch())
        if path.endswith('/cached-job'): return Response(job())
        if path.endswith('/audio'): return Response(b'valid-audio')
        raise AssertionError(path)
    def probe(path):
        if path.read_bytes() == b'broken-local-cache':
            raise subprocess.CalledProcessError(1, ['ffprobe'], stderr=b'private local file path')
        return 1000
    adapter = HostedTTS(client(tmp_path, opener), tmp_path / 'cache', probe=probe)
    adapter.synthesize('测试', 'doubao:test', 1, tmp_path / 'first.aiff')
    cached = next(adapter.cache.rglob('*.mp3'))
    cached.write_bytes(b'broken-local-cache')
    result = adapter.synthesize('测试', 'doubao:test', 1, tmp_path / 'refetched.aiff')
    assert result.duration_ms == 1000 and result.output_path.read_bytes() == b'valid-audio'
    assert sum(path.endswith('/audio') for path in calls) == 2
    assert not any(path.endswith('/start') or path.endswith('/retry') for path in calls)


def test_wrong_password_does_not_refresh_or_discard_valid_session(tmp_path):
    calls = []
    def opener(request, **kwargs):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 401, 'incorrect password', {}, io.BytesIO(b'{"error":{"code":"invalid_credentials"}}'))
    service = client(tmp_path, opener)
    with pytest.raises(VoicePaused, match='密码不正确'):
        service.change_password('wrong-password', 'new-password')
    assert len(calls) == 1 and calls[0].endswith('/password')
    assert service._session()['access_token'] == 'ACCESS-SECRET'


@pytest.mark.parametrize(('code', 'expected'), [
    ('account_unavailable', '换一个账号名'),
    ('invalid_username', '4—32'),
    ('invalid_password', '6—128'),
    ('voice_unavailable', '声音暂不可用'),
    ('supplier_budget_exceeded', '本段未扣字数'),
    ('service_busy', '正在处理其他任务'),
    ('retry_not_ready', '查询原结果'),
    ('batch_released', '预留已释放'),
    ('request_too_large', '减少本批'),
])
def test_server_error_codes_have_specific_safe_customer_messages(tmp_path, code, expected):
    raw = json.dumps({'error': {'code': code, 'message': 'PRIVATE-SUPPLIER-SECRET / original text', 'details': {}}}).encode()
    def opener(request, **kwargs):
        raise HTTPError(request.full_url, 409, 'controlled error', {}, io.BytesIO(raw))
    service = client(tmp_path, opener)
    with pytest.raises(VoicePaused) as error:
        service._request('GET', '/v1/me')
    assert error.value.code == code
    assert expected in str(error.value)
    assert 'PRIVATE' not in str(error.value) and 'original text' not in str(error.value)
    assert '检查账号和服务状态' not in str(error.value)


def test_reopening_restores_native_session_without_password_login(tmp_path):
    vault, calls = Vault(), []
    def opener(request, **kwargs):
        calls.append(request.full_url)
        assert request.get_header('Authorization') == 'Bearer ACCESS-SECRET'
        return Response(dict(account_id='account-a', username='tester', available_words=1234, reserved_words=0, used_words=0))
    first = CloudVoiceAccess(paths(tmp_path), endpoint='http://127.0.0.1:12345', opener=opener, vault=vault)
    first._save_session(dict(account_id='account-a', username='tester', access_token='ACCESS-SECRET', refresh_token='REFRESH-SECRET'))
    reopened = CloudVoiceAccess(paths(tmp_path), endpoint='http://127.0.0.1:12345', opener=opener, vault=vault)
    assert reopened.read()['authenticated'] is True
    assert calls == ['http://127.0.0.1:12345/v1/me']
    reopened.logout()
    again = CloudVoiceAccess(paths(tmp_path), endpoint='http://127.0.0.1:12345', opener=opener, vault=vault)
    assert again.read()['authenticated'] is False


def test_first_use_policy_is_versioned_and_account_scoped(tmp_path):
    service=client(tmp_path,lambda request,**kwargs:Response(dict(available_words=10,used_words=0,reserved_words=0)))
    assert service.read()['policy_notice_required'] is True
    service.policy_seen()
    reopened=CloudVoiceAccess(paths(tmp_path),endpoint=service.endpoint,opener=service.opener,vault=service.vault)
    assert reopened.read()['policy_notice_required'] is False
    reopened._save_session(dict(account_id='different',username='other',access_token='OTHER',refresh_token='OTHER-R'))
    assert reopened.read()['policy_notice_required'] is True


def test_source_default_makes_operator_service_available_without_synthesis(tmp_path, monkeypatch):
    from yijianjifa.app_services.cloud_voice_access import CLOUD_VOICE_ENDPOINT
    monkeypatch.delenv('P011_CLOUD_VOICE_ENDPOINT', raising=False)
    assert CLOUD_VOICE_ENDPOINT == 'https://39.108.82.213'
    service = CloudVoiceAccess(paths(tmp_path), opener=Mock(), vault=Vault())
    assert service.endpoint == CLOUD_VOICE_ENDPOINT
    assert service.read()['enabled'] is True
    service.opener.assert_not_called()
