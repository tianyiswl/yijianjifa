"""No live requests: supplier task identity and direct-download boundaries."""
import io
import json
import urllib.error

import pytest

from yijianjifa_cloud_voice.provider import ProviderFailure, ProviderUnknown
from yijianjifa_cloud_voice.provider_async import (
    AsyncVolcengineProvider, MAX_JSON_BYTES, validate_supplier_url,
)


TASK = '4d0ddf16a94c4d5fbb8c29a920f011b3'
URL = 'https://lf26-lab-speech-tt-sign.bytespeech.com/audio/test.mp3?signature=test-only'


@pytest.fixture(autouse=True)
def isolated_operator_configuration(monkeypatch):
    for name in ('YJJF_VOLC_API_KEY', 'YJJF_VOLC_APP_ID', 'YJJF_VOLC_ACCESS_KEY',
                 'YJJF_CLOUD_AUDIO_DIR', 'YJJF_CLOUD_VOICES_FILE',
                 'YJJF_CLOUD_MAX_DAILY_WORDS', 'YJJF_CLOUD_TRUSTED_PROXIES'):
        monkeypatch.delenv(name, raising=False)


def response(data=None, *, code=20000000, task=TASK, state=1, **fields):
    if data is None:
        data = {'task_id': task, 'task_status': state, **fields}
    return io.BytesIO(json.dumps({'code': code, 'data': data}).encode())


def provider(opener, **kwargs):
    return AsyncVolcengineProvider('operator-app', 'operator-secret', max_daily_words=10000,
                                  opener=opener, **kwargs)


def test_submit_keeps_unique_id_fixed_endpoint_and_server_headers():
    calls = []
    def opener(request, timeout):
        calls.append((request, timeout))
        return response()
    speech = provider(opener, timeout=15)
    assert speech.submit('你好', 'doubao:test', 1.25, TASK) == {'task_id': TASK, 'state': 'running'}
    request, timeout = calls[0]
    assert request.full_url == 'https://openspeech.bytedance.com/api/v3/tts/submit'
    assert timeout == 15
    headers = dict((key.lower(), value) for key, value in request.header_items())
    assert headers['x-api-app-id'] == 'operator-app'
    assert headers['x-api-access-key'] == 'operator-secret'
    assert 'x-api-key' not in headers
    assert headers['x-api-resource-id'] == 'seed-tts-2.0'
    assert headers['x-api-request-id'] == TASK
    body = json.loads(request.data)
    assert body['unique_id'] == TASK
    assert body['req_params']['text'] == '你好'
    assert body['req_params']['audio_params'] == {
        'format': 'mp3', 'sample_rate': 24000, 'speech_rate': 25, 'enable_timestamp': True}
    assert 'operator-secret' not in repr(speech)
    assert speech.delivery_mode == 'direct'
    assert speech.version == 'seed-tts-2.0-unicode-codepoint-v1'


def test_query_never_submits_and_maps_running_success_failure_and_expired():
    calls = []
    replies = [response(), response(state=2, audio_url=URL, url_expire_time=2000,
                                   sentences=[{'endTime': 1.25}, {'endTime': 2.215}]),
               response(state=3), response(state=2, audio_url=URL, url_expire_time=500)]
    def opener(request, timeout):
        calls.append(request)
        return replies.pop(0)
    speech = provider(opener, clock=lambda: 1000)
    assert speech.query(TASK) == {'task_id': TASK, 'state': 'running'}
    assert speech.query(TASK) == {'task_id': TASK, 'state': 'succeeded', 'url': URL,
                                  'expires_at': 2000.0, 'duration_ms': 2215}
    assert speech.query(TASK) == {'task_id': TASK, 'state': 'failed'}
    assert speech.query(TASK) == {'task_id': TASK, 'state': 'expired',
                                  'expires_at': 500.0, 'duration_ms': None}
    assert all(item.full_url == 'https://openspeech.bytedance.com/api/v3/tts/query' for item in calls)
    assert all(json.loads(item.data) == {'task_id': TASK} for item in calls)


