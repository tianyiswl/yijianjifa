import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess
from yijianjifa.features.mixing.tts.hosted import HostedTTS, VoicePaused


class Vault:
    def __init__(self): self.values = {}
    def get_password(self, service, key): return self.values.get((service, key))
    def set_password(self, service, key, value): self.values[service, key] = value


class Response(io.BytesIO):
    def __init__(self, value): super().__init__(json.dumps(value).encode())


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class CloudFixture:
    def __init__(self, tmp_path, *, running=0, initial='pending'):
        self.calls, self.running, self.state, self.starts, self.descriptors = [], running, initial, 0, 0
        self.key = None
        self.paths = SimpleNamespace(data_dir=tmp_path, cache_dir=tmp_path/'cache')
        self.cloud = CloudVoiceAccess(self.paths, endpoint='http://127.0.0.1:12345', opener=self.open, vault=Vault())
        self.cloud._save_session(dict(account_id='fixture-account', username='fixture', access_token='OWN-BEARER-SECRET', refresh_token='OWN-REFRESH-SECRET'))

    def job(self):
        return dict(key=self.key, id='job-original', state=self.state, delivery_mode='direct', words=2)

    def open(self, request, **kwargs):
        self.calls.append((request.method, request.full_url))
        assert request.get_header('Authorization') == 'Bearer OWN-BEARER-SECRET'
        if request.full_url.endswith('/v1/batches'):
            self.key = json.loads(request.data)['items'][0]['key']
            return Response(dict(id='batch-original', jobs=[self.job()]))
        if request.full_url.endswith('/lease'):
            return Response({})
        if request.full_url.endswith('/batch-original'):
            return Response(dict(id='batch-original', jobs=[self.job()]))
        if request.full_url.endswith('/start'):
            self.starts += 1
            self.state = 'running'
            return Response(self.job())
        if request.full_url.endswith('/job-original'):
            if self.running:
                self.running -= 1
            else:
                self.state = 'succeeded'
            return Response(self.job())
        if request.full_url.endswith('/download'):
            self.descriptors += 1
            return Response(dict(kind='supplier_url', job_id='job-original', url=f'https://lf26-lab-speech-tt-sign.bytespeech.com/original.mp3?signature=PRIVATE-{self.descriptors}', expires_at=time.time()+3600, max_bytes=33554432))
        raise AssertionError(request.full_url)


class Download:
    def __init__(self, errors=()): self.calls, self.errors = [], list(errors)
    def download(self, descriptor, destination):
        self.calls.append(dict(descriptor))
        if self.errors:
            raise self.errors.pop(0)
        Path(destination).write_bytes(b'fixture-valid-audio')


def adapter(fixture, downloader, clock=None, **kwargs):
    clock = clock or Clock()
    return HostedTTS(fixture.cloud, fixture.paths.cache_dir, downloader=downloader, probe=lambda path:1000,
                     clock=clock, sleep=clock.sleep, **kwargs)


def test_async_job_polls_original_then_downloads_directly_without_proxy_audio(tmp_path):
    fixture, download, clock = CloudFixture(tmp_path, running=2), Download(), Clock()
    tts = adapter(fixture, download, clock)
    result = tts.synthesize('测试', 'doubao:test', 1, tmp_path/'out.aiff')
    assert result.output_path.read_bytes() == b'fixture-valid-audio'
    assert fixture.starts == 1 and fixture.descriptors == 1
    assert not any(url.endswith('/audio') or url.endswith('/retry') for _, url in fixture.calls)
    assert len(download.calls) == 1 and download.calls[0]['job_id'] == 'job-original'
    assert clock.now > 0
    records = ''.join(path.read_text() for path in (tmp_path/'cloud-voice-jobs').glob('*.json'))
    assert 'PRIVATE-' not in records and 'bytespeech.com' not in records and 'OWN-BEARER-SECRET' not in records


def test_download_failure_then_new_client_continues_same_job_without_generation(tmp_path):
    fixture = CloudFixture(tmp_path)
    failed = Download([VoicePaused('audio_download_failed', '下载中断，请继续获取原音频。')])
    with pytest.raises(VoicePaused, match='下载中断'):
        adapter(fixture, failed).synthesize('测试', 'doubao:test', 1, tmp_path/'first.aiff')
    download = Download()
    result = adapter(fixture, download).synthesize('测试', 'doubao:test', 1, tmp_path/'second.aiff')
    assert result.duration_ms == 1000
    assert fixture.starts == 1 and fixture.descriptors == 2
    assert not any(url.endswith('/retry') for _, url in fixture.calls)
    assert not list((tmp_path/'cache').rglob('*.partial.mp3'))


