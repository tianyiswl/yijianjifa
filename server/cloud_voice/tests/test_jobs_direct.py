import concurrent.futures
import json
from pathlib import Path
import threading

import pytest

from yijianjifa_cloud_voice.credits import CreditService
from yijianjifa_cloud_voice.errors import CloudError
from yijianjifa_cloud_voice.jobs import JobService
from yijianjifa_cloud_voice.provider import ProviderFailure, ProviderUnknown
from yijianjifa_cloud_voice.store import Store
from test_jobs import Provider, wav_bytes
from test_api import call
from yijianjifa_cloud_voice.api import create_app


class AsyncProvider:
    delivery_mode = 'direct'
    version = 'seed-tts-2.0-unicode-codepoint-v1'
    max_daily_words = 10000

    def __init__(self, clock):
        self.clock = clock
        self.submissions, self.queries = [], []
        self.state, self.submit_error, self.query_error = 'running', None, None

    def submit(self, text, voice, speed, request_id):
        self.submissions.append((text, voice, speed, request_id))
        if self.submit_error:
            raise self.submit_error
        return {'task_id':request_id, 'state':'running'}

    def query(self, task_id):
        self.queries.append(task_id)
        if self.query_error:
            raise self.query_error
        result = {'task_id':task_id, 'state':self.state, 'duration_ms':100}
        if self.state == 'succeeded':
            result.update(url='https://lf26-lab-speech-tt-sign.bytespeech.com/test.mp3?signature=private', expires_at=self.clock()+3600)
        return result


@pytest.fixture
def direct(tmp_path):
    now = [100000.0]
    store = Store(tmp_path/'voice.db', clock=lambda:now[0])
    with store.transaction() as connection:
        for account in ('alice','bob'):
            connection.execute('INSERT INTO accounts(id,username,password_hash,credits,created_at) VALUES (?,?,?,?,?)',
                               (account,account,'unused',100,now[0]))
    provider = AsyncProvider(lambda:now[0])
    jobs = JobService(store,tmp_path/'audio',provider,['doubao:test'],clock=lambda:now[0])
    return jobs, provider, store, now, tmp_path


def payload(text='本地测试。', idem='direct-batch'):
    return {'idempotency_key':idem, 'items':[{'key':'a'*32,'voice':'doubao:test','text':text,'speed':1,'purpose':'直下载测试'}]}


def balance(store):
    return CreditService(store).balance('alice')


def test_direct_success_settles_from_supplier_once_without_audio_or_url_storage(direct):
    jobs, provider, store, _, root = direct
    batch = jobs.prepare('alice',payload())
    result = jobs.start('alice',batch['id'],'a'*32)
    assert result['state'] == 'running' and result['delivery_mode'] == 'direct'
    assert balance(store)['used_words'] == 0
    provider.state = 'succeeded'
    result = jobs.get('alice',result['id'])
    assert result['state'] == 'succeeded'
    assert balance(store)['used_words'] == len('本地测试。')
    for _ in range(2):
        descriptor = jobs.download('alice',result['id'])
        assert descriptor['kind'] == 'supplier_url' and descriptor['job_id'] == result['id']
        assert descriptor['max_bytes'] == 33554432
        assert descriptor['url'].startswith('https://')
    assert jobs.start('alice',batch['id'],'a'*32)['state'] == 'succeeded'
    assert jobs.retry('alice',result['id'])['state'] == 'succeeded'
    assert len(provider.submissions) == 1
    assert balance(store)['used_words'] == len('本地测试。')
    assert not list((root/'audio').rglob('*'))
    with store.connect() as connection:
        row = dict(connection.execute('SELECT * FROM voice_jobs').fetchone())
        assert row['provider_task_id'] == provider.submissions[0][3]
        assert 'https://' not in json.dumps(row) and 'signature' not in json.dumps(row)
        assert connection.execute("SELECT COUNT(*) FROM ledger WHERE kind='generation'").fetchone()[0] == 1
    assert 'url' not in result and 'provider_task_id' not in result


def test_lost_submit_response_and_restart_only_query_same_durable_task(direct):
    jobs, provider, store, now, root = direct
    provider.submit_error = ProviderUnknown('private timeout')
    provider.query_error = ProviderUnknown('private query timeout')
    batch = jobs.prepare('alice',payload())
    result = jobs.start('alice',batch['id'],'a'*32)
    assert result['state'] == 'unknown'
    task_id = provider.submissions[0][3]
    with store.connect() as connection:
        assert connection.execute('SELECT provider_task_id FROM voice_jobs').fetchone()[0] == task_id
    recovered = JobService(store,root/'audio',provider,['doubao:test'],clock=lambda:now[0])
    assert recovered.start('alice',batch['id'],'a'*32)['state'] == 'unknown'
    provider.query_error, provider.state = None, 'succeeded'
    assert recovered.get('alice',result['id'])['state'] == 'succeeded'
    assert len(provider.submissions) == 1
    assert set(provider.queries) == {task_id}
    assert balance(store)['used_words'] == len('本地测试。')


