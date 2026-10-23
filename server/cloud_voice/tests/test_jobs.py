from __future__ import annotations

import concurrent.futures
import io
import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from yijianjifa_cloud_voice.errors import CloudError
from yijianjifa_cloud_voice.store import Store
from yijianjifa_cloud_voice.jobs import JobService
from yijianjifa_cloud_voice.provider import ProviderFailure, ProviderUnknown, VolcengineProvider


def wav_bytes():
    target = io.BytesIO()
    with wave.open(target, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 800)
    return target.getvalue()


class Provider:
    version = 'test-v1'
    max_daily_words = 100000

    def __init__(self):
        self.calls = []
        self.error = None

    def synthesize(self, text, voice, speed, request_id):
        self.calls.append((text, voice, speed, request_id))
        if self.error:
            raise self.error
        return wav_bytes(), 100


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 100000.0
        self.store = Store(self.root / 'test.db', clock=lambda: self.now)
        self.provider = Provider()
        with self.store.transaction() as connection:
            for account in ('alice', 'bob'):
                connection.execute('INSERT INTO accounts (id,username,password_hash,recovery_hash,credits,used,reserved,created_at) VALUES (?,?,?,?,?,?,?,?)', (account, account, 'x', 'y', 100, 0, 0, self.now))
        self.jobs = JobService(self.store, self.root / 'audio', self.provider, [{'id':'doubao:test','name':'测试声音'}], clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, text='你好，A1。', key='a' * 32, idem='batch-1'):
        return {'idempotency_key':idem,'items':[{'key':key,'text':text,'voice':'doubao:test','speed':1.0,'purpose':'制作测试'}]}

    def balances(self, account='alice'):
        with self.store.connect() as connection:
            return tuple(connection.execute('SELECT credits,used,reserved FROM accounts WHERE id=?', (account,)).fetchone())

    def test_insufficient_whole_batch_calls_nothing(self):
        with self.assertRaises(CloudError) as raised:
            self.jobs.prepare('alice', self.payload('字' * 101))
        self.assertEqual(raised.exception.code, 'insufficient_credit')
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.balances(), (100,0,0))

    def test_repeated_segments_and_batches_share_one_reservation_and_settlement(self):
        payload = self.payload()
        payload['items'].append(dict(payload['items'][0], key='b' * 32))
        first = self.jobs.prepare('alice', payload)
        second = self.jobs.prepare('alice', self.payload(idem='batch-2'))
        self.assertEqual(first['jobs'][0]['id'], first['jobs'][1]['id'])
        self.assertEqual(first['jobs'][0]['id'], second['jobs'][0]['id'])
        self.assertEqual(self.balances(), (100,0,6))
        result = self.jobs.start('alice', first['id'], 'a' * 32)
        self.assertEqual(result['state'], 'succeeded')
        self.jobs.start('alice', second['id'], 'a' * 32)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.balances(), (100,6,0))
        self.assertEqual(self.jobs.audio('alice', result['id']), (wav_bytes(), 'audio/wav'))

    def test_unicode_count_is_actual_submitted_text(self):
        raw = '  你好😀 A1，\n再见。  '
        result = self.jobs.prepare('alice', self.payload(raw))
        self.assertEqual(result['required_words'], len(raw))
        self.jobs.start('alice', result['id'], 'a' * 32)
        self.assertEqual(self.provider.calls[0][0], raw)

    def test_custom_audition_preserves_and_charges_edge_spaces(self):
        raw = '  你好  '
        result = self.jobs.prepare('alice', self.payload(raw))
        self.assertEqual(result['required_words'], 6)
        generated = self.jobs.start('alice', result['id'], 'a' * 32)
        self.assertEqual(generated['words'], 6)
        self.assertEqual(self.provider.calls[0][0], raw)
        self.assertEqual(self.balances(), (100,6,0))

    def test_full_text_limit_counts_edge_spaces(self):
        with self.assertRaises(CloudError) as raised:
            self.jobs.prepare('alice', self.payload(' ' + '字' * 3999 + ' '))
        self.assertEqual(raised.exception.code, 'invalid_request')

    def test_default_batch_limit_allows_one_hundred_videos_with_multiple_sentences(self):
        payload = self.payload('共用配音。')
        payload['items'] = [dict(payload['items'][0], key=f'{index:032x}') for index in range(500)]
        result = self.jobs.prepare('alice', payload)
        self.assertEqual(len(result['jobs']), 500)
        self.assertEqual(result['required_words'], 5)

    def test_idempotency_payload_cannot_change(self):
        self.jobs.prepare('alice', self.payload())
        with self.assertRaises(CloudError) as raised:
            self.jobs.prepare('alice', self.payload('另一段'))
        self.assertEqual(raised.exception.code, 'idempotency_conflict')

    def test_concurrent_reservations_cannot_overdraw(self):
        def reserve(index):
            try:
                return self.jobs.prepare('alice', self.payload(str(index) * 60, idem=f'batch-{index}'))
            except CloudError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(reserve, [1,2]))
        self.assertEqual(sum(item is not None for item in result), 1)
        self.assertEqual(self.balances(), (100,0,60))

    def test_unknown_does_not_resend_and_expires_before_explicit_retry(self):
        prepared = self.jobs.prepare('alice', self.payload())
        self.provider.error = ProviderUnknown('timeout')
        result = self.jobs.start('alice', prepared['id'], 'a' * 32)
        self.assertEqual(result['state'], 'unknown')
        self.jobs.start('alice', prepared['id'], 'a' * 32)
        self.assertEqual(len(self.provider.calls), 1)
        with self.assertRaises(CloudError):
            self.jobs.retry('alice', result['id'])
        self.now += 1801
        self.assertEqual(self.jobs.get('alice', result['id'])['reserved_words'], 0)
        self.assertEqual(self.balances(), (100,0,0))
        self.provider.error = None
        retried = self.jobs.retry('alice', result['id'])
        self.assertEqual(retried['attempt'], 2)
        self.jobs.lease('alice', prepared['id'])
        self.jobs.start('alice', prepared['id'], 'a' * 32)
        self.assertEqual(self.balances(), (100,6,0))

    def test_known_failure_releases_and_invalid_audio_never_charges(self):
        prepared = self.jobs.prepare('alice', self.payload())
        self.provider.error = ProviderFailure('rejected')
        self.assertEqual(self.jobs.start('alice', prepared['id'], 'a' * 32)['state'], 'failed')
        self.assertEqual(self.balances(), (100,0,0))
        self.provider.error = None
        self.jobs.retry('alice', prepared['jobs'][0]['id'])
        self.provider.synthesize = lambda *args: (b'not audio', 100)
        result = self.jobs.start('alice', prepared['id'], 'a' * 32)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.balances(), (100,0,0))

    def test_release_keeps_shared_active_batch_and_lease_can_resume(self):
        first = self.jobs.prepare('alice', self.payload())
        second = self.jobs.prepare('alice', self.payload(idem='batch-2'))
        self.jobs.release('alice', first['id'])
        self.assertEqual(self.balances()[2], 6)
        self.jobs.release('alice', second['id'])
        self.assertEqual(self.balances()[2], 0)
        self.jobs.lease('alice', first['id'])
        self.assertEqual(self.balances()[2], 6)

    def test_expired_pending_lease_releases(self):
        prepared = self.jobs.prepare('alice', self.payload())
        self.now += 901
        self.jobs.batch('alice', prepared['id'])
        self.assertEqual(self.balances(), (100,0,0))

    def test_cross_account_job_access_and_audio_rejected(self):
        prepared = self.jobs.prepare('alice', self.payload())
        result = self.jobs.start('alice', prepared['id'], 'a' * 32)
        for operation in (self.jobs.get,self.jobs.audio,self.jobs.retry):
            with self.assertRaises(CloudError) as raised:
                operation('bob', result['id'])
            self.assertEqual(raised.exception.status, 404)

    def test_late_success_after_unknown_release_cannot_charge(self):
        prepared = self.jobs.prepare('alice', self.payload())
        entered, finish = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            finish.wait(5)
            return wav_bytes(), 100
        self.provider.synthesize = slow
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(self.jobs.start,'alice',prepared['id'],'a'*32)
            self.assertTrue(entered.wait(2))
            self.now += 1801
            self.jobs.get('alice',prepared['jobs'][0]['id'])
            finish.set()
            result = future.result(5)
        self.assertNotEqual(result['state'], 'succeeded')
        self.assertEqual(self.balances(), (100,0,0))

    def test_saved_completion_survives_restart_and_only_settles_once(self):
        prepared = self.jobs.prepare('alice', self.payload())
        original = self.jobs._settle
        self.jobs._settle = lambda *args: (_ for _ in ()).throw(RuntimeError('simulated crash'))
        with self.assertRaises(RuntimeError):
            self.jobs.start('alice', prepared['id'], 'a' * 32)
        recovered = JobService(self.store,self.root/'audio',self.provider,['doubao:test'],clock=lambda:self.now)
        result = recovered.get('alice',prepared['jobs'][0]['id'])
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(self.balances(),(100,6,0))
        recovered.start('alice',prepared['id'],'a'*32)
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.balances(),(100,6,0))
        self.jobs._settle = original

    def test_saved_completion_is_recovered_by_original_query_without_restart(self):
        prepared = self.jobs.prepare('alice', self.payload())
        original = self.jobs._settle
        self.jobs._settle = lambda *args: (_ for _ in ()).throw(RuntimeError('transient failure'))
        with self.assertRaises(RuntimeError):
            self.jobs.start('alice',prepared['id'],'a'*32)
        self.jobs._settle = original
        self.assertEqual(self.jobs.get('alice',prepared['jobs'][0]['id'])['state'],'succeeded')
        self.assertEqual(self.balances(),(100,6,0))
        self.assertEqual(len(self.provider.calls),1)

    def test_simultaneous_start_only_calls_provider_once(self):
        prepared=self.jobs.prepare('alice',self.payload())
        entered,finish=threading.Event(),threading.Event()
        original=self.provider.synthesize
        def slow(*args):
            entered.set()
            finish.wait(5)
            return original(*args)
        self.provider.synthesize=slow
        with concurrent.futures.ThreadPoolExecutor() as pool:
            first=pool.submit(self.jobs.start,'alice',prepared['id'],'a'*32)
            self.assertTrue(entered.wait(2))
            second=self.jobs.start('alice',prepared['id'],'a'*32)
            self.assertEqual(second['state'],'running')
            finish.set()
            self.assertEqual(first.result(5)['state'],'succeeded')
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.balances(),(100,6,0))

    def test_maintenance_releases_expired_reservations_for_balance_reads(self):
        self.jobs.prepare('alice',self.payload())
        self.now+=901
        self.jobs.maintenance()
        self.assertEqual(self.balances(),(100,0,0))

    def test_explicit_retry_nonce_cannot_create_another_attempt_after_later_failure(self):
        prepared=self.jobs.prepare('alice',self.payload())
        self.provider.error=ProviderFailure('reject')
        failed=self.jobs.start('alice',prepared['id'],'a'*32)
        authorization={'idempotency_key':'resume-action-1'}
        first=self.jobs.retry('alice',failed['id'],authorization)
        self.assertEqual(first['attempt'],2)
        failed_again=self.jobs.start('alice',prepared['id'],'a'*32)
        self.assertEqual(failed_again['state'],'failed')
        replay=self.jobs.retry('alice',failed['id'],authorization)
        self.assertEqual(replay['attempt'],2)
        self.assertEqual(replay['state'],'failed')
        second=self.jobs.retry('alice',failed['id'],{'idempotency_key':'resume-action-2'})
        self.assertEqual(second['attempt'],3)

    def test_missing_success_audio_is_not_regenerated_or_charged_again(self):
        prepared=self.jobs.prepare('alice',self.payload())
        result=self.jobs.start('alice',prepared['id'],'a'*32)
        for path in (self.root/'audio').rglob('*.audio'):
            path.unlink()
        with self.assertRaises(CloudError) as raised:
            self.jobs.audio('alice',result['id'])
        self.assertEqual(raised.exception.code,'audio_gone')
        self.jobs.start('alice',prepared['id'],'a'*32)
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.balances(),(100,6,0))

    def test_unhashable_voice_is_a_controlled_validation_error(self):
        payload=self.payload()
        payload['items'][0]['voice']=[]
        with self.assertRaises(CloudError):
            self.jobs.prepare('alice',payload)

    def test_server_restart_marks_unfinished_running_unknown(self):
        prepared = self.jobs.prepare('alice',self.payload())
        with self.store.transaction() as connection:
            connection.execute("UPDATE voice_jobs SET state='running',started_at=?,unknown_until=? WHERE id=?",(self.now,self.now+1800,prepared['jobs'][0]['id']))
        recovered = JobService(self.store,self.root/'audio',self.provider,['doubao:test'],clock=lambda:self.now)
        self.assertEqual(recovered.get('alice',prepared['jobs'][0]['id'])['state'],'unknown')
        recovered.start('alice',prepared['id'],'a'*32)
        self.assertEqual(self.provider.calls,[])

    def test_supplier_budget_cap_is_persistent_and_precedes_call(self):
        self.provider.max_daily_words = 5
        prepared = self.jobs.prepare('alice', self.payload())
        with self.assertRaises(CloudError) as raised:
            self.jobs.start('alice', prepared['id'], 'a' * 32)
        self.assertEqual(raised.exception.code,'supplier_budget_exceeded')
        self.assertEqual(self.provider.calls,[])
        self.assertEqual(self.balances()[1],0)

    def test_production_cap_change_resumes_same_pending_job_without_reset_or_double_charge(self):
        import time
        self.provider.max_daily_words = 100
        day = time.strftime('%Y-%m-%d', time.gmtime(self.now))
        with self.store.transaction() as connection:
            connection.execute('INSERT INTO voice_supplier_budget(day,words) VALUES (?,?)',(day,83))
        prepared = self.jobs.prepare('alice', self.payload('字' * 18))
        with self.assertRaises(CloudError) as raised:
            self.jobs.start('alice', prepared['id'], 'a' * 32)
        self.assertEqual(raised.exception.code, 'supplier_budget_exceeded')
        self.assertEqual(self.balances(), (100,0,18))
        self.assertEqual(self.provider.calls, [])
        self.provider.max_daily_words = 100000
        restarted = JobService(self.store,self.root/'audio',self.provider,['doubao:test'],clock=lambda:self.now)
        result = restarted.start('alice', prepared['id'], 'a' * 32)
        self.assertEqual(result['state'], 'succeeded')
        restarted.start('alice', prepared['id'], 'a' * 32)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.balances(), (100,18,0))
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT words FROM voice_supplier_budget WHERE day=?',(day,)).fetchone()[0],101)


