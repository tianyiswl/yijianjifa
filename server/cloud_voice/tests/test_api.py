import io
import json

import pytest

from yijianjifa_cloud_voice.api import create_app
from yijianjifa_cloud_voice.auth import AuthService
from yijianjifa_cloud_voice.credits import CreditService
from yijianjifa_cloud_voice.store import Store


def call(app, method, path, body=None, token=None, **extra):
    raw = json.dumps(body).encode() if body is not None else b''
    env = {'REQUEST_METHOD':method, 'PATH_INFO':path, 'QUERY_STRING':'',
           'CONTENT_LENGTH':str(len(raw)), 'CONTENT_TYPE':'application/json',
           'wsgi.input':io.BytesIO(raw), 'REMOTE_ADDR':'127.0.0.1', **extra}
    if token:
        env['HTTP_AUTHORIZATION'] = f'Bearer {token}'
    response = {}
    def start(status, headers):
        response['status'] = int(status.split()[0]); response['headers'] = dict(headers)
    data = b''.join(app(env, start))
    response['body'] = json.loads(data) if response['headers'].get('Content-Type') == 'application/json; charset=utf-8' else data
    return response


@pytest.fixture
def app_state(tmp_path):
    store = Store(tmp_path/'api.db')
    auth = AuthService(store)
    credits = CreditService(store)
    return create_app(store, auth=auth, credits=credits), credits


def test_api_register_redeem_balance_and_forbid_client_amount(app_state):
    app, credits = app_state
    response = call(app, 'POST', '/v1/accounts', {'username':'api_user','password':'safe password 123'})
    assert response['status'] == 201
    token = response['body']['access_token']
    code = credits.issue(1, order_ref='api-isolated-test')[0]['code']
    denied = call(app,'POST','/v1/redemptions',{'code':code,'idempotency_key':'redeem-request-01','words':900000},token)
    assert denied['status'] == 400
    result = call(app,'POST','/v1/redemptions',{'code':code,'idempotency_key':'redeem-request-01'},token)
    assert result['body']['added_words'] == 10000
    balance = call(app,'GET','/v1/me',token=token)
    assert balance['body']['available_words'] == 10000
    assert balance['body']['pack'] == {'price_fen':1990,'words':10000}
    assert 'Access-Control-Allow-Origin' not in balance['headers']
    assert balance['headers']['Cache-Control'] == 'no-store'


def test_api_requires_auth_controls_body_and_sanitizes_errors(app_state):
    app, _ = app_state
    assert call(app,'GET','/v1/me')['status'] == 401
    assert call(app,'GET','/missing')['status'] == 404
    result = call(app,'POST','/v1/accounts',{},CONTENT_LENGTH='9999999')
    assert result['status'] == 413
    origin = call(app,'POST','/v1/accounts',{'username':'web_user','password':'safe password 123'},HTTP_ORIGIN='https://untrusted.example')
    assert origin['status'] == 403


def test_generation_routes_delegate_only_after_auth(tmp_path):
    class Jobs:
        def allowed_voices(self): return [{'id':'doubao:test'}]
        def prepare(self, account, payload): return {'id':'batch-1','owner':account,'payload':payload}
        def audio(self, account, job): return b'fake-audio','audio/mpeg'
    store = Store(tmp_path/'jobs.db')
    app = create_app(store, jobs=Jobs())
    token = call(app,'POST','/v1/accounts',{'username':'job_user','password':'safe password 123'})['body']['access_token']
    assert call(app,'POST','/v1/batches',{'idempotency_key':'batch-request-01','items':[]},token)['body']['id'] == 'batch-1'
    assert call(app,'GET','/v1/jobs/job-1/audio',token=token)['body'] == b'fake-audio'
    assert call(app,'GET','/v1/voices',token=token)['body']['voices'] == [{'id':'doubao:test'}]


def test_login_rate_limit_is_source_bound(app_state):
    app, _ = app_state
    statuses = [call(app,'POST','/v1/sessions',{'username':'nobody','password':'bad password 123'})['status'] for _ in range(12)]
    assert 429 in statuses


def test_generation_error_does_not_disclose_input_or_provider_exception(tmp_path):
    class Jobs:
        def prepare(self,account,payload):
            raise RuntimeError('private supplier secret / full submitted text')
    app=create_app(Store(tmp_path/'failure.db'),jobs=Jobs())
    token=call(app,'POST','/v1/accounts',{'username':'private_user','password':'safe password 123'})['body']['access_token']
    response=call(app,'POST','/v1/batches',{'idempotency_key':'batch-request-01','items':[]},token)
    assert response['status']==500
    assert 'private supplier secret' not in json.dumps(response['body'])