def test_direct_unknown_after_reservation_expiry_never_retries_or_late_charges(direct):
    jobs, provider, store, now, _ = direct
    provider.query_error = ProviderUnknown('timeout')
    batch = jobs.prepare('alice',payload())
    result = jobs.start('alice',batch['id'],'a'*32)
    now[0] += 1801
    provider.query_error, provider.state = None, 'succeeded'
    result = jobs.get('alice',result['id'])
    assert result['state'] != 'succeeded' and result['reserved_words'] == 0
    with pytest.raises(CloudError) as error:
        jobs.retry('alice',result['id'])
    assert error.value.code == 'direct_retry_forbidden'
    assert balance(store)['used_words'] == 0
    assert len(provider.submissions) == 1


@pytest.mark.parametrize('failure', ['failed','expired','invalid_url','invalid_task','invalid_duration'])
def test_direct_failed_or_invalid_supplier_result_never_charges(direct, failure):
    jobs, provider, store, _, _ = direct
    batch = jobs.prepare('alice',payload())
    result = jobs.start('alice',batch['id'],'a'*32)
    if failure in ('failed','expired'):
        provider.state = failure
    else:
        provider.state = 'succeeded'
        original = provider.query
        def bad_result(task_id):
            value = original(task_id)
            if failure == 'invalid_url': value['url'] = 'http://127.0.0.1/private'
            if failure == 'invalid_task': value['task_id'] = 'other-task'
            if failure == 'invalid_duration': value['duration_ms'] = -1
            return value
        provider.query = bad_result
    result = jobs.get('alice',result['id'])
    assert result['state'] != 'succeeded'
    assert balance(store)['used_words'] == 0
    if failure == 'expired':
        assert result['state'] == 'unknown'
        assert result['error']['code'] == 'audio_link_expired'
        assert result['reserved_words'] == len('本地测试。')
        with pytest.raises(CloudError) as error:
            jobs.retry('alice',result['id'])
        assert error.value.code == 'direct_retry_forbidden'


def test_direct_expired_or_broken_download_preserves_settlement_and_never_regenerates(direct):
    jobs, provider, store, _, _ = direct
    provider.state = 'succeeded'
    batch = jobs.prepare('alice',payload())
    result = jobs.start('alice',batch['id'],'a'*32)
    provider.state = 'expired'
    with pytest.raises(CloudError) as error:
        jobs.download('alice',result['id'])
    assert error.value.code == 'audio_link_expired'
    assert jobs.get('alice',result['id'])['state'] == 'succeeded'
    provider.query_error = ProviderUnknown('private details')
    with pytest.raises(CloudError) as error:
        jobs.download('alice',result['id'])
    assert error.value.code == 'audio_unavailable'
    assert balance(store)['used_words'] == len('本地测试。') and len(provider.submissions) == 1


def test_expired_signed_link_can_refresh_original_task_and_settle_once(direct):
    jobs, provider, store, _, _ = direct
    provider.state = 'expired'
    batch = jobs.prepare('alice',payload())
    result = jobs.start('alice',batch['id'],'a'*32)
    assert result['state'] == 'unknown' and result['error']['code'] == 'audio_link_expired'
    assert balance(store)['reserved_words'] == len('本地测试。')
    provider.state = 'succeeded'
    assert jobs.get('alice',result['id'])['state'] == 'succeeded'
    assert jobs.download('alice',result['id'])['kind'] == 'supplier_url'
    assert balance(store)['used_words'] == len('本地测试。')
    assert balance(store)['reserved_words'] == 0
    assert len(provider.submissions) == 1


def test_direct_download_cross_account_and_early_audio_are_rejected_before_query(direct):
    jobs, provider, _, _, _ = direct
    batch = jobs.prepare('alice',payload())
    result = jobs.start('alice',batch['id'],'a'*32)
    queries = len(provider.queries)
    with pytest.raises(CloudError) as error:
        jobs.download('bob',result['id'])
    assert error.value.status == 404 and len(provider.queries) == queries
    with pytest.raises(CloudError) as error:
        jobs.download('alice',result['id'])
    assert error.value.code == 'audio_not_ready'
    with pytest.raises(CloudError) as error:
        jobs.audio('alice',result['id'])
    assert error.value.code == 'direct_download_required'