@pytest.mark.parametrize('bad_url', [
    'http://lf26-lab-speech-tt-sign.bytespeech.com/a.mp3',
    'https://bytespeech.com.attacker.test/a.mp3', 'https://attackerbytespeech.com/a.mp3',
    'https://user:pass@bytespeech.com/a.mp3', 'https://bytespeech.com:444/a.mp3',
    'https://bytespeech.com/a.mp3#secret', 'https://127.0.0.1/a.mp3',
    'https://[::1]/a.mp3', 'https://bytespeech.com./a.mp3',
    'https://bytespeech.com%2eattacker.test/a.mp3', 'https://bytespeech.com/a%GG.mp3',
    'https://bytespeech.com/a\\b', 'https://bytespeech.com/a\n',
    'https://bytetos.com:443@attacker.test/a.mp3', 'https://bytespeech.com/a b.mp3',
    'https://bytespeech.com:/a.mp3', 'https://bytespeech.com/a%00.mp3',
    'https://bytespeech.com/a%0A.mp3', 'https://bytespeech.com/a%5Cb.mp3',
    '', None,
])
def test_supplier_url_rejects_non_supplier_or_ambiguous_addresses(bad_url):
    with pytest.raises(ProviderUnknown, match='invalid_supplier_url'):
        validate_supplier_url(bad_url)


@pytest.mark.parametrize('url', [URL, 'https://bucket.bytetos.com/a%20b.mp3?x=a%2Fb',
                              'https://bytespeech.com:443/a.mp3'])
def test_supplier_url_accepts_only_explicit_official_domains(url):
    assert validate_supplier_url(url) == url


@pytest.mark.parametrize('raw', [b'bad-json', b'[]', b'{"code":20000000}',
                               b'x' * (1024 * 1024 + 1)])
def test_malformed_or_oversized_reply_stays_unknown_and_sanitized(raw):
    speech = provider(lambda *args, **kwargs: io.BytesIO(raw))
    with pytest.raises(ProviderUnknown) as error:
        speech.submit('private full input', 'doubao:test', 1.0, TASK)
    assert 'private full input' not in str(error.value)
    assert 'operator-secret' not in str(error.value)


def test_reads_at_most_one_mebibyte_plus_one_byte():
    sizes = []
    class Bounded(io.BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)
    speech = provider(lambda *args, **kwargs: Bounded(b'x' * (MAX_JSON_BYTES + 1)))
    with pytest.raises(ProviderUnknown):
        speech.query(TASK)
    assert sizes == [MAX_JSON_BYTES + 1]


@pytest.mark.parametrize('action', ['submit', 'query'])
def test_mismatched_task_id_cannot_settle_another_task(action):
    speech = provider(lambda *args, **kwargs: response(task='a' * 32, state=2,
                                                      audio_url=URL, url_expire_time=2000))
    with pytest.raises(ProviderUnknown, match='provider_task_mismatch'):
        speech.submit('你好', 'doubao:test', 1, TASK) if action == 'submit' else speech.query(TASK)


def test_submission_timeout_is_not_an_explicit_failure_or_automatic_retry():
    calls = []
    def opener(request, timeout):
        calls.append(request)
        raise TimeoutError('operator-secret private full input')
    speech = provider(opener)
    with pytest.raises(ProviderUnknown) as error:
        speech.submit('private full input', 'doubao:test', 1, TASK)
    assert len(calls) == 1
    assert str(error.value) == 'provider_result_unknown'


@pytest.mark.parametrize('status', [400, 404, 429, 500, 503, 302])
def test_ambiguous_submission_http_status_never_implies_safe_resubmit(status):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, 'private data', {}, None)
    with pytest.raises(ProviderUnknown):
        provider(opener).submit('你好', 'doubao:test', 1, TASK)


@pytest.mark.parametrize('status', [401, 403, 413])
def test_explicit_submission_rejection_is_failure(status):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, 'private data', {}, None)
    with pytest.raises(ProviderFailure, match='provider_rejected'):
        provider(opener).submit('你好', 'doubao:test', 1, TASK)


@pytest.mark.parametrize('status', [400, 401, 403, 404, 410, 429, 503])
def test_query_error_does_not_turn_missing_or_expired_task_into_new_generation(status):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, 'private data', {}, None)
    with pytest.raises(ProviderUnknown):
        provider(opener).query(TASK)