def test_environment_requires_complete_supplier_configuration(tmp_path,monkeypatch):
    from yijianjifa_cloud_voice.wsgi import create_from_environment
    for name in ('YJJF_CLOUD_DATABASE','YJJF_VOLC_API_KEY','YJJF_VOLC_APP_ID','YJJF_VOLC_ACCESS_KEY',
                 'YJJF_CLOUD_AUDIO_DIR','YJJF_CLOUD_VOICES_FILE','YJJF_CLOUD_MAX_DAILY_WORDS'):
        monkeypatch.delenv(name,raising=False)
    with pytest.raises(RuntimeError):
        create_from_environment()
    monkeypatch.setenv('YJJF_CLOUD_DATABASE',str(tmp_path/'env.db'))
    app=create_from_environment()
    token=call(app,'POST','/v1/accounts',{'username':'env_user','password':'safe password 123'})['body']['access_token']
    assert call(app,'GET','/v1/voices',token=token)['body']['error']['code']=='service_not_configured'
    monkeypatch.setenv('YJJF_VOLC_APP_ID','test-only')
    with pytest.raises(RuntimeError):
        create_from_environment()


def test_retry_requires_explicit_request_identity_and_delegates(tmp_path):
    class Jobs:
        def retry(self,account,job,payload): return {'id':job,'request':payload['idempotency_key']}
    app=create_app(Store(tmp_path/'retry.db'),jobs=Jobs())
    token=call(app,'POST','/v1/accounts',{'username':'retry_user','password':'safe password 123'})['body']['access_token']
    assert call(app,'POST','/v1/jobs/job-1/retry',{},token)['status']==400
    assert call(app,'POST','/v1/jobs/job-1/retry',{'idempotency_key':'explicit-request-01'},token)['body']['request']=='explicit-request-01'


