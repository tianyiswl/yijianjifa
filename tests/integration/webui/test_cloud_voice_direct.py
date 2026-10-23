"""Real HTTP between two isolated origins; supplier credentials are fixtures only.

The injected transports map fixed official API/CDN URLs onto a local fake
supplier. Production URL policy still runs; no production loopback bypass is
added and no supplier model, account or bill is accessed.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess
from yijianjifa.features.mixing.tts.hosted import HostedTTS, VoicePaused
from yijianjifa.features.mixing.tts.supplier_download import SupplierAudioDownloader


class Vault:
    def __init__(self): self.values = {}
    def get_password(self, service, key): return self.values.get((service, key))
    def set_password(self, service, key, value): self.values[service, key] = value


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args): pass


@contextmanager
def listening(app):
    server = make_server('127.0.0.1', 0, app, handler_class=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


class MappedResponse:
    def __init__(self, response, original):
        self.response, self.original = response, original
        self.headers, self.status = response.headers, response.status
    def geturl(self): return self.original
    def read(self, size): return self.response.read(size)
    def close(self): self.response.close()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class Supplier:
    def __init__(self, audio, mode):
        self.audio, self.mode, self.tasks = audio, mode, {}
        self.submissions, self.queries, self.downloads = [], [], []
        self.lost = False

    def app(self, env, start):
        path, method = env['PATH_INFO'], env['REQUEST_METHOD']
        if path.startswith('/objects/'):
            assert method == 'GET'
            assert not any(env.get(key) for key in ('HTTP_AUTHORIZATION','HTTP_X_API_ACCESS_KEY','HTTP_X_API_APP_ID','HTTP_X_API_KEY','HTTP_COOKIE','HTTP_REFERER'))
            self.downloads.append(path)
            if self.mode == 'expired_once' and len(self.downloads) == 1:
                start('403 Forbidden', [('Content-Length','0')]); return [b'']
            if self.mode == 'interrupted_once' and len(self.downloads) == 1:
                start('200 OK', [('Content-Length',str(len(self.audio)+20))]); return [self.audio[:20]]
            audio = b'not-a-valid-audio' if self.mode == 'invalid_once' and len(self.downloads) == 1 else self.audio
            start('200 OK', [('Content-Type','audio/mpeg'),('Content-Length',str(len(audio)))]); return [audio]
        assert env['HTTP_X_API_ACCESS_KEY'] == 'FIXTURE-OPERATOR-KEY'
        assert env['HTTP_X_API_APP_ID'] == 'fixture-app'
        raw = env['wsgi.input'].read(int(env['CONTENT_LENGTH']))
        body = json.loads(raw)
        if path.endswith('/submit'):
            task = body['unique_id']; self.submissions.append(task)
            assert task not in self.tasks, 'A paid submit was repeated'
            self.tasks[task] = 0
            data = dict(task_id=task, task_status=1)
        else:
            assert path.endswith('/query')
            task = body['task_id']; assert task in self.tasks
            self.queries.append(task); self.tasks[task] += 1
            data = dict(task_id=task, task_status=1)
            if self.tasks[task] > 1:
                data.update(task_status=2, audio_url=f'https://lf26-lab-speech-tt-sign.bytespeech.com/objects/{task}?signature=FIXTURE-URL-{len(self.queries)}', url_expire_time=int(time.time()+3600), sentences=[{'endTime':1.0}])
        payload = json.dumps(dict(code=20000000, data=data)).encode()
        start('200 OK', [('Content-Type','application/json'),('Content-Length',str(len(payload)))]); return [payload]

    def transport(self, endpoint, *, api=False):
        def open_mapped(request, timeout):
            parts = urlsplit(request.full_url)
            if api:
                assert parts.hostname == 'openspeech.bytedance.com'
            else:
                assert parts.hostname == 'lf26-lab-speech-tt-sign.bytespeech.com'
                assert not request.has_header('Authorization')
            target = endpoint + parts.path + ('?'+parts.query if parts.query else '')
            mapped = Request(target, data=request.data, headers=dict(request.header_items()), method=request.get_method())
            response = urlopen(mapped, timeout=timeout)
            if api and parts.path.endswith('/submit') and self.mode == 'submit_lost_once' and not self.lost:
                self.lost = True; response.close(); raise URLError('fixture lost submit receipt')
            return MappedResponse(response, request.full_url)
        return open_mapped


@pytest.mark.parametrize('mode', ['normal','expired_once','interrupted_once','invalid_once','submit_lost_once'])
@pytest.mark.parametrize('usage', ['audition','making_batch'])
def test_direct_http_download_and_recovery_never_resubmit_or_double_charge(tmp_path, monkeypatch, mode, usage, synthetic_mp3):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root/'server/cloud_voice'))
    from yijianjifa_cloud_voice.api import create_app
    from yijianjifa_cloud_voice.credits import CreditService
    from yijianjifa_cloud_voice.jobs import JobService
    from yijianjifa_cloud_voice.provider_async import AsyncVolcengineProvider
    from yijianjifa_cloud_voice.store import Store

    # Locally generated sine audio; no supplier sample or model generation.
    audio = synthetic_mp3.read_bytes()
    supplier = Supplier(audio, mode)
    with listening(supplier.app) as supplier_endpoint:
        provider = AsyncVolcengineProvider('fixture-app','FIXTURE-OPERATOR-KEY',max_daily_words=10000, opener=supplier.transport(supplier_endpoint,api=True))
        store = Store(tmp_path/'server/voice.db')
        credits = CreditService(store)
        jobs = JobService(store,tmp_path/'server/audio',provider,['doubao:test'])
        app = create_app(store,credits=credits,jobs=jobs)
        control_requests = []
        def control(env, start):
            control_requests.append((env['REQUEST_METHOD'],env['PATH_INFO']))
            return app(env,start)
        with listening(control) as endpoint:
            assert endpoint != supplier_endpoint
            data = tmp_path/'client'; data.mkdir()
            paths = SimpleNamespace(data_dir=data,cache_dir=data/'cache')
            vault = Vault()
            cloud = CloudVoiceAccess(paths,endpoint=endpoint,vault=vault)
            assert cloud.register('direct_fixture','fixture-password')['authenticated']
            code = credits.issue(1,order_ref='ISOLATED-DIRECT-HTTP-NOT-PAYMENT')[0]['code']
            cloud.redeem(code)
            def new_client():
                fresh = CloudVoiceAccess(paths,endpoint=endpoint,vault=vault)
                return HostedTTS(fresh,paths.cache_dir,poll_interval=.01,poll_timeout=5,
                                 downloader=SupplierAudioDownloader(opener=supplier.transport(supplier_endpoint)))
            def generate(output):
                adapter = new_client()
                if usage == 'audition':
                    return adapter.synthesize('测试','doubao:test',1,output)
                snapshot = dict(copies=[{'voice':'测试'}], options={'voice':'doubao:test','speed':1})
                snapshot['voice_identity'] = adapter.freeze(snapshot)
                with adapter.batch_context('original-making-batch',snapshot) as bound:
                    return bound.synthesize('测试','doubao:test',1,output)
            if mode in {'interrupted_once','invalid_once'}:
                with pytest.raises(VoicePaused) as error:
                    generate(data/'first.aiff')
                assert error.value.code in {'audio_download_failed','invalid_audio'}
                assert not list(paths.cache_dir.rglob('*.mp3'))
                assert cloud.read()['used_words'] == 2
            result = generate(data/'second.aiff')
            assert result.duration_ms > 0 and result.output_path.read_bytes() == audio
            count = len(supplier.downloads)
            # New client object reuses its durable original job and local audio.
            again = generate(data/'third.aiff')
            assert again.output_path.read_bytes() == audio and len(supplier.downloads) == count
            assert len(supplier.submissions) == 1 and len(set(supplier.queries)) == 1
            assert cloud.read()['available_words'] == 9998
            with store.connect() as connection:
                row = connection.execute('SELECT * FROM voice_jobs').fetchone()
                assert row['provider_task_id'] == supplier.submissions[0]
                assert connection.execute("SELECT COUNT(*) FROM ledger WHERE kind='generation'").fetchone()[0] == 1
                assert connection.execute('SELECT COUNT(*) FROM voice_jobs').fetchone()[0] == 1
                assert 'signature=' not in '\n'.join(connection.iterdump())
                job_id = row['id']
            assert not list((tmp_path/'server/audio').rglob('*'))
            assert not any(path.endswith('/audio') or path.endswith('/retry') for _,path in control_requests)
            persisted = ''.join(path.read_text() for path in (data/'cloud-voice-jobs').glob('*.json'))
            assert 'FIXTURE-OPERATOR-KEY' not in persisted and 'signature=' not in persisted
            other_data = tmp_path/'other-client'; other_data.mkdir()
            other = CloudVoiceAccess(SimpleNamespace(data_dir=other_data),endpoint=endpoint,vault=Vault())
            other.register('other_fixture','fixture-password')
            query_count = len(supplier.queries)
            with pytest.raises(VoicePaused):
                other._request('GET',f'/v1/jobs/{job_id}/download')
            assert len(supplier.queries) == query_count