@pytest.mark.parametrize('code', [45000001, 55000000, 404, '20000000', None])
def test_unrecognized_supplier_codes_stay_unknown(code):
    speech = provider(lambda *args, **kwargs: response(code=code))
    with pytest.raises(ProviderUnknown):
        speech.query(TASK)


def test_query_does_not_publish_unsafe_or_missing_download_metadata():
    for fields in ({'audio_url': 'https://attacker.test/a.mp3', 'url_expire_time': 2000},
                   {'audio_url': URL}, {'audio_url': URL, 'url_expire_time': True},
                   {'audio_url': URL, 'url_expire_time': float('inf')}):
        speech = provider(lambda *args, **kwargs: response(state=2, **fields), clock=lambda: 1000)
        with pytest.raises(ProviderUnknown):
            speech.query(TASK)


def test_default_opener_installs_a_redirect_handler_which_refuses_redirects():
    from yijianjifa_cloud_voice.provider_async import _NoRedirect
    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, 'redirect', {}, 'https://attacker.test') is None
    speech = AsyncVolcengineProvider('app', 'secret', max_daily_words=10)
    assert any(isinstance(item, _NoRedirect) for item in speech.opener.__self__.handlers)
    with pytest.raises(TypeError):
        AsyncVolcengineProvider('app', 'secret', max_daily_words=10,
                                endpoint='https://attacker.test')


@pytest.mark.parametrize('kwargs', [{'max_daily_words': 0}, {'max_daily_words': True},
                                  {'max_daily_words': 100, 'timeout': 0},
                                  {'max_daily_words': 100, 'timeout': float('inf')}])
def test_operator_budget_and_timeout_are_required_and_bounded(kwargs):
    with pytest.raises(ValueError):
        AsyncVolcengineProvider('app', 'secret', **kwargs)


@pytest.mark.parametrize('request_id', ['', 'short', 'a' * 65, 'a' * 31 + '\n'])
def test_invalid_identity_never_reaches_transport(request_id):
    calls = []
    speech = provider(lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ProviderFailure, match='invalid_request'):
        speech.submit('你好', 'doubao:test', 1, request_id)
    assert calls == []


def test_a_transport_redirect_or_duplicate_submission_result_stays_unknown():
    class Redirected(io.BytesIO):
        def geturl(self):
            return 'https://attacker.test/?private-secret'
    speech = provider(lambda *args, **kwargs: Redirected(b'{}'))
    with pytest.raises(ProviderUnknown) as error:
        speech.query(TASK)
    assert 'private-secret' not in str(error.value)
    speech = provider(lambda *args, **kwargs: response(code=45000001))
    with pytest.raises(ProviderUnknown):
        speech.submit('你好', 'doubao:test', 1, TASK)


def test_success_can_omit_timestamps_and_never_returns_supplier_text_or_accounting_fields():
    speech = provider(lambda *args, **kwargs: response(state=2, audio_url=URL,
        url_expire_time=2000, req_text_length=2, synthesize_text_length=2,
        text='private full input', sentences=[{'endTime': float('inf')}]), clock=lambda: 1000)
    assert speech.query(TASK) == {'task_id': TASK, 'state': 'succeeded', 'url': URL,
                                  'expires_at': 2000.0, 'duration_ms': None}


def test_production_factory_uses_async_delivery_without_requiring_audio_directory(tmp_path, monkeypatch):
    from yijianjifa_cloud_voice import wsgi
    config = {'YJJF_CLOUD_DATABASE': str(tmp_path/'voice.db'),
              'YJJF_VOLC_APP_ID': 'operator-app', 'YJJF_VOLC_ACCESS_KEY': 'operator-secret',
              'YJJF_CLOUD_VOICES_FILE': str(tmp_path/'voices.json'),
              'YJJF_CLOUD_MAX_DAILY_WORDS': '1000'}
    (tmp_path/'voices.json').write_text('["doubao:test"]')
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('YJJF_CLOUD_AUDIO_DIR', raising=False)
    captured = {}
    def create_app(store, *, jobs, trusted_proxies):
        captured['jobs'] = jobs
        return object()
    monkeypatch.setattr(wsgi, 'create_app', create_app)
    app = wsgi.create_from_environment()
    try:
        assert isinstance(captured['jobs'].provider, AsyncVolcengineProvider)
        assert captured['jobs'].audio_dir == tmp_path/'audio'
    finally:
        app.close()