def test_complete_http_contract_credits_reservation_generation_and_repeat_download(tmp_path):
    import wave
    from yijianjifa_cloud_voice.jobs import JobService
    class Provider:
        version='isolated-test-v1'
        max_daily_words=10000
        calls=0
        def synthesize(self,text,voice,speed,request_id):
            self.calls+=1
            target=io.BytesIO()
            with wave.open(target,'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
                stream.writeframes(b'\0\0'*800)
            return target.getvalue(),100
    provider=Provider()
    store=Store(tmp_path/'full.db')
    credits=CreditService(store)
    jobs=JobService(store,tmp_path/'audio',provider,[{'id':'doubao:test','name':'测试声音'}])
    app=create_app(store,credits=credits,jobs=jobs)
    session=call(app,'POST','/v1/accounts',{'username':'full_user','password':'safe password 123'})['body']
    token=session['access_token']
    code=credits.issue(1,order_ref='isolated-full-contract')[0]['code']
    recharge={'code':code,'idempotency_key':'recharge-persistent-01'}
    first=call(app,'POST','/v1/redemptions',recharge,token)['body']
    assert call(app,'POST','/v1/redemptions',recharge,token)['body']==dict(first,replayed=True)
    text='你好😀 A1，\n再见。'
    body={'idempotency_key':'frozen-original-batch','items':[
        {'key':'a'*32,'text':text,'voice':'doubao:test','speed':1,'purpose':'制作测试'}]}
    prepared=call(app,'POST','/v1/batches',body,token)['body']
    assert prepared['required_words']==len(text)
    before=call(app,'GET','/v1/me',token=token)['body']
    assert before['reserved_words']==len(text)
    assert before['used_words']==0
    route=f'/v1/batches/{prepared["id"]}/jobs/{"a"*32}/start'
    result=call(app,'POST',route,{},token)['body']
    assert result['state']=='succeeded'
    assert call(app,'POST',route,{},token)['body']['id']==result['id']
    for _ in range(2):
        assert call(app,'GET',f'/v1/jobs/{result["id"]}/audio',token=token)['headers']['Content-Type']=='audio/wav'
    after=call(app,'GET','/v1/me',token=token)['body']
    assert (after['available_words'],after['used_words'],after['reserved_words'])==(10000-len(text),len(text),0)
    assert provider.calls==1
    other=call(app,'POST','/v1/accounts',{'username':'other_user','password':'safe password 123'})['body']['access_token']
    assert call(app,'GET',f'/v1/jobs/{result["id"]}/audio',token=other)['status']==404
    assert call(app,'GET','/v1/usage',token=token)['body']['total']==2


def test_trusted_proxy_uses_strict_real_ip_without_cross_client_limit(tmp_path):
    from yijianjifa_cloud_voice.errors import CloudError
    class Auth:
        def login(self,*args): raise CloudError('invalid_credentials','账号或密码不正确。',401)
    app=create_app(Store(tmp_path/'trusted.db'),auth=Auth(),trusted_proxies=['127.0.0.1'])
    def login(index,source):
        return call(app,'POST','/v1/sessions',{'username':f'name_{source[-1]}_{index}','password':'wrong password'},
                    HTTP_X_REAL_IP=source)['status']
    assert [login(index,'192.0.2.10') for index in range(10)]==[401]*10
    assert [login(index,'192.0.2.20') for index in range(10)]==[401]*10
    assert login(11,'192.0.2.10')==429
    for bad in ('192.0.2.1,192.0.2.2','192.0.2.1:443',' 192.0.2.1','fe80::1%en0','unknown'):
        assert call(app,'GET','/missing',HTTP_X_REAL_IP=bad)['status']==400


def test_untrusted_forwarded_ip_cannot_bypass_source_or_account_limit(tmp_path):
    from yijianjifa_cloud_voice.errors import CloudError
    class Auth:
        def login(self,*args): raise CloudError('invalid_credentials','账号或密码不正确。',401)
        def authenticate(self,*args): return 'same-account'
    app=create_app(Store(tmp_path/'untrusted.db'),auth=Auth())
    statuses=[call(app,'POST','/v1/sessions',{'username':f'name_{index}','password':'wrong password'},
                   REMOTE_ADDR='192.0.2.1',HTTP_X_REAL_IP=f'198.51.100.{index+1}')['status'] for index in range(11)]
    assert statuses==[401]*10+[429]
    # Authenticated account limits remain global across devices and real source addresses.
    statuses=[call(app,'GET','/v1/me',token='any',REMOTE_ADDR=f'198.51.100.{index+1}')['status'] for index in range(121)]
    assert statuses[-1]==429


def test_wsgi_trusted_proxy_environment_is_explicit_and_validated(tmp_path,monkeypatch):
    from yijianjifa_cloud_voice.wsgi import create_from_environment
    for name in ('YJJF_VOLC_API_KEY','YJJF_VOLC_APP_ID','YJJF_VOLC_ACCESS_KEY','YJJF_CLOUD_AUDIO_DIR',
                 'YJJF_CLOUD_VOICES_FILE','YJJF_CLOUD_MAX_DAILY_WORDS'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('YJJF_CLOUD_DATABASE',str(tmp_path/'proxy-env.db'))
    monkeypatch.setenv('YJJF_CLOUD_TRUSTED_PROXIES','127.0.0.1,::1')
    app=create_from_environment()
    assert call(app,'GET','/missing',HTTP_X_REAL_IP='bad-source')['status']==400
    monkeypatch.setenv('YJJF_CLOUD_TRUSTED_PROXIES','*')
    with pytest.raises(RuntimeError):
        create_from_environment()


def test_wsgi_holds_exclusive_process_lock_and_releases_on_close(tmp_path,monkeypatch):
    import os
    import subprocess
    import sys
    from yijianjifa_cloud_voice.wsgi import create_from_environment
    for name in ('YJJF_VOLC_API_KEY','YJJF_VOLC_APP_ID','YJJF_VOLC_ACCESS_KEY','YJJF_CLOUD_AUDIO_DIR',
                 'YJJF_CLOUD_VOICES_FILE','YJJF_CLOUD_MAX_DAILY_WORDS','YJJF_CLOUD_TRUSTED_PROXIES'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('YJJF_CLOUD_DATABASE',str(tmp_path/'locked.db'))
    app=create_from_environment()
    probe=subprocess.run([sys.executable,'-c',
        'from yijianjifa_cloud_voice.wsgi import create_from_environment; create_from_environment()'],
        env=dict(os.environ),capture_output=True,timeout=15)
    assert probe.returncode!=0
    assert '已由另一服务进程使用' in probe.stderr.decode()
    with pytest.raises(RuntimeError,match='已由另一服务进程使用'):
        create_from_environment()
    app.close()
    restarted=create_from_environment()
    assert call(restarted,'GET','/missing')['status']==404
    restarted.close()


def test_wsgi_refuses_inherited_preload_application(tmp_path,monkeypatch):
    import os
    from yijianjifa_cloud_voice.wsgi import create_from_environment
    for name in ('YJJF_VOLC_API_KEY','YJJF_VOLC_APP_ID','YJJF_VOLC_ACCESS_KEY','YJJF_CLOUD_AUDIO_DIR',
                 'YJJF_CLOUD_VOICES_FILE','YJJF_CLOUD_MAX_DAILY_WORDS','YJJF_CLOUD_TRUSTED_PROXIES'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('YJJF_CLOUD_DATABASE',str(tmp_path/'preload.db'))
    app=create_from_environment()
    owner=os.getpid()
    with monkeypatch.context() as patch:
        patch.setattr(os,'getpid',lambda:owner+1)
        response=call(app,'GET','/missing')
        assert response['status']==503
        assert response['body']['error']['code']=='invalid_process_owner'
    app.close()