def test_concurrent_direct_start_submits_once_and_concurrent_query_settles_once(direct):
    jobs, provider, store, _, _ = direct
    batch = jobs.prepare('alice',payload())
    entered, finish = threading.Event(), threading.Event()
    original = provider.submit
    def blocked_submit(*arguments):
        entered.set()
        assert finish.wait(3)
        return original(*arguments)
    provider.submit = blocked_submit
    with concurrent.futures.ThreadPoolExecutor() as pool:
        first = pool.submit(jobs.start,'alice',batch['id'],'a'*32)
        assert entered.wait(2)
        second = jobs.start('alice',batch['id'],'a'*32)
        assert second['state'] == 'running'
        finish.set()
        result = first.result(3)
    provider.state = 'succeeded'
    with concurrent.futures.ThreadPoolExecutor() as pool:
        states = list(pool.map(lambda _:jobs.get('alice',result['id'])['state'],range(3)))
    assert states == ['succeeded']*3 and len(provider.submissions) == 1
    assert balance(store)['used_words'] == len('本地测试。')


def test_old_success_audio_stays_readable_and_pending_only_can_upgrade(direct):
    jobs, provider, store, now, root = direct
    sync = Provider()
    sync.version = provider.version
    legacy = JobService(store,root/'audio',sync,['doubao:test'],clock=lambda:now[0])
    completed_batch = legacy.prepare('alice',payload())
    completed = legacy.start('alice',completed_batch['id'],'a'*32)
    pending = legacy.prepare('alice',payload('另一段。','legacy-pending'))
    unknown = legacy.prepare('alice',payload('旧未知。','legacy-unknown'))
    sync.error = ProviderUnknown('old response lost')
    legacy.start('alice',unknown['id'],'a'*32)
    with store.transaction() as connection:
        # Recreate the pre-direct schema with real legacy rows and audio files.
        connection.execute('ALTER TABLE voice_jobs DROP COLUMN provider_task_id')
        connection.execute('ALTER TABLE voice_jobs DROP COLUMN delivery_mode')
    current = JobService(store,root/'audio',provider,['doubao:test'],clock=lambda:now[0])
    assert current.audio('alice',completed['id']) == (wav_bytes(),'audio/wav')
    reused = current.prepare('alice',payload(idem='reuse-legacy-success'))
    assert reused['jobs'][0]['id'] == completed['id'] and reused['jobs'][0]['state'] == 'succeeded'
    assert current.start('alice',pending['id'],'a'*32)['delivery_mode'] == 'direct'
    before = len(provider.queries)
    assert current.get('alice',unknown['jobs'][0]['id'])['state'] == 'unknown'
    assert len(provider.queries) == before and len(provider.submissions) == 1
    now[0] += 1801
    current.get('alice',unknown['jobs'][0]['id'])
    with pytest.raises(CloudError) as error:
        current.retry('alice',unknown['jobs'][0]['id'])
    assert error.value.code == 'direct_retry_forbidden'


def test_direct_known_rejection_can_only_retry_after_explicit_new_action(direct):
    jobs, provider, store, _, _ = direct
    provider.submit_error = ProviderFailure('explicit-reject')
    batch = jobs.prepare('alice',payload())
    failed = jobs.start('alice',batch['id'],'a'*32)
    assert failed['state'] == 'failed' and balance(store)['reserved_words'] == 0
    old_task = provider.submissions[0][3]
    assert jobs.start('alice',batch['id'],'a'*32)['state'] == 'failed'
    assert len(provider.submissions) == 1
    retried = jobs.retry('alice',failed['id'],{'idempotency_key':'explicit-direct-retry'})
    assert retried['state'] == 'pending' and retried['attempt'] == 2
    provider.submit_error, provider.state = None, 'succeeded'
    generated = jobs.start('alice',batch['id'],'a'*32)
    assert generated['state'] == 'succeeded'
    assert provider.submissions[-1][3] != old_task
    assert balance(store)['used_words'] == len('本地测试。')


def test_unknown_direct_task_still_occupies_concurrency_budget(direct):
    jobs, provider, _, _, _ = direct
    jobs.max_concurrent = 1
    provider.query_error = ProviderUnknown('uncertain')
    first = jobs.prepare('alice',payload())
    assert jobs.start('alice',first['id'],'a'*32)['state'] == 'unknown'
    second = jobs.prepare('alice',payload('下一段。','second-direct-batch'))
    with pytest.raises(CloudError) as error:
        jobs.start('alice',second['id'],'a'*32)
    assert error.value.code == 'service_busy' and len(provider.submissions) == 1


