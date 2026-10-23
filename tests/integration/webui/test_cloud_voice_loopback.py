"""Real native HTTP -> isolated WSGI service, using only synthetic WAV audio."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import wave
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess
from yijianjifa.features.mixing.tts.hosted import HostedTTS, VoicePaused


class Vault:
    def __init__(self): self.values = {}
    def get_password(self, service, key): return self.values.get((service, key))
    def set_password(self, service, key, value): self.values[service, key] = value
    def delete_password(self, service, key): self.values.pop((service, key), None)


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args): pass


@contextmanager
def listening(app):
    server = make_server('127.0.0.1', 0, app, handler_class=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_http_reservation_generation_and_reuse_charge_once(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'server/cloud_voice'))
    from yijianjifa_cloud_voice.api import create_app
    from yijianjifa_cloud_voice.credits import CreditService
    from yijianjifa_cloud_voice.jobs import JobService
    from yijianjifa_cloud_voice.store import Store

    class Provider:
        max_daily_words = 10000
        def __init__(self): self.calls = []
        def synthesize(self, text, voice, speed, request_id):
            self.calls.append((text, voice, speed, request_id))
            output = io.BytesIO()
            with wave.open(output, 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\0\0' * 1600)
            return output.getvalue(), 100

    store = Store(tmp_path / 'server' / 'isolated.db')
    credits, provider = CreditService(store), Provider()
    jobs = JobService(store, tmp_path / 'server' / 'audio', provider, [{'id': 'doubao:test', 'name': '测试音色'}])
    app = create_app(store, credits=credits, jobs=jobs)
    native_paths = SimpleNamespace(data_dir=tmp_path / 'client', cache_dir=tmp_path / 'client/cache')
    native_paths.data_dir.mkdir()
    with listening(app) as endpoint:
        cloud = CloudVoiceAccess(native_paths, endpoint=endpoint, vault=Vault())
        assert cloud.register('cloud_tester', 'safe-password-123')['authenticated']
        cloud.logout()
        assert cloud.login('cloud_tester', 'safe-password-123')['authenticated']
        # Isolated fixture code only; never a sales code or external delivery.
        code = credits.issue(1, order_ref='isolated-HTTP-test')[0]['code']
        assert cloud.redeem(code)['added_words'] == 10000
        assert cloud.redeem(code)['added_words'] == 10000
        assert cloud.read()['available_words'] == 10000
        adapter = HostedTTS(cloud, native_paths.cache_dir)
        snapshot = dict(copies=[{'voice': ' 中文A😀！ 重复。 '}, {'voice': '重复。'}], options={'voice': 'doubao:test', 'speed': 1})
        snapshot['voice_identity'] = adapter.freeze(snapshot)
        with adapter.batch_context('original-task', snapshot) as bound:
            before = cloud.read()
            assert before['available_words'] == 9992
            assert before['reserved_words'] == 8
            for index, text in enumerate(('中文A😀！', '重复。', '重复。')):
                assert bound.synthesize(text, 'doubao:test', 1, tmp_path / f'speech-{index}.aiff').duration_ms == 100
        after = cloud.read()
        assert (after['available_words'], after['reserved_words'], after['used_words']) == (9992, 0, 8)
        assert len(provider.calls) == 2
        # Clearing local audio cannot cause a new supplier job or ledger debit.
        for audio in native_paths.cache_dir.rglob('*.mp3'): audio.unlink()
        with adapter.batch_context('original-task', snapshot) as bound:
            bound.synthesize('中文A😀！', 'doubao:test', 1, tmp_path / 'again.aiff')
        assert len(provider.calls) == 2
        assert cloud.read()['used_words'] == 8
        assert len(cloud.usage()['items']) == 3
        assert all('access_token' not in file.read_text() for file in native_paths.data_dir.rglob('*.json'))


def test_bearer_request_never_follows_redirect(tmp_path):
    visits = []
    def app(env, respond):
        visits.append(env['PATH_INFO'])
        respond('302 Found', [('Location', '/secret-leak'), ('Content-Length', '0')])
        return [b'']
    with listening(app) as endpoint:
        cloud = CloudVoiceAccess(SimpleNamespace(data_dir=tmp_path), endpoint=endpoint, vault=Vault())
        cloud._save_session(dict(account_id='a', username='tester', access_token='secret-access', refresh_token='secret-refresh'))
        with pytest.raises(VoicePaused): cloud._request('GET', '/v1/me')
    assert visits == ['/v1/me']


def test_account_password_lifecycle_and_reopening_over_real_http(tmp_path, monkeypatch):
    """Disposable synthetic credentials; no production account or supplier calls."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / 'server/cloud_voice'))
    from yijianjifa_cloud_voice.api import create_app
    from yijianjifa_cloud_voice.store import Store

    store = Store(tmp_path / 'server' / 'account-lifecycle.db')
    native_paths = SimpleNamespace(data_dir=tmp_path / 'client', cache_dir=tmp_path / 'client/cache')
    native_paths.data_dir.mkdir()
    vault = Vault()
    with listening(create_app(store)) as endpoint:
        def reopen():
            return CloudVoiceAccess(native_paths, endpoint=endpoint, vault=vault)

        cloud = reopen()
        assert not cloud.read()['authenticated']
        registered = cloud.register('lifecycle_fixture', 'fixture-old-password-123')
        assert registered['authenticated']
        account_id = registered['account_id']
        cloud.logout()
        assert not reopen().read()['authenticated']
        assert cloud.login('lifecycle_fixture', 'fixture-old-password-123')['account_id'] == account_id
        old_session = dict(cloud._session())
        assert not cloud.change_password('fixture-old-password-123', 'fixture-new-password-456')['authenticated']
        assert not reopen().read()['authenticated']
        with pytest.raises(VoicePaused) as rejected:
            cloud.login('lifecycle_fixture', 'fixture-old-password-123')
        assert rejected.value.code == 'invalid_credentials'
        assert cloud.login('lifecycle_fixture', 'fixture-new-password-456')['account_id'] == account_id
        restored = reopen().read()
        assert restored['authenticated'] and restored['account_id'] == account_id
        assert restored['available_words'] == restored['used_words'] == 0
        # Credentials from before the password change cannot refresh back in.
        with pytest.raises(VoicePaused):
            cloud._request('POST', '/v1/sessions/refresh', {'refresh_token': old_session['refresh_token']}, authenticated=False)
        assert reopen().read()['authenticated']
        with store.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM ledger').fetchone()[0] == 0