class ProviderTests(unittest.TestCase):
    def test_credentials_budget_and_endpoint_are_operator_only(self):
        with self.assertRaises(ValueError):
            VolcengineProvider('app','secret',max_daily_words=0)

    def test_sse_completion_and_safe_errors(self):
        import base64
        class Response(io.BytesIO):
            pass
        audio = wav_bytes()
        lines = b'data: '+json.dumps({'code':0,'data':base64.b64encode(audio).decode()}).encode()+b'\n\ndata: {"code":20000000}\n\n'
        requests = []
        def opener(request,timeout):
            requests.append(request)
            return Response(lines)
        provider = VolcengineProvider('123','secret',max_daily_words=1000,opener=opener)
        result,duration = provider.synthesize('你好','doubao:test',1.0,'request-1')
        self.assertEqual(result,audio)
        self.assertEqual(duration,100)
        self.assertNotIn('secret',repr(provider))
        self.assertEqual(requests[0].full_url,'https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse')

    def test_incomplete_and_malformed_stream_are_unknown(self):
        for body in (b'data: {"code":0}\n',b'data: bad\n'):
            provider = VolcengineProvider('123','secret',max_daily_words=1000,opener=lambda *a,**kw:io.BytesIO(body))
            with self.assertRaises(ProviderUnknown):
                provider.synthesize('你好','doubao:test',1.0,'request-1')


if __name__ == '__main__':
    unittest.main()