@pytest.mark.parametrize('action', ['submit', 'query'])
def test_new_console_key_uses_only_api_key_and_preserves_original_protocol(action):
    calls = []
    def opener(request, timeout):
        calls.append(request)
        return response()
    speech = AsyncVolcengineProvider(api_key='new-console-test-only', max_daily_words=10000,
                                    opener=opener)
    result = (speech.submit('你好', 'doubao:test', 1, TASK) if action == 'submit'
              else speech.query(TASK))
    assert result == {'task_id': TASK, 'state': 'running'}
    assert len(calls) == 1
    request = calls[0]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers['x-api-key'] == 'new-console-test-only'
    assert 'x-api-app-id' not in headers and 'x-api-access-key' not in headers
    assert headers['x-api-resource-id'] == 'seed-tts-2.0'
    assert headers['x-api-request-id'] == TASK
    assert request.full_url == 'https://openspeech.bytedance.com/api/v3/tts/' + action
    body = json.loads(request.data)
    assert body.get('unique_id' if action == 'submit' else 'task_id') == TASK
    assert speech.version == 'seed-tts-2.0-unicode-codepoint-v1'
    assert 'new-console-test-only' not in repr(speech)


@pytest.mark.parametrize('credentials', [
    {}, {'app_id': 'app'}, {'access_key': 'token'},
    {'api_key': ''}, {'api_key': 'private test key'}, {'api_key': 'private\nkey'},
    {'api_key': 123}, {'api_key': 'x' * 4097},
    {'api_key': 'new-key', 'app_id': 'old-app'},
    {'api_key': 'new-key', 'access_key': 'old-token'},
    {'api_key': 'new-key', 'app_id': 'old-app', 'access_key': 'old-token'},
])
def test_authentication_missing_invalid_or_mixed_is_rejected_before_transport(credentials):
    calls = []
    with pytest.raises(ValueError) as error:
        AsyncVolcengineProvider(**credentials, max_daily_words=10000,
                                opener=lambda *args, **kwargs: calls.append(args))
    assert calls == []
    assert 'private' not in str(error.value) and 'old-token' not in str(error.value)


def test_new_key_transport_failure_does_not_leak_credentials_or_retry():
    calls = []
    def opener(request, timeout):
        calls.append(request)
        raise TimeoutError('new-console-test-only private text')
    speech = AsyncVolcengineProvider(api_key='new-console-test-only', max_daily_words=10000,
                                    opener=opener)
    with pytest.raises(ProviderUnknown) as error:
        speech.submit('private text', 'doubao:test', 1, TASK)
    assert str(error.value) == 'provider_result_unknown' and len(calls) == 1


def test_production_factory_accepts_new_console_key_without_legacy_credentials(tmp_path, monkeypatch):
    from yijianjifa_cloud_voice import wsgi
    (tmp_path/'voices.json').write_text('["doubao:test"]')
    for key, value in {'YJJF_CLOUD_DATABASE': str(tmp_path/'voice.db'),
                       'YJJF_VOLC_API_KEY': 'new-console-test-only',
                       'YJJF_CLOUD_VOICES_FILE': str(tmp_path/'voices.json'),
                       'YJJF_CLOUD_MAX_DAILY_WORDS': '1000'}.items():
        monkeypatch.setenv(key, value)
    captured = {}
    def create_app(store, *, jobs, trusted_proxies):
        captured['jobs'] = jobs
        return object()
    monkeypatch.setattr(wsgi, 'create_app', create_app)
    app = wsgi.create_from_environment()
    try:
        calls = []
        def opener(request, timeout):
            calls.append(request)
            return response()
        speech = captured['jobs'].provider
        speech.opener = opener
        assert speech.submit('你好', 'doubao:test', 1, TASK)['task_id'] == TASK
        headers = {key.lower(): value for key, value in calls[0].header_items()}
        assert headers['x-api-key'] == 'new-console-test-only'
        assert 'x-api-app-id' not in headers and 'x-api-access-key' not in headers
    finally:
        app.close()