def test_lost_audio_automatically_regenerates_and_charges_once(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]/'server/cloud_voice'))
    from yijianjifa_cloud_voice.api import create_app
    from yijianjifa_cloud_voice.store import Store
    from yijianjifa_cloud_voice.jobs import JobService
    from yijianjifa_cloud_voice.credits import CreditService
    class Provider:
        max_daily_words=10000
        def __init__(self):self.calls=0
        def synthesize(self,*args):
            self.calls+=1
            out=io.BytesIO()
            with wave.open(out,'wb') as audio:
                audio.setparams((1,2,16000,0,'NONE','not compressed'));audio.writeframes(b'\0\0'*1600)
            return out.getvalue(),100
    store=Store(tmp_path/'server/voice.db');credits=CreditService(store);provider=Provider()
    jobs=JobService(store,tmp_path/'server/audio',provider,['doubao:test'])
    client_paths=SimpleNamespace(data_dir=tmp_path/'client',cache_dir=tmp_path/'client/cache');client_paths.data_dir.mkdir()
    with listening(create_app(store,credits=credits,jobs=jobs)) as endpoint:
        cloud=CloudVoiceAccess(client_paths,endpoint=endpoint,vault=Vault())
        cloud.register('restore_fixture','fixture-password-123')
        cloud.redeem(credits.issue(1,order_ref='isolated-restore-test')[0]['code'])
        tts=HostedTTS(cloud,client_paths.cache_dir)
        tts.synthesize('你好','doubao:test',1,tmp_path/'first.aiff')
        assert cloud.read()['used_words']==2 and provider.calls==1
        # Removing the local copy only downloads the same paid object again.
        for audio in tts.cache.rglob('*.mp3'):audio.unlink()
        tts.synthesize('你好','doubao:test',1,tmp_path/'downloaded.aiff')
        assert cloud.read()['used_words']==2 and provider.calls==1
        # Definite local simulation loss on both sides permits a new attempt.
        for audio in tts.cache.rglob('*.mp3'):audio.unlink()
        for audio in (tmp_path/'server/audio').rglob('*.audio'):audio.unlink()
        tts.synthesize('你好','doubao:test',1,tmp_path/'restored.aiff')
        assert cloud.read()['used_words']==4 and provider.calls==2
        tts.synthesize('你好','doubao:test',1,tmp_path/'reused.aiff')
        assert cloud.read()['used_words']==4 and provider.calls==2
        with store.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_job_attempts').fetchone()[0]==1