def test_expired_download_link_refreshes_once_and_never_generates_again(tmp_path):
    fixture = CloudFixture(tmp_path)
    download = Download([VoicePaused('audio_link_expired', '链接已过期')])
    adapter(fixture, download).synthesize('测试', 'doubao:test', 1, tmp_path/'out.aiff')
    assert fixture.starts == 1 and fixture.descriptors == 2
    assert download.calls[0]['url'] != download.calls[1]['url']


def test_repeated_expired_links_stop_without_loop_or_new_generation(tmp_path):
    fixture = CloudFixture(tmp_path)
    download = Download([VoicePaused('audio_link_expired', '链接已过期') for _ in range(3)])
    with pytest.raises(VoicePaused):
        adapter(fixture, download).synthesize('测试', 'doubao:test', 1, tmp_path/'out.aiff')
    assert fixture.starts == 1 and fixture.descriptors == 2 and len(download.calls) == 2


def test_poll_budget_pauses_and_later_continues_original_job(tmp_path):
    fixture, clock = CloudFixture(tmp_path, running=1000), Clock()
    with pytest.raises(VoicePaused) as error:
        adapter(fixture, Download(), clock, poll_timeout=3, poll_interval=1).synthesize('测试', 'doubao:test', 1, tmp_path/'first.aiff')
    assert error.value.code == 'job_running'
    assert clock.now <= 3 and fixture.starts == 1 and fixture.descriptors == 0
    fixture.running = 0
    adapter(fixture, Download()).synthesize('测试', 'doubao:test', 1, tmp_path/'second.aiff')
    assert fixture.starts == 1 and fixture.descriptors == 1


def test_unknown_direct_job_only_queries_even_with_explicit_resume_authorization(tmp_path):
    fixture = CloudFixture(tmp_path, initial='unknown')
    adapter(fixture, Download()).synthesize('测试', 'doubao:test', 1, tmp_path/'out.aiff')
    assert fixture.starts == 0
    assert not any(url.endswith('/retry') for _, url in fixture.calls)


def test_descriptor_for_another_job_cannot_be_downloaded(tmp_path):
    fixture, download = CloudFixture(tmp_path), Download()
    original = fixture.open
    def wrong_descriptor(request, **kwargs):
        response = original(request, **kwargs)
        if request.full_url.endswith('/download'):
            value = json.loads(response.read());value['job_id'] = 'some-other-job'
            return Response(value)
        return response
    fixture.cloud.opener = wrong_descriptor
    with pytest.raises(VoicePaused) as error:
        adapter(fixture, download).synthesize('测试', 'doubao:test', 1, tmp_path/'out.aiff')
    assert error.value.code == 'invalid_response' and not download.calls


def test_invalid_audio_is_not_cached_and_retrieval_retry_does_not_regenerate(tmp_path):
    fixture, download = CloudFixture(tmp_path), Download()
    tts = adapter(fixture, download)
    tts.probe = lambda path:0
    with pytest.raises(VoicePaused, match='音频'):
        tts.synthesize('测试', 'doubao:test', 1, tmp_path/'first.aiff')
    assert not list((tmp_path/'cache').rglob('*.mp3'))
    adapter(fixture, download).synthesize('测试', 'doubao:test', 1, tmp_path/'second.aiff')
    assert fixture.starts == 1 and fixture.descriptors == 2


def test_local_cache_survives_client_restart_and_needs_no_new_download(tmp_path):
    fixture, download = CloudFixture(tmp_path), Download()
    adapter(fixture, download).synthesize('测试', 'doubao:test', 1, tmp_path/'first.aiff')
    adapter(fixture, download).synthesize('测试', 'doubao:test', 1, tmp_path/'second.aiff')
    assert fixture.starts == 1 and fixture.descriptors == 1 and len(download.calls) == 1


def test_expired_reservation_surfaces_its_reason_without_waiting_or_retrying(tmp_path):
    fixture, clock = CloudFixture(tmp_path, initial='unknown'), Clock()
    original = fixture.job
    fixture.job = lambda:dict(original(),state='unknown',reserved_words=0,error={'code':'reservation_expired','message':'原配音预留已到期。'})
    with pytest.raises(VoicePaused) as error:
        adapter(fixture, Download(), clock, poll_timeout=3).synthesize('测试','doubao:test',1,tmp_path/'out.aiff')
    assert error.value.code == 'reservation_expired'
    assert clock.now == 0 and fixture.starts == 0 and fixture.descriptors == 0
    assert not any(url.endswith('/retry') for _,url in fixture.calls)


def test_audio_survives_disposable_cache_cleanup(tmp_path):
    import shutil
    fixture, download = CloudFixture(tmp_path), Download()
    voice = adapter(fixture, download)
    voice.synthesize('你好', 'doubao:test', 1, tmp_path/'first.aiff')
    assert voice.cache == tmp_path/'cloud-voice-audio'
    assert list(voice.cache.rglob('*.mp3'))
    shutil.rmtree(fixture.paths.cache_dir, ignore_errors=True)
    adapter(fixture, download).synthesize('你好', 'doubao:test', 1, tmp_path/'second.aiff')
    assert len(download.calls) == 1 and fixture.starts == 1