@pytest.mark.parametrize('overrides', [
    {'YJJF_VOLC_API_KEY': 'new-key', 'YJJF_VOLC_APP_ID': 'old-app'},
    {'YJJF_VOLC_API_KEY': 'new-key', 'YJJF_VOLC_ACCESS_KEY': 'old-token'},
    {'YJJF_VOLC_API_KEY': 'new-key', 'YJJF_VOLC_APP_ID': 'old-app',
     'YJJF_VOLC_ACCESS_KEY': 'old-token'},
    {'YJJF_VOLC_APP_ID': 'old-app'}, {'YJJF_VOLC_ACCESS_KEY': 'old-token'},
    {}, {'YJJF_VOLC_API_KEY': 'new-key', 'YJJF_CLOUD_VOICES_FILE': ''},
    {'YJJF_VOLC_API_KEY': 'new-key', 'YJJF_CLOUD_MAX_DAILY_WORDS': ''},
    {'YJJF_VOLC_API_KEY': 'private test key'},
])
def test_factory_rejects_mixed_or_incomplete_auth_without_disclosing_values(tmp_path, monkeypatch, overrides):
    from yijianjifa_cloud_voice.wsgi import create_from_environment
    (tmp_path/'voices.json').write_text('["doubao:test"]')
    configuration = {'YJJF_CLOUD_DATABASE': str(tmp_path/'voice.db'),
                     'YJJF_CLOUD_VOICES_FILE': str(tmp_path/'voices.json'),
                     'YJJF_CLOUD_MAX_DAILY_WORDS': '1000', **overrides}
    for key, value in configuration.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError) as error:
        create_from_environment()
    assert not any(value in str(error.value) for value in ('old-token', 'new-key', 'private test key'))


def test_new_auth_keeps_existing_task_identity_and_single_charge(tmp_path):
    from yijianjifa_cloud_voice.credits import CreditService
    from yijianjifa_cloud_voice.jobs import JobService
    from yijianjifa_cloud_voice.store import Store
    store = Store(tmp_path/'voice.db', clock=lambda: 1000)
    with store.transaction() as connection:
        connection.execute('INSERT INTO accounts(id,username,password_hash,credits,created_at) VALUES (?,?,?,?,?)',
                           ('qa-owner', 'qa_owner', 'unused', 100, 1000))
    calls = []
    def opener(request, timeout):
        calls.append(request)
        body = json.loads(request.data)
        task = body.get('unique_id') or body['task_id']
        return (response(task=task) if request.full_url.endswith('/submit') else
                response(task=task, state=2, audio_url=URL, url_expire_time=2000))
    old = AsyncVolcengineProvider('old-app', 'old-token', max_daily_words=10000,
                                 opener=opener, clock=lambda: 1000)
    payload = {'idempotency_key': 'same-original-batch', 'items':[
        {'key': 'a' * 32, 'text': '你好', 'voice': 'doubao:test', 'speed': 1, 'purpose': '隔离兼容测试'}]}
    jobs = JobService(store, tmp_path/'audio', old, ['doubao:test'])
    batch = jobs.prepare('qa-owner', payload)
    original = jobs.start('qa-owner', batch['id'], 'a' * 32)
    assert original['state'] == 'succeeded'
    new = AsyncVolcengineProvider(api_key='new-console-test-only', max_daily_words=10000,
                                 opener=opener, clock=lambda: 1000)
    recovered = JobService(store, tmp_path/'audio', new, ['doubao:test'])
    assert recovered.prepare('qa-owner', payload)['id'] == batch['id']
    assert recovered.start('qa-owner', batch['id'], 'a' * 32)['id'] == original['id']
    assert recovered.download('qa-owner', original['id'])['job_id'] == original['id']
    assert sum(request.full_url.endswith('/submit') for request in calls) == 1
    assert CreditService(store).balance('qa-owner')['used_words'] == 2
    with store.connect() as connection:
        row = connection.execute('SELECT request_id,provider_task_id FROM voice_jobs').fetchone()
        assert row['request_id'] == row['provider_task_id'] == json.loads(calls[0].data)['unique_id']
        assert connection.execute("SELECT COUNT(*) FROM ledger WHERE kind='generation'").fetchone()[0] == 1
    assert not list((tmp_path/'audio').iterdir())