def test_authenticated_download_http_route_only_returns_descriptor(direct):
    jobs, provider, store, _, _ = direct
    class Auth:
        def authenticate(self, token): return token
    app = create_app(store,jobs=jobs,auth=Auth())
    provider.state = 'succeeded'
    batch = jobs.prepare('alice',payload())
    result = jobs.start('alice',batch['id'],'a'*32)
    path = f'/v1/jobs/{result["id"]}/download'
    response = call(app,'GET',path,token='alice')
    assert response['status'] == 200 and response['body']['kind'] == 'supplier_url'
    assert response['headers']['Cache-Control'] == 'no-store'
    assert call(app,'GET',path,token='bob')['status'] == 404
    assert call(app,'GET',path)['status'] == 401


def test_confirmed_gone_restore_is_atomic_and_charges_only_new_success(direct):
    from yijianjifa_cloud_voice.provider import ProviderAudioGone
    jobs, provider, store, _, _ = direct
    provider.state='succeeded'
    batch=jobs.prepare('alice',payload())
    original=jobs.start('alice',batch['id'],'a'*32)
    provider.query_error=ProviderAudioGone('confirmed test deletion')
    with pytest.raises(CloudError) as gone: jobs.download('alice',original['id'])
    assert gone.value.code=='audio_gone'
    p={'idempotency_key':'restore-one','expected_attempt':1}
    restored=jobs.restore('alice',original['id'],p)
    assert restored['attempt']==2 and restored['state']=='pending'
    assert jobs.restore('alice',original['id'],p)['attempt']==2
    assert jobs.restore('alice',original['id'],dict(p,idempotency_key='other-caller'))['attempt']==2
    assert balance(store)['used_words']==len('本地测试。')
    provider.query_error=None
    jobs.start('alice',batch['id'],'a'*32)
    assert balance(store)['used_words']==2*len('本地测试。')
    with store.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_job_attempts').fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM ledger WHERE kind='generation'").fetchone()[0]==2


def test_restore_never_regenerates_on_unknown_or_existing_download(direct):
    jobs, provider, _, _, _=direct
    provider.state='succeeded';b=jobs.prepare('alice',payload());j=jobs.start('alice',b['id'],'a'*32)
    for error in (None,ProviderUnknown('timeout')):
        provider.query_error=error
        with pytest.raises(CloudError):jobs.restore('alice',j['id'],{'idempotency_key':'r','expected_attempt':1})
    assert len(provider.submissions)==1


def test_restore_concurrent_callers_share_one_attempt_and_archive(direct):
    from yijianjifa_cloud_voice.provider import ProviderAudioGone
    jobs,p,store,_,_=direct;p.state='succeeded'
    b=jobs.prepare('alice',payload());j=jobs.start('alice',b['id'],'a'*32)
    p.query_error=ProviderAudioGone('confirmed fixture loss')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda n:jobs.restore('alice',j['id'],{'expected_attempt':1,'idempotency_key':f'caller-{n}'}),range(4)))
    assert {x['attempt'] for x in results}=={2}
    with store.connect() as c:assert c.execute('SELECT COUNT(*) FROM voice_job_attempts').fetchone()[0]==1
    assert balance(store)['reserved_words']==len('本地测试。')


def test_restore_insufficient_credit_and_wrong_owner_do_not_mutate(direct):
    from yijianjifa_cloud_voice.provider import ProviderAudioGone
    jobs,p,store,_,_=direct;p.state='succeeded'
    b=jobs.prepare('alice',payload());j=jobs.start('alice',b['id'],'a'*32)
    p.query_error=ProviderAudioGone('confirmed fixture loss')
    with pytest.raises(CloudError) as owner:jobs.restore('bob',j['id'],{'expected_attempt':1,'idempotency_key':'r'})
    assert owner.value.status==404
    with store.transaction() as c:c.execute('UPDATE accounts SET credits=used WHERE id=?',('alice',))
    with pytest.raises(CloudError) as funds:jobs.restore('alice',j['id'],{'expected_attempt':1,'idempotency_key':'r'})
    assert funds.value.code=='insufficient_credit'
    assert balance(store)['reserved_words']==0
    with store.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_job_attempts').fetchone()[0]==0
        assert c.execute('SELECT attempt FROM voice_jobs').fetchone()[0]==1