def test_old_cache_migrates_without_download_or_new_generation(tmp_path):
    from yijianjifa.features.mixing.tts.hosted import _digest
    fixture, download = CloudFixture(tmp_path), Download()
    voice = adapter(fixture, download)
    voice.synthesize('你好', 'doubao:test', 1, tmp_path/'first.aiff')
    original = next(voice.cache.rglob('*.mp3'))
    old = fixture.paths.cache_dir/'hosted-tts'/original.parent.name/(_digest('job-original')+'.mp3')
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(original.read_bytes());original.unlink()
    adapter(fixture, download).synthesize('你好', 'doubao:test', 1, tmp_path/'second.aiff')
    assert original.is_file() and old.is_file()
    assert len(download.calls) == 1 and fixture.starts == 1


def test_confirmed_lost_audio_restores_once_using_next_attempt(tmp_path):
    fixture, download=CloudFixture(tmp_path),Download()
    base=fixture.cloud.opener
    restores=[];attempt=[1]
    original_job=fixture.job
    fixture.job=lambda:dict(original_job(),attempt=attempt[0])
    def opener(request,**kwargs):
        if request.full_url.endswith('/download') and attempt[0]==1:
            from urllib.error import HTTPError
            import io
            raise HTTPError(request.full_url,410,'Gone',{},io.BytesIO(b'{"error":{"code":"audio_gone"}}'))
        if request.full_url.endswith('/restore'):
            restores.append(json.loads(request.data));attempt[0]=2;fixture.state='pending'
            return Response(dict(fixture.job(),attempt=2))
        return base(request,**kwargs)
    fixture.cloud.opener=opener
    voice=adapter(fixture,download)
    result=voice.synthesize('你好','doubao:test',1,tmp_path/'restored.aiff')
    assert result.duration_ms==1000
    assert len(restores)==1 and restores[0]['expected_attempt']==1
    assert any(p.name.endswith('.2.mp3') for p in voice.cache.rglob('*.mp3'))


def test_output_permission_failure_preserves_valid_persistent_audio(tmp_path,monkeypatch):
    fixture,download=CloudFixture(tmp_path),Download();voice=adapter(fixture,download)
    voice.synthesize('你好','doubao:test',1,tmp_path/'first.aiff')
    cached=next(voice.cache.rglob('*.mp3'));original=cached.read_bytes()
    def denied(*args,**kwargs):raise PermissionError('fixture output denied')
    monkeypatch.setattr('yijianjifa.features.mixing.tts.hosted.shutil.copyfile',denied)
    with pytest.raises(VoicePaused) as error:voice.synthesize('你好','doubao:test',1,tmp_path/'denied.aiff')
    assert error.value.code=='local_storage_unavailable'
    assert cached.read_bytes()==original and len(download.calls)==1


def test_refreshed_attempt_sets_current_cache_filename(tmp_path):
    fixture,download=CloudFixture(tmp_path),Download();base=fixture.cloud.opener;job=fixture.job;attempt=[1]
    fixture.job=lambda:dict(job(),attempt=attempt[0])
    def opener(request,**kwargs):
        if request.full_url.endswith('/start'):attempt[0]=2
        return base(request,**kwargs)
    fixture.cloud.opener=opener
    voice=adapter(fixture,download);voice.synthesize('你好','doubao:test',1,tmp_path/'result.aiff')
    assert any(p.name.endswith('.2.mp3') for p in voice.cache.rglob('*.mp3'))
    assert not any(p.name.endswith('.1.mp3') for p in voice.cache.rglob('*.mp3'))


def test_unreadable_legacy_audio_cannot_authorize_paid_restore(tmp_path):
    from yijianjifa.features.mixing.tts.hosted import _digest
    fixture,download=CloudFixture(tmp_path),Download();voice=adapter(fixture,download)
    voice.synthesize('你好','doubao:test',1,tmp_path/'first.aiff')
    cached=next(voice.cache.rglob('*.mp3'))
    old=fixture.paths.cache_dir/'hosted-tts'/cached.parent.name/(_digest('job-original')+'.mp3')
    old.parent.mkdir(parents=True,exist_ok=True);old.write_bytes(cached.read_bytes());cached.unlink()
    def denied(path):raise PermissionError('fixture unreadable legacy')
    voice.probe=denied
    with pytest.raises(VoicePaused) as error:voice.synthesize('你好','doubao:test',1,tmp_path/'second.aiff')
    assert error.value.code=='local_storage_unavailable'
    assert old.is_file() and len(download.calls)==1
