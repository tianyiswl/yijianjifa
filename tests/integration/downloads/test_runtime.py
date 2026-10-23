"""Isolated scheduler contracts and real owned-file/media composition.

Controlled providers/transports prove scheduling, never live platform acceptance.
"""
from dataclasses import asdict
import importlib
import json
from pathlib import Path
import shutil
import os
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import select, text

from yijianjifa.app_services.media_access import MediaAccess
from yijianjifa.features.downloads.contracts import Candidate, DownloadError, DownloadOptions, MediaSource, Progress, ResolvePage, SubtitleSegment
from yijianjifa.features.downloads.repository import DownloadRepository
from yijianjifa.storage.models import DownloadOutputRow, DownloadSubtitleOriginalRow, DownloadSubtitleDraftRow, Setting
from tests.integration.downloads.fixtures import make_video, digest

FILTER={'query':'','state':''}
MODEL='whisper-small-multilingual'


def eventually(check, timeout=6):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        value=check()
        if value:return value
        time.sleep(.01)
    pytest.fail('bounded background condition was not reached')


class Provider:
    def __init__(self, watermark='clean'):
        self.watermark=watermark;self.issued=[];self.media_calls=[];self.fail=False
    def resolve(self,source,cursor,*,cancel):
        candidate=Candidate(source.source_key,'direct',source.source_key,source.source_key,None,title='隔离视频')
        self.issued.append(candidate)
        return ResolvePage((candidate,),None,False,'complete')
    def media(self,candidate,*,cancel):
        assert any(candidate is row for row in self.issued), 'provider-owned Candidate was copied/reconstructed'
        self.media_calls.append(candidate)
        if self.fail:raise DownloadError('verification_required')
        return MediaSource(candidate.id,self.watermark)


class Fetcher:
    def __init__(self,source):
        self.source=source;self.entered=threading.Event();self.release=threading.Event();self.release.set()
        self.calls=0;self.active=0;self.maximum=0;self.lock=threading.Lock();self.fail_next=False
    def download(self,source,destination,*,cancel,progress):
        with self.lock:
            self.calls+=1;self.active+=1;self.maximum=max(self.maximum,self.active)
        self.entered.set()
        try:
            while not self.release.wait(.01):
                if cancel.cancelled():raise DownloadError('cancelled')
            if self.fail_next:
                self.fail_next=False;raise DownloadError('network')
            shutil.copyfile(self.source,destination)
            progress(Progress('download',destination.stat().st_size,destination.stat().st_size))
        finally:
            with self.lock:self.active-=1


class Models:
    def __init__(self):
        self.ready=True;self.calls=0;self.install_entered=threading.Event();self.install_release=threading.Event();self.install_release.set()
    def list(self):
        self.calls+=1
        return [{'id':MODEL,'name':'受控模型','state':'ready' if self.ready else 'missing'}]
    def require(self,identity):
        if not self.ready:raise DownloadError('model_required')
        return Path('/backend/model')
    def install(self,identity,*,cancel,progress):
        # Simulate the leaf's existing ModelStore semaphore ownership.
        while not self.network_slots.acquire(timeout=.01):
            if cancel.cancelled():raise DownloadError('cancelled')
        try:
            self.install_entered.set()
            while not self.install_release.wait(.01):
                if cancel.cancelled():raise DownloadError('cancelled')
            self.ready=True
        finally:self.network_slots.release()
    def pending_cleanup(self):return ()


class Asr:
    def __init__(self):self.calls=0
    def transcribe(self,audio,*,model_id,language,cancel,progress):
        import wave
        with wave.open(str(audio)) as wav:
            assert (wav.getnchannels(),wav.getframerate(),wav.getsampwidth())==(1,16000,2)
        self.calls+=1
        return (SubtitleSegment('asr_segment',0,500,'真实音轨结构，受控识别文字',.7),)


@pytest.fixture
def runtime(db_engine,app_paths,tmp_path):
    module='yijianjifa.app_services.download_runtime'
    assert importlib.util.find_spec(module), 'DownloadRuntime is missing'
    provider=Provider();fetcher=Fetcher(make_video(tmp_path/'fixture'));models=Models();asr=Asr()
    media=MediaAccess(db_engine,app_paths)
    worker=importlib.import_module(module).DownloadRuntime(db_engine,app_paths,media=media,
                providers={'direct':provider},fetcher=fetcher,models=models,asr=asr)
    output=tmp_path/'output';output.mkdir()
    native=worker.prepare_native('download.output.choose',{},output)
    with db_engine.begin() as connection:worker.enqueue(connection,'download.output.choose',{},native=native)
    worker.start()
    eventually(lambda:worker.recovery_complete)
    yield worker,provider,fetcher,models,asr
    fetcher.release.set();models.install_release.set()
    assert eventually(lambda:worker.close(.05))
    media.close()


def command(runtime,operation,params):
    with runtime.engine.begin() as connection:result=runtime.enqueue(connection,'download.'+operation,params)
    runtime.wake();return result


def candidates(runtime,count=1):
    result=command(runtime,'resolve.start',{'input':'\n'.join(f'https://example.com/{i}.mp4?token=private' for i in range(count))})
    eventually(lambda:runtime.query('download.resolve.status',{'job_id':result['job_id']})['state']=='succeeded')
    page=runtime.query('download.resolve.list',{'session_id':result['session_id'],'page':1,'filter':FILTER})
    return [row['id'] for row in page['items']],result


def cover_catalog(worker,count=1,*,invalid=False,missing=False,size_limit=False):
    import io
    from PIL import Image
    from yijianjifa.features.downloads.network import SecureFetcher
    from yijianjifa.features.downloads.providers import DouyinProvider
    image=io.BytesIO();Image.new('RGB',(12,8),'navy').save(image,format='PNG')
    body=b'<html>invalid cover</html>' if invalid else image.getvalue()
    class Reader:
        def __init__(self):self.calls=[]
        def row(self,i):return {'aweme_id':str(i),'author':{'sec_uid':'a','nickname':'隔离作者'},'desc':'隔离作品',
            'video':{'duration':1000,'cover':None if missing else {'url_list':['https://example.com/cover.png?sig=PRIVATE_COVER']},'play_addr':{'url_list':['https://example.com/video.mp4?sig=PRIVATE_VIDEO']}}}
        def read(self,url,*,cancel):
            self.calls.append(url)
            if '/video/' in url:return {'aweme_detail':self.row(url.rsplit('/',1)[-1])}
            return {'status_code':0,'aweme_list':[self.row(i) for i in range(1,count+1)],'has_more':False,'max_cursor':count,
                '_catalog_source':'a','_catalog_request_cursor':'0'}
        def read_next(self,*a,**kw):pytest.fail('buffered cover catalog must not read another page')
    class Response(io.BytesIO):
        status=200
        def getheader(self,key):return str(16*1024*1024+1 if size_limit else len(body)) if key=='Content-Length' else None
    class Transport:
        def __init__(self):self.calls=0
        def open(self,url):self.calls+=1;return Response(body)
    reader=Reader();transport=Transport();worker.providers['douyin']=DouyinProvider(worker.registry,reader)
    worker.cover_fetcher=SecureFetcher(worker.registry,transport=transport,max_bytes=16*1024*1024)
    request=command(worker,'resolve.start',{'input':'https://www.douyin.com/user/a'})
    eventually(lambda:worker.query('download.resolve.status',{'job_id':request['job_id']})['state']=='succeeded')
    return request,reader,transport


def cover_page(worker,request,page=1):
    return worker.query('download.resolve.list',{'session_id':request['session_id'],'page':page,'filter':FILTER})


@pytest.mark.parametrize('invalid',[False,True])
def test_controlled_cover_is_background_owned_without_video_and_failure_keeps_download(runtime,invalid):
    worker,_,fetcher,models,asr=runtime;request,reader,transport=cover_catalog(worker,invalid=invalid)
    page=eventually(lambda:value if (value:=cover_page(worker,request))['items'][0].get('cover_state') in ('succeeded','failed') else None)
    row=page['items'][0]
    assert row['state']=='not_downloaded' and page['completion']=='complete'
    assert len(reader.calls)==1 and fetcher.calls==0 and asr.calls==0
    if invalid:
        assert row['cover_state']=='failed' and row['cover_resource_id'] is None
    else:
        resource=worker.repository.get_output(row['cover_resource_id'])
        assert resource.kind=='cover' and resource.item_id is None and worker.files.path_for(resource.id).is_file()
        result=command(worker,'preview',{'resource_id':resource.id})
        status=eventually(lambda:value if (value:=worker.query('download.detail',{'job_id':result['job_id']}))['state']=='succeeded' else None)
        token=status['url'].rsplit('/',1)[-1]
        assert status['kind']=='cover' and worker.media.registry.read(token,0,resource.size_bytes).startswith(b'\x89PNG')
    task=batch(worker,[row['id']],prefer_clean=False)
    eventually(lambda:detail(worker,task)['state']=='completed')
    assert fetcher.calls==1 and len(reader.calls)==2
    with worker.engine.connect() as connection:
        encoded=' '.join(str(value) for value in connection.execute(text('SELECT * FROM download_candidates')).all())
        encoded+=' '.join(str(value) for value in connection.execute(text('SELECT * FROM download_auxiliary_jobs')).all())
        encoded+=' '.join(str(value) for value in connection.execute(text('SELECT * FROM settings')).all())
    assert 'PRIVATE_COVER' not in encoded and 'PRIVATE_VIDEO' not in encoded


def test_cover_grant_lru_across_four_pages_preserves_player_and_library(runtime):
    worker,_,fetcher,_,_=runtime;request,reader,transport=cover_catalog(worker,count=80)
    video=worker.media.grant_read(fetcher.source,name='playing.mp4',kind='video',owner='download:playing')
    library=worker.media.grant_read(fetcher.source,name='library.mp4',kind='video',owner='library')
    first_tokens=[]
    for page in range(1,5):
        if page>1:
            previous=cover_page(worker,request,page-1)
            job=command(worker,'resolve.start',{'session_id':request['session_id'],'cursor':previous['next_cursor']})
            eventually(lambda:worker.query('download.resolve.status',{'job_id':job['job_id']})['state']=='succeeded')
        try:
            data=eventually(lambda:value if len((value:=cover_page(worker,request,page))['items'])==20 and all(row.get('cover_state')=='succeeded' for row in value['items']) else None,timeout=60)
        except pytest.fail.Exception:
            pytest.fail('Cover fixture states: '+repr(cover_page(worker,request,page)))
        for row in data['items']:
            response=command(worker,'preview',{'resource_id':row['cover_resource_id']})
            status=eventually(lambda:value if (value:=worker.query('download.detail',{'job_id':response['job_id']}))['state']=='succeeded' else None)
            token=status['url'].rsplit('/',1)[-1]
            assert worker.media.registry.size(token)>0
            if page==1:first_tokens.append(token)
        assert sum(value.get('kind')=='cover' for value in worker._urls.values())<=20
        assert len(worker.media._read_players)<=22
        assert worker.media.registry.size(video['url'].rsplit('/',1)[-1])>0
        assert worker.media.registry.size(library['url'].rsplit('/',1)[-1])>0
    for token in first_tokens:
        with pytest.raises(ValueError):worker.media.registry.size(token)
    assert reader.calls==['https://www.douyin.com/user/a'] and transport.calls==80 and fetcher.calls==0


def test_cover_grants_yield_one_slot_to_new_player_at_shared_capacity(runtime):
    worker,_,fetcher,_,_=runtime;request,_,_=cover_catalog(worker,count=20)
    page=eventually(lambda:value if len((value:=cover_page(worker,request))['items'])==20 and all(row['cover_state']=='succeeded' for row in value['items']) else None,timeout=30)
    owners=[worker.media.grant_read(fetcher.source,name='existing.mp4',kind='video',owner='preserve:'+str(i)) for i in range(44)]
    cover_tokens=[]
    for row in page['items']:
        receipt=command(worker,'preview',{'resource_id':row['cover_resource_id']})
        ready=eventually(lambda:value if (value:=worker.query('download.detail',{'job_id':receipt['job_id']}))['state']=='succeeded' else None)
        cover_tokens.append(ready['url'].rsplit('/',1)[-1])
    assert len(worker.media._read_players)==64
    task=batch(worker,[page['items'][0]['id']],prefer_clean=False)
    completed=eventually(lambda:value if (value:=detail(worker,task))['state']=='completed' else None)
    video=completed['items'][0]['stages']['video']['resource_id'];receipt=command(worker,'preview',{'resource_id':video})
    ready=eventually(lambda:value if (value:=worker.query('download.detail',{'job_id':receipt['job_id']}))['state'] in ('succeeded','failed') else None)
    assert ready['state']=='succeeded' and ready['kind']=='video'
    assert len(worker.media._read_players)==64 and len(worker._cover_reads)==19
    for owner in owners:assert worker.media.registry.size(owner['url'].rsplit('/',1)[-1])>0
    with pytest.raises(ValueError):worker.media.registry.size(cover_tokens[0])
    for token in cover_tokens[1:]:assert worker.media.registry.size(token)>0


def test_detail_does_not_mix_completed_task_with_earlier_resource_snapshot(runtime,monkeypatch):
    worker,_,fetcher,_,_=runtime
    fetcher.release.clear();identities,_=candidates(worker);task=batch(worker,identities,prefer_clean=False)
    assert fetcher.entered.wait(3)
    original=DownloadRepository.summary;reader=threading.get_ident();observed=[]
    def finish_between_reads(repository,task_id):
        snapshot=original(repository,task_id)
        if threading.get_ident()==reader and task_id==task:
            assert snapshot['state']!='completed';observed.append(snapshot['state'])
            fetcher.release.set()
            eventually(lambda:original(repository,task_id)['state']=='completed')
        return snapshot
    monkeypatch.setattr(DownloadRepository,'summary',finish_between_reads)
    result=detail(worker,task)
    assert observed and original(worker.repository,task)['state']=='completed'
    assert result['state']==observed[0]
    assert result['items'][0]['stages']['video']['resource_id'] is None


def test_cover_metadata_refresh_reuses_local_resource_and_reopen_never_fetches(runtime):
    worker,_,fetcher,models,asr=runtime;request,reader,transport=cover_catalog(worker)
    row=eventually(lambda:value if (value:=cover_page(worker,request)['items'][0]).get('cover_state')=='succeeded' else None)
    resource_id=row['cover_resource_id']
    second=command(worker,'resolve.start',{'input':'https://www.douyin.com/user/a'})
    eventually(lambda:worker.query('download.resolve.status',{'job_id':second['job_id']})['state']=='succeeded')
    assert cover_page(worker,second)['items'][0]['cover_resource_id']==resource_id and transport.calls==1
    assert eventually(lambda:worker.close(.05))
    def forbidden(*args,**kwargs):pytest.fail('reopen may not fetch or resolve cover')
    worker.providers['douyin'].reader.read=forbidden;worker.cover_fetcher.download=forbidden
    restarted=type(worker)(worker.engine,worker.paths,media=worker.media,providers=worker.providers,fetcher=fetcher,
        cover_fetcher=worker.cover_fetcher,models=models,asr=asr)
    try:
        restarted.start();eventually(lambda:restarted.recovery_complete)
        assert cover_page(restarted,request)['items'][0]['cover_resource_id']==resource_id
        preview=command(restarted,'preview',{'resource_id':resource_id})
        status=eventually(lambda:value if (value:=restarted.query('download.detail',{'job_id':preview['job_id']}))['state']=='succeeded' else None)
        assert status['kind']=='cover' and status['resource_id']==resource_id
    finally:assert eventually(lambda:restarted.close(.05))


@pytest.mark.parametrize('failure',['missing','size_limit'])
def test_cover_missing_or_oversized_response_is_terminal_and_video_remains_available(runtime,failure):
    worker,_,fetcher,_,_=runtime;request,reader,transport=cover_catalog(worker,**{failure:True})
    state='skipped' if failure=='missing' else 'failed'
    row=eventually(lambda:value if (value:=cover_page(worker,request)['items'][0])['cover_state']==state else None)
    status=worker.query('download.detail',{'job_id':row['cover_job_id']})
    assert status['error_code']==(None if failure=='missing' else 'size_limit')
    assert row['cover_resource_id'] is None and worker.files._values('temporary')==[]
    assert len(reader.calls)==1 and transport.calls==(0 if failure=='missing' else 1)
    task=batch(worker,[row['id']],prefer_clean=False)
    eventually(lambda:detail(worker,task)['state']=='completed')
    assert fetcher.calls==1


@pytest.mark.parametrize('action',['cancel','close'])
def test_cover_cancel_or_close_at_trusted_writer_handoff_releases_only_owned_temporary(runtime,action,monkeypatch):
    worker,_,_,_,_=runtime
    entered=threading.Event();release=threading.Event();confirm=worker.files.confirm_temporary
    def handoff(temporary,*args,**kwargs):
        entered.set();assert release.wait(3)
        return confirm(temporary,*args,**kwargs)
    monkeypatch.setattr(worker.files,'confirm_temporary',handoff)
    try:
        request,reader,transport=cover_catalog(worker)
        assert entered.wait(3)
        row=cover_page(worker,request)['items'][0]
        if action=='cancel':
            command(worker,'cancel',{'job_id':row['cover_job_id']})
            eventually(lambda:worker._events[('job',row['cover_job_id'])].is_set())
        else:assert worker.close(.05) is False
        release.set()
        if action=='close':assert eventually(lambda:worker.close(.05))
        state='cancelled' if action=='cancel' else 'interrupted'
        eventually(lambda:worker.query('download.detail',{'job_id':row['cover_job_id']})['state']==state)
        assert worker.files._values('temporary')==[]
        assert cover_page(worker,request)['items'][0]['cover_resource_id'] is None
        assert len(reader.calls)==1 and cover_page(worker,request)['completion']=='complete'
    finally:release.set()


def test_cover_cancel_preserves_replaced_temporary_and_original_error(runtime,monkeypatch):
    worker,_,_,_,_=runtime;confirm=worker.files.confirm_temporary
    entered=threading.Event();release=threading.Event();paths=[]
    def handoff(temporary,*args,**kwargs):
        confirm(temporary,*args,**kwargs);entered.set();assert release.wait(3)
        original=temporary.with_suffix('.original');temporary.rename(original)
        temporary.write_bytes(b'unrelated replacement');paths.extend((temporary,original))
    monkeypatch.setattr(worker.files,'confirm_temporary',handoff)
    try:
        request,_,_=cover_catalog(worker);assert entered.wait(3)
        row=cover_page(worker,request)['items'][0];command(worker,'cancel',{'job_id':row['cover_job_id']})
        eventually(lambda:worker._events[('job',row['cover_job_id'])].is_set());release.set()
        status=eventually(lambda:value if (value:=worker.query('download.detail',{'job_id':row['cover_job_id']}))['state']=='cancelled' else None)
        assert status['error_code']=='cancelled' and status['resource_id'] is None
        assert paths[0].read_bytes()==b'unrelated replacement' and paths[1].exists()
        assert len(worker.files._values('temporary'))==1  # Identity conflict remains recoverable, never adopted/deleted.
    finally:release.set()


def test_cover_recovery_detaches_changed_inode_without_fetching_or_deleting_replacement(runtime):
    worker,_,fetcher,models,asr=runtime;request,reader,transport=cover_catalog(worker)
    row=eventually(lambda:value if (value:=cover_page(worker,request)['items'][0])['cover_state']=='succeeded' else None)
    resource_id=row['cover_resource_id'];path=worker.files.path_for(resource_id)
    assert eventually(lambda:worker.close(.05))
    original=path.with_suffix('.original');path.rename(original);path.write_bytes(b'unrelated replacement')
    def forbidden(*args,**kwargs):pytest.fail('local recovery cannot perform cover network work')
    worker.cover_fetcher.download=forbidden
    restarted=type(worker)(worker.engine,worker.paths,media=worker.media,providers=worker.providers,
        fetcher=fetcher,cover_fetcher=worker.cover_fetcher,models=models,asr=asr)
    try:
        restarted.start();eventually(lambda:restarted.recovery_complete)
        after=cover_page(restarted,request)['items'][0]
        assert after['cover_resource_id'] is None and after['cover_state']=='interrupted'
        assert worker.repository.get_output(resource_id).id==resource_id
        assert path.read_bytes()==b'unrelated replacement' and original.exists() and transport.calls==1
    finally:assert eventually(lambda:restarted.close(.05))


def test_default_cover_transport_has_its_own_budget_and_the_injected_checked_connector(runtime):
    from yijianjifa.features.downloads.network import PublicConnector,SecureFetcher
    worker,_,_,models,asr=runtime;connector=PublicConnector(resolver=lambda *a,**k:[])
    fetcher=SecureFetcher(worker.registry,connector=connector)
    prepared=type(worker)(worker.engine,worker.paths,media=worker.media,fetcher=fetcher,models=models,asr=asr)
    assert prepared.cover_fetcher.max_bytes==16*1024*1024
    assert prepared.cover_fetcher.transport is not fetcher.transport
    assert prepared.cover_fetcher.transport.connector is connector


def batch(runtime,ids,**options):
    output=runtime.query('download.output.read',{})
    return command(runtime,'start',{'candidate_ids':ids,'options':{**asdict(DownloadOptions(**options)),'model_ids':list(options.get('model_ids',()))},
                                   'directory_grant_id':output['grant_id']})['task_id']


def detail(runtime,identity):return runtime.query('download.detail',{'task_id':identity})


def test_runtime_does_not_block_controls(runtime):
    worker,_,fetcher,_,_=runtime;ids,_=candidates(worker);fetcher.release.clear()
    task=batch(worker,ids)
    assert fetcher.entered.wait(3)
    began=time.monotonic();status=detail(worker,task)
    result=command(worker,'cancel',{'task_id':task})
    assert time.monotonic()-began<.5 and result['task_id']==task
    assert status['state']=='running'
    eventually(lambda:detail(worker,task)['state']=='cancelled')
    assert fetcher.active==0


def test_only_committed_intent_is_visible_and_new_direct_id_is_safe(runtime):
    worker,provider,_,_,_=runtime
    with worker.engine.connect() as connection:
        transaction=connection.begin()
        created=worker.enqueue(connection,'download.resolve.start',{'input':'https://example.com/private.mp4?token=do-not-persist'})
        worker.wake();time.sleep(.1)
        assert not provider.issued
        transaction.rollback()
    ids,_=candidates(worker)
    assert all(len(identity)==32 for identity in ids)
    with worker.engine.connect() as connection:
        values=' '.join(str(row) for row in connection.execute(text('SELECT * FROM download_resolutions')).all())
        values+=' '.join(str(row) for row in connection.execute(text('SELECT * FROM download_candidates')).all())
        values+=' '.join(str(row) for row in connection.execute(text('SELECT * FROM settings')).all())
    assert 'token=private' not in values and 'do-not-persist' not in values


def test_two_network_slots_cover_batch_preview_and_model(runtime):
    worker,_,fetcher,models,_=runtime;ids,_=candidates(worker,4);fetcher.release.clear()
    task=batch(worker,ids[:3])
    eventually(lambda:fetcher.active==2)
    preview=command(worker,'preview.prepare',{'candidate_id':ids[3]})
    models.install_release.clear();model=command(worker,'models.download',{'model_id':MODEL})
    assert fetcher.maximum==2 and not models.install_entered.is_set()
    fetcher.release.set();models.install_release.set()
    eventually(lambda:detail(worker,task)['state']=='completed')
    eventually(lambda:worker.query('download.preview.status',{'job_id':preview['job_id']})['state']=='succeeded')
    eventually(lambda:worker.query('download.detail',{'job_id':model['job_id']})['state']=='succeeded')
    assert fetcher.maximum<=2


def test_waiting_watermark_does_not_stop_other_items_and_original_is_not_clean(runtime):
    worker,provider,_,_,_=runtime;ids,_=candidates(worker,2)
    provider.watermark='unknown'
    task=batch(worker,ids)
    eventually(lambda:detail(worker,task)['state']=='paused')
    items=detail(worker,task)['items']
    assert all(item['decision_required'] for item in items)
    command(worker,'decision',{'task_id':task,'item_id':items[0]['id'],'choice':'original'})
    status=eventually(lambda:value if (value:=detail(worker,task))['counts']['succeeded']==1 and value['state']=='paused' else None)
    assert status['state']=='paused' and status['items'][0]['watermark']!='clean'
    command(worker,'decision',{'task_id':task,'item_id':items[1]['id'],'choice':'skip'})
    status=eventually(lambda:detail(worker,task) if detail(worker,task)['state']=='completed' else None)
    assert status['counts']['succeeded']==1 and status['counts']['skipped']==1


def test_auth_and_model_wait_are_distinct_and_successful_video_survives(runtime):
    worker,provider,_,models,_=runtime;ids,_=candidates(worker)
    models.ready=False
    task=batch(worker,ids,asr_language='zh',model_ids=(MODEL,))
    eventually(lambda:detail(worker,task)['state']=='paused')
    status=detail(worker,task)
    assert status['counts']['partial']==1 and status['items'][0]['waiting_reason']=='model'
    assert not status['verification_required'] and status['items'][0]['outputs']
    models.ready=True
    command(worker,'resume',{'task_id':task,'show_browser':False})
    eventually(lambda:detail(worker,task)['state']=='completed')
    ids,_=candidates(worker,2);provider.fail=True
    auth=batch(worker,ids[1:])
    eventually(lambda:detail(worker,auth)['state']=='paused_auth')
    assert detail(worker,auth)['verification_required']


def test_one_failed_item_does_not_stop_next_and_retry_reuses_success(runtime):
    worker,_,fetcher,_,_=runtime;ids,_=candidates(worker,2);fetcher.fail_next=True
    task=batch(worker,ids)
    eventually(lambda:detail(worker,task)['state']=='completed_with_errors')
    status=detail(worker,task)
    assert status['counts']['failed']==1 and status['counts']['succeeded']==1
    failed=next(item for item in status['items'] if item['state']=='failed')
    command(worker,'retry',{'task_id':task,'item_ids':[failed['id']],'stages':['video']})
    eventually(lambda:detail(worker,task)['state']=='completed')
    assert fetcher.calls==3


def test_asr_uses_real_stage_wav_and_rerun_keeps_original_and_edits(runtime):
    worker,_,fetcher,_,asr=runtime;ids,_=candidates(worker)
    task=batch(worker,ids,asr_language='zh',model_ids=(MODEL,))
    eventually(lambda:detail(worker,task)['state']=='completed')
    item=detail(worker,task)['items'][0];original=item['subtitles'][0]['resource_id']
    command(worker,'subtitle.save',{'resource_id':original,'origin':'asr','expected_revision':0,
                                   'patches':[{'id':'asr_segment','text':'人工编辑保留'}]})
    command(worker,'retry',{'task_id':task,'item_ids':[item['id']],'stages':['asr']})
    eventually(lambda:len(detail(worker,task)['items'][0]['subtitles'])==2)
    assert worker.query('download.subtitle.read',{'resource_id':original,'origin':'asr','cursor':None})['segments'][0]['text']=='人工编辑保留'
    with worker.engine.connect() as connection:
        assert connection.execute(select(DownloadSubtitleOriginalRow.id)).all().__len__()==2
        assert connection.execute(select(DownloadSubtitleDraftRow.id)).all().__len__()==2
    assert fetcher.calls==1 and asr.calls==2


def test_actual_commands_native_single_list_and_start_count(runtime,tmp_path):
    from yijianjifa.app_services.download_commands import DownloadCommands
    worker,_,_,_,_=runtime;api=DownloadCommands(worker.engine,worker)
    selected=tmp_path/'native-folder';selected.mkdir()
    def request(operation,params,identity):
        return json.dumps({'v':1,'request_id':identity,'operation':'download.'+operation,'params':params})
    native=request('output.choose',{},'native-folder')
    calls=[]
    def picker(*_):calls.append(1);return [str(selected)]
    chosen=api.dispatch_native(native,picker,trusted_origin=True)
    assert chosen['ok'] and chosen['data']['grant_id'] and len(calls)==1
    assert api.dispatch_native(native,picker,trusted_origin=True)==chosen and len(calls)==1
    ids,resolved=candidates(worker,2)
    page=worker.query('download.resolve.list',{'session_id':resolved['session_id'],'page':1,'filter':FILTER})
    assert page['source_kind']=='direct' and len(page['items'])==2 and all(len(row['id'])<=64 for row in page['items'])
    started=api.dispatch(request('start',{'candidate_ids':ids,'options':{**asdict(DownloadOptions()),'model_ids':[]},
                                'directory_grant_id':chosen['data']['grant_id']},'start-two'),trusted_origin=True)
    assert started['ok'] and started['data']['count']==2
    eventually(lambda:detail(worker,started['data']['task_id'])['state']=='completed')
    assert worker.query('download.resolve.status',{'job_id':resolved['job_id']})['state']=='succeeded'


def test_repeated_resolution_preserves_both_catalogs(runtime):
    worker,_,_,_,_=runtime
    first,one=candidates(worker,2);second,two=candidates(worker,2)
    assert set(first)==set(second) and len(second)==2
    for resolution in (one,two):
        page=worker.query('download.resolve.list',{'session_id':resolution['session_id'],'page':1,'filter':FILTER})
        assert page['total']==page['total_read']==2 and {row['id'] for row in page['items']}==set(first)


def test_verified_media_duration_replaces_provider_estimate_for_subtitle_edits(runtime):
    from dataclasses import replace
    worker,provider,_,_,asr=runtime;resolve=provider.resolve
    def estimated(*args,**kwargs):
        page=resolve(*args,**kwargs);candidate=replace(page.candidates[0],duration_ms=1000)
        provider.issued[-1]=candidate
        return ResolvePage((candidate,),None,False,'complete')
    provider.resolve=estimated
    def transcribe(*args,**kwargs):return (SubtitleSegment('near_tail',0,1390,'音轨尾段',.7),)
    asr.transcribe=transcribe
    ids,_=candidates(worker);task=batch(worker,ids,asr_language='zh',model_ids=(MODEL,))
    eventually(lambda:detail(worker,task)['state']=='completed')
    original=detail(worker,task)['items'][0]['subtitles'][0]['resource_id']
    params={'resource_id':original,'origin':'asr','cursor':None}
    assert worker.query('download.subtitle.read',params)['segments'][0]['end_ms']==1390
    with worker.engine.connect() as connection:
        from yijianjifa.storage.models import DownloadCandidateRow
        assert connection.execute(select(DownloadCandidateRow.duration_ms).where(DownloadCandidateRow.id==ids[0])).scalar_one()==1400
    restored,_=candidates(worker)
    assert restored==ids and worker.query('download.subtitle.read',params)['segments'][0]['end_ms']==1390
    command(worker,'subtitle.save',{'resource_id':original,'origin':'asr','expected_revision':0,
                                  'patches':[{'id':'near_tail','text':'保留尾段'}]})
    assert worker.query('download.subtitle.read',params)['segments'][0]['text']=='保留尾段'


def test_decision_is_available_while_other_items_are_running(runtime):
    worker,provider,fetcher,_,_=runtime;ids,_=candidates(worker,2)
    original=provider.media
    def media(candidate,*,cancel):
        source=original(candidate,cancel=cancel)
        return MediaSource(candidate.id,'unknown' if candidate is provider.issued[0] else 'clean')
    provider.media=media;fetcher.release.clear()
    task=batch(worker,ids)
    eventually(lambda:fetcher.active==1)
    status=eventually(lambda:detail(worker,task) if any(row['decision_required'] for row in detail(worker,task)['items']) else None)
    assert status['state']=='running'
    waiting=next(row for row in status['items'] if row['decision_required'])
    command(worker,'decision',{'task_id':task,'item_id':waiting['id'],'choice':'original'})
    fetcher.release.set()
    eventually(lambda:detail(worker,task)['state']=='completed')


def test_local_processing_has_one_slot_and_cleanup_is_real(runtime):
    worker,_,fetcher,models,_=runtime;ids,_=candidates(worker,2)
    processor=worker.processor;entered=threading.Event();release=threading.Event();lock=threading.Lock()
    state={'active':0,'maximum':0,'calls':0}
    def process(*args,**kwargs):
        with lock:
            state['active']+=1;state['calls']+=1;state['maximum']=max(state['maximum'],state['active'])
        entered.set()
        try:
            while not release.wait(.01):
                if kwargs['cancel'].cancelled():raise DownloadError('cancelled')
            return processor.run(*args,**kwargs)
        finally:
            with lock:state['active']-=1
    worker.processor=type('Processor',(),{'run':staticmethod(process)})()
    task=batch(worker,ids,audio_format='wav',silent_video=True)
    assert entered.wait(3)
    model=command(worker,'models.download',{'model_id':MODEL})
    time.sleep(.1)
    assert state['calls']==1 and not models.install_entered.is_set()
    release.set()
    eventually(lambda:detail(worker,task)['state']=='completed')
    eventually(lambda:worker.query('download.detail',{'job_id':model['job_id']})['state']=='succeeded')
    assert state['maximum']==1 and state['calls']==4 and fetcher.calls==2
    assert worker.files._values('temporary')==[]
    folder=Path(worker.query('download.output.read',{})['display_path'])
    assert not list(folder.glob('.yijianjifa-download-*/temporary-*'))


def test_query_uses_committed_cache_without_file_or_model_hashes(runtime,monkeypatch):
    worker,_,fetcher,models,_=runtime;ids,resolved=candidates(worker)
    task=batch(worker,ids);eventually(lambda:detail(worker,task)['state']=='completed')
    def forbidden(*args,**kwargs):raise AssertionError('bridge query invoked slow work')
    monkeypatch.setattr(worker.files,'_hash',forbidden)
    monkeypatch.setattr(worker.files,'path_for',forbidden)
    monkeypatch.setattr(worker.models,'list',forbidden)
    monkeypatch.setattr(worker.models,'require',forbidden)
    began=time.monotonic()
    for operation,params in [('detail',{'task_id':task}),('list',{'page':1,'filter':FILTER}),
        ('resolve.list',{'session_id':resolved['session_id'],'page':1,'filter':FILTER}),('models.list',{}),('settings.read',{}),('output.read',{})]:
        result=worker.query('download.'+operation,params)
        assert 'backend_path' not in json.dumps(result) and 'sha256' not in json.dumps(result)
    assert time.monotonic()-began<.5


def test_resume_while_another_item_runs_and_cancellation_keeps_success(runtime):
    worker,provider,fetcher,_,_=runtime;ids,_=candidates(worker,2)
    original=provider.media
    def media(candidate,*,cancel):
        if candidate is provider.issued[0]:raise DownloadError('verification_required')
        return original(candidate,cancel=cancel)
    provider.media=media;fetcher.release.clear()
    task=batch(worker,ids)
    eventually(lambda:fetcher.active==1)
    eventually(lambda:any(row['waiting_reason']=='auth' for row in detail(worker,task)['items']))
    provider.media=original
    command(worker,'resume',{'task_id':task,'show_browser':False})
    eventually(lambda:fetcher.active==2)
    fetcher.release.set()
    eventually(lambda:detail(worker,task)['state']=='completed')


def reopen(worker,provider,fetcher,models,asr):
    from yijianjifa.app_services.download_runtime import DownloadRuntime
    other=DownloadRuntime(worker.engine,worker.paths,media=worker.media,providers={'direct':provider},
                          fetcher=fetcher,models=models,asr=asr)
    other.start();eventually(lambda:other.recovery_complete)
    return other


def test_queued_cancel_settles_stages_and_reopen_preserves_cancelled(runtime):
    from yijianjifa.app_services.download_runtime import DownloadRuntime
    worker,provider,fetcher,models,asr=runtime;ids,_=candidates(worker)
    grant=worker.query('download.output.read',{})['grant_id']
    assert eventually(lambda:worker.close(.05))
    # A real committed intent cancelled before there is a scheduler/first dispatch.
    queued=DownloadRuntime(worker.engine,worker.paths,media=worker.media,providers={'direct':provider},
                           fetcher=fetcher,models=models,asr=asr)
    try:
        task=command(queued,'start',{'candidate_ids':ids,'options':{**asdict(DownloadOptions()),'model_ids':[]},
                                     'directory_grant_id':grant})['task_id']
        command(queued,'cancel',{'task_id':task});queued._cycle()
        status=detail(queued,task)
        assert status['state']=='cancelled' and status['counts']['cancelled']==1
        assert status['items'][0]['stages']['video']['state']=='cancelled'
        assert fetcher.calls==asr.calls==0
    finally:assert queued.close()
    other=reopen(worker,provider,fetcher,models,asr)
    try:
        status=detail(other,task)
        assert status['state']=='cancelled' and status['counts']['cancelled']==1
        assert status['items'][0]['stages']['video']['state']=='cancelled'
        time.sleep(.1);assert fetcher.calls==asr.calls==0
    finally:assert eventually(lambda:other.close(.05))


@pytest.mark.parametrize('control',['resume','retry'])
def test_paused_cancel_preserves_success_and_only_explicit_control_resumes(runtime,control):
    worker,provider,fetcher,models,_=runtime;ids,_=candidates(worker);models.ready=False
    task=batch(worker,ids,asr_language='zh',model_ids=(MODEL,))
    eventually(lambda:detail(worker,task)['state']=='paused')
    item=detail(worker,task)['items'][0]
    video=item['stages']['video']['resource_id'];path=worker.files.path_for(video);before=digest(path)
    command(worker,'cancel',{'task_id':task})
    eventually(lambda:detail(worker,task)['state']=='cancelled')
    status=detail(worker,task)
    assert status['items'][0]['stages']['asr']['state']=='cancelled'
    assert status['items'][0]['stages']['video']['resource_id']==video and digest(path)==before
    assert status['counts']['partial']==1 and status['counts']['waiting']==0
    assert eventually(lambda:worker.close(.05))
    replacement=Fetcher(fetcher.source);speech=Asr()
    other=reopen(worker,provider,replacement,models,speech)
    try:
        assert detail(other,task)['state']=='cancelled'
        assert detail(other,task)['items'][0]['stages']['video']['resource_id']==video and digest(path)==before
        time.sleep(.1);assert replacement.calls==speech.calls==0
        with other.engine.connect() as connection:assert other._control(connection,task)['cancel'] is True
        params=({'task_id':task,'show_browser':False} if control=='resume' else
                {'task_id':task,'item_ids':[item['id']],'stages':['asr']})
        models.ready=True;command(other,control,params)
        eventually(lambda:detail(other,task)['state']=='completed')
        with other.engine.connect() as connection:assert other._control(connection,task)['cancel'] is False
        assert replacement.calls==0 and speech.calls==1 and digest(path)==before
    finally:assert eventually(lambda:other.close(.05))


def test_fully_settled_cancelled_reopen_retains_terminal_history(runtime):
    worker,provider,fetcher,models,asr=runtime;ids,_=candidates(worker);fetcher.release.clear()
    task=batch(worker,ids);assert fetcher.entered.wait(3)
    command(worker,'cancel',{'task_id':task})
    eventually(lambda:detail(worker,task)['state']=='cancelled')
    before=detail(worker,task)
    assert before['counts']['cancelled']==1 and before['items'][0]['stages']['video']['state']=='cancelled'
    assert fetcher.active==0 and eventually(lambda:worker.close(.05))
    replacement=Fetcher(fetcher.source)
    other=reopen(worker,provider,replacement,models,asr)
    try:
        status=detail(other,task)
        assert status['state']=='cancelled' and status['counts']==before['counts']
        assert status['items'][0]['stages']==before['items'][0]['stages']
        time.sleep(.1);assert replacement.calls==asr.calls==0
    finally:assert eventually(lambda:other.close(.05))


def test_watermark_decision_cannot_clear_explicit_cancel(runtime):
    worker,provider,fetcher,_,_=runtime;ids,_=candidates(worker);provider.watermark='unknown'
    task=batch(worker,ids);eventually(lambda:detail(worker,task)['state']=='paused')
    item=detail(worker,task)['items'][0]['id']
    command(worker,'cancel',{'task_id':task});eventually(lambda:detail(worker,task)['state']=='cancelled')
    with pytest.raises(DownloadError) as error:
        command(worker,'decision',{'task_id':task,'item_id':item,'choice':'original'})
    assert error.value.code=='input_limit' and detail(worker,task)['state']=='cancelled'
    time.sleep(.1);assert fetcher.calls==0
    with worker.engine.connect() as connection:assert worker._control(connection,task)['cancel'] is True
    command(worker,'resume',{'task_id':task,'show_browser':False})
    command(worker,'decision',{'task_id':task,'item_id':item,'choice':'original'})
    eventually(lambda:detail(worker,task)['state']=='completed')
    assert fetcher.calls==1


def test_close_returns_false_until_worker_stops_then_reopen_needs_explicit_resume(runtime):
    worker,provider,fetcher,models,_=runtime;ids,_=candidates(worker)
    entered=threading.Event();release=threading.Event()
    class SlowAsr:
        def transcribe(self,*_,cancel,**__):
            entered.set();assert release.wait(3)
            if cancel.cancelled():raise DownloadError('cancelled')
    worker.asr=SlowAsr();task=batch(worker,ids,asr_language='zh',model_ids=(MODEL,))
    assert entered.wait(3)
    video=detail(worker,task)['items'][0]['outputs'][0]['id']
    original=worker.files.path_for(video);before=digest(original)
    assert worker.close(.01) is False
    release.set();eventually(lambda:worker.close(.05))
    assert worker.files._values('temporary')==[] and digest(original)==before
    replacement=Fetcher(fetcher.source);asr=Asr()
    other=reopen(worker,Provider(),replacement,models,asr)
    try:
        assert detail(other,task)['state']=='interrupted'
        time.sleep(.1);assert replacement.calls==asr.calls==0
        command(other,'resume',{'task_id':task,'show_browser':False})
        eventually(lambda:detail(other,task)['state']=='completed')
        assert replacement.calls==0 and asr.calls==1 and digest(original)==before
    finally:assert eventually(lambda:other.close(.05))


def test_recovery_revalidates_success_missing_video_and_only_repeats_missing_stage(runtime):
    worker,_,fetcher,models,_=runtime;ids,_=candidates(worker)
    task=batch(worker,ids,audio_format='wav')
    eventually(lambda:detail(worker,task)['state']=='completed')
    item=detail(worker,task)['items'][0]
    video=next(row['id'] for row in item['outputs'] if row['kind']=='video')
    audio=next(row['id'] for row in item['outputs'] if row['kind']=='audio_wav')
    audio_path=worker.files.path_for(audio);audio_before=digest(audio_path)
    worker.files.path_for(video).unlink()
    assert eventually(lambda:worker.close(.05))
    replacement=Fetcher(fetcher.source)
    other=reopen(worker,Provider(),replacement,models,Asr())
    try:
        status=detail(other,task)
        assert status['state']=='interrupted' and status['items'][0]['stages']['video']['state']=='interrupted'
        assert status['items'][0]['stages']['audio']['state']=='succeeded'
        time.sleep(.1);assert replacement.calls==0
        # Direct URL handles deliberately do not survive restart. User rereads
        # the input to restore provider authority before explicit continuation.
        restored,_=candidates(other)
        assert restored==ids
        command(other,'resume',{'task_id':task,'show_browser':False})
        eventually(lambda:detail(other,task)['state']=='completed')
        assert replacement.calls==1 and digest(audio_path)==audio_before
        assert detail(other,task)['items'][0]['stages']['audio']['resource_id']==audio
    finally:assert eventually(lambda:other.close(.05))


def test_actual_preview_import_reveal_and_delete_jobs_are_terminal(runtime,monkeypatch):
    worker,_,_,_,_=runtime;ids,_=candidates(worker)
    task=batch(worker,ids);eventually(lambda:detail(worker,task)['state']=='completed')
    resource=detail(worker,task)['items'][0]['outputs'][0]['id']
    path=worker.files.path_for(resource);before=digest(path)
    playback=command(worker,'preview',{'resource_id':resource})
    shown=eventually(lambda:worker.query('download.preview.status',{'job_id':playback['job_id']}) if
                     worker.query('download.preview.status',{'job_id':playback['job_id']})['state']=='succeeded' else None)
    assert shown['resource_id']==resource and shown['url'].startswith('p011media://asset/')
    assert command(worker,'preview',{'resource_id':resource})['url']==shown['url']
    seen=[];monkeypatch.setattr('yijianjifa.os_integration.reveal.reveal_path',lambda selected:seen.append(selected))
    revealed=command(worker,'reveal',{'resource_id':resource})
    eventually(lambda:worker.query('download.detail',revealed)['state']=='succeeded')
    assert seen==[path]
    imported=command(worker,'import',{'resource_id':resource,'category_ids':[]})
    imported=eventually(lambda:worker.query('download.detail',imported) if worker.query('download.detail',imported)['state']=='succeeded' else None)
    assert imported['library_id'] and digest(path)==before
    # Imported assets retain a reference and intentionally block physical delete.
    refused=command(worker,'file.delete',{'resource_id':resource})
    refused=eventually(lambda:worker.query('download.detail',refused) if worker.query('download.detail',refused)['state']=='failed' else None)
    assert refused['error_code']=='identity_mismatch' and digest(path)==before


def test_export_uses_frozen_revision_and_owned_physical_id(runtime,tmp_path):
    worker,_,_,_,_=runtime;ids,_=candidates(worker)
    task=batch(worker,ids,asr_language='zh',model_ids=(MODEL,));eventually(lambda:detail(worker,task)['state']=='completed')
    original=detail(worker,task)['items'][0]['subtitles'][0]['resource_id']
    params={'resource_id':original,'origin':'asr','format':'srt'}
    target=tmp_path/'export.srt';native=worker.prepare_native('download.subtitle.export',params,[str(target)])
    with worker.engine.begin() as connection:exported=worker.enqueue(connection,'download.subtitle.export',params,native=native)
    worker.wake()
    result=eventually(lambda:worker.query('download.detail',exported) if worker.query('download.detail',exported)['state']=='succeeded' else None)
    assert result['resource_id']!=original and target.is_file() and '00:00:00,500' in target.read_text(encoding='utf-8')
    assert worker.repository.get_output(result['resource_id']).kind=='subtitle_srt'
    conflict=tmp_path/'frozen-conflict.srt';native=worker.prepare_native('download.subtitle.export',params,[str(conflict)])
    worker.local_slot.acquire()
    try:
        with worker.engine.begin() as connection:queued=worker.enqueue(connection,'download.subtitle.export',params,native=native)
        command(worker,'subtitle.save',{'resource_id':original,'origin':'asr','expected_revision':0,
                         'patches':[{'id':'asr_segment','text':'提交后新增编辑'}]})
        worker.wake()
    finally:worker.local_slot.release()
    rejected=eventually(lambda:worker.query('download.detail',queued) if worker.query('download.detail',queued)['state']=='failed' else None)
    assert rejected['error_code']=='subtitle_conflict' and not conflict.exists()
    assert worker.files._values('temporary')==[]


@pytest.mark.parametrize('selected',[[],['a','b'],[42]])
def test_native_picker_rejects_empty_multiple_or_non_path(runtime,selected):
    worker,*_=runtime
    with pytest.raises(DownloadError) as error:worker.prepare_native('download.output.choose',{},selected)
    assert error.value.code=='input_limit'


def test_native_import_identity_is_checked_after_commit(runtime,tmp_path):
    worker,_,_,models,_=runtime
    selected=tmp_path/'model.bin';selected.write_bytes(b'isolated-model')
    params={'model_id':MODEL};native=worker.prepare_native('download.models.import',params,[str(selected)])
    worker.local_slot.acquire()
    try:
        with worker.engine.begin() as connection:queued=worker.enqueue(connection,'download.models.import',params,native=native)
        replacement=tmp_path/'replacement.bin';replacement.write_bytes(b'replacement');replacement.replace(selected)
        worker.wake()
    finally:worker.local_slot.release()
    rejected=eventually(lambda:worker.query('download.detail',queued) if worker.query('download.detail',queued)['state']=='failed' else None)
    assert rejected['error_code']=='identity_mismatch'


def test_new_auxiliary_inputs_and_results_are_finite_and_owned(runtime):
    from yijianjifa.features.downloads.contracts import OutputResource,StageResult
    worker,_,_,_,_=runtime;ids,_=candidates(worker,2)
    task=batch(worker,ids);eventually(lambda:detail(worker,task)['state']=='completed')
    items=detail(worker,task)['items'];resource=items[0]['outputs'][0]['id']
    for kind in ('resource_preview','resource_reveal','resource_delete'):
        with worker.engine.begin() as connection:
            with pytest.raises(DownloadError) as error:worker.repository.create_auxiliary_job(connection,kind=kind,output_resource_id=resource,candidate_id=ids[1])
            assert error.value.code=='identity_mismatch'
            with pytest.raises(DownloadError):worker.repository.create_auxiliary_job(connection,kind=kind,output_resource_id=resource,model_id=MODEL)
            job=worker.repository.create_auxiliary_job(connection,kind=kind,output_resource_id=resource)
            with pytest.raises(DownloadError):worker.repository.record_auxiliary_job(connection,job,state='succeeded',resource_id=resource)
            worker.repository.record_auxiliary_job(connection,job,state='succeeded')
    with worker.engine.begin() as connection:
        with pytest.raises(DownloadError):worker.repository.reset_stage(connection,items[0]['id'],'video',intent='automatic')
        worker.repository.reset_stage(connection,items[0]['id'],'video',intent='resume')
    assert worker.repository.summary(task)['items'][0]['stages']['video']['state']=='succeeded'


def test_browser_work_and_cleanup_keep_owner_thread_and_verification_is_explicit(runtime,request):
    from types import SimpleNamespace
    worker,_,_,_,_=runtime
    calls=[];closed=threading.Event();finish=threading.Event()
    request.addfinalizer(finish.set)
    class Lease:
        visible=True;closed=False;cleanup_errors=();verification_required=False;interactive=False;verification_event=None
        def __init__(self):self.owner=threading.get_ident();self.begins=0;self.completes=0;self.closes=0
        def check(self):assert self.owner==threading.get_ident()
        def begin_verification(self,url):
            self.check();self.begins+=1;self.interactive=self.verification_required=True
            self.verification_event=SimpleNamespace(state='required')
        def complete_verification(self,*,cancel):
            self.check();self.completes+=1;self.interactive=self.verification_required=False
            self.verification_event=SimpleNamespace(state='resolved')
        def read(self,url,*,cancel):
            self.check();calls.append(url)
            return {'status_code':0,'_catalog_source':'isolated-profile','_catalog_request_cursor':'0','max_cursor':'1','has_more':False,
                    'aweme_list':[{'aweme_id':'12345','author':{'sec_uid':'isolated-profile','nickname':'隔离作者'},
                    'desc':'本地受控目录','video':{'duration':1400,'play_addr':{'url_list':['https://example.com/controlled.mp4?private=hidden']}}}]}
        def read_next(self,url,*,cancel):return self.read(url,cancel=cancel)
        def close(self):
            self.check();self.closes+=1;self.closed=True
            if self.closes==1:self.cleanup_errors=('context_cleanup_failed',);closed.set()
            else:assert finish.wait(3);self.cleanup_errors=()
    class Pool:
        def acquire(self,_):self.lease=Lease();return self.lease
    pool=Pool();worker._browser_pool=pool
    params={'input':'https://www.douyin.com/user/isolated-profile','show_browser':True}
    first=command(worker,'resolve.start',params)
    failed=eventually(lambda:worker.query('download.resolve.status',{'job_id':first['job_id']}) if
                      worker.query('download.resolve.status',{'job_id':first['job_id']})['state']=='failed' else None)
    assert failed['verification_required'] and calls==[] and pool.lease.begins==1
    for _ in range(2):
        result=command(worker,'resolve.start',params)
        eventually(lambda:worker.query('download.resolve.status',{'job_id':result['job_id']})['state']=='succeeded')
        page=worker.query('download.resolve.list',{'session_id':result['session_id'],'page':1,'filter':FILTER})
        assert page['source_kind']=='douyin_profile' and page['total']==1 and page['items'][0]['work_id']=='12345'
    assert pool.lease.begins==pool.lease.completes==1
    assert pool.lease.owner!=threading.get_ident()
    assert worker.close(.01) is False
    assert closed.wait(2)
    finish.set();assert eventually(lambda:worker.close(.05))
    assert pool.lease.closes>=2


def test_failed_browser_constructor_is_retried_on_shutdown_without_fresh_launch(runtime,request,monkeypatch):
    import yijianjifa.features.downloads.browser as browser
    worker,*_=runtime
    ready=threading.Event();retried=threading.Event();contexts=[];calls=[]
    request.addfinalizer(ready.set)
    class Proxy:
        url='http://127.0.0.1:1'
        def __init__(self,*_):pass
        def close(self):calls.append(('proxy',threading.get_ident()))
    monkeypatch.setattr(browser,'PublicConnectProxy',Proxy)
    class Context:
        def __init__(self):self.owner=threading.get_ident();self.closes=0;contexts.append(self)
        def route(self,*_):raise RuntimeError('SECRET route')
        def close(self):
            assert threading.get_ident()==self.owner;self.closes+=1;calls.append(('context',self.owner))
            if self.closes>1:retried.set()
            if not ready.is_set():raise RuntimeError('SECRET context')
    pool=browser.DownloadBrowserPool(worker.paths.data_dir,launch=lambda *a,**k:Context())
    worker._browser_pool=pool
    class Cancel:
        def cancelled(self):return True
    with pytest.raises(DownloadError) as error:worker._platform_executor.submit(worker._browser_lease).result()
    assert error.value.code=='network' and worker._lease is None and len(contexts)==1
    assert getattr(pool,'_pending_lease',None) is not None and pool.anonymous_lock.locked()
    with pytest.raises(DownloadError) as error:worker._platform_executor.submit(worker._browser_lease,cancel=Cancel()).result()
    assert error.value.code=='cancelled' and len(contexts)==1
    assert worker.close(.01) is False
    assert retried.wait(2) and pool.anonymous_lock.locked()
    with pytest.raises(DownloadError) as error:worker._platform_executor.submit(worker._browser_lease).result()
    assert error.value.code=='cancelled' and len(contexts)==1
    ready.set();assert eventually(lambda:worker.close(.05))
    assert pool._pending_lease is None and not pool.anonymous_lock.locked()
    assert len(contexts)==1 and contexts[0].closes>=2
    assert {identity for _,identity in calls}=={contexts[0].owner}
    assert contexts[0].owner!=threading.get_ident()


def test_profile_continuation_is_bound_and_members_are_unique(runtime):
    worker,*_=runtime
    class Profile:
        def resolve(self,source,cursor,*,cancel):
            assert source.kind=='douyin_profile'
            numbers=range(1,21) if cursor is None else range(11,31)
            rows=tuple(Candidate('douyin:'+str(i),'douyin',str(i),source.source_key,
                      'https://www.douyin.com/video/'+str(i),title='作品'+str(i)) for i in numbers)
            return ResolvePage(rows,'native-page' if cursor is None else None,cursor is None,'partial' if cursor is None else 'complete')
    worker.providers['douyin']=Profile()
    first=command(worker,'resolve.start',{'input':'https://www.douyin.com/user/isolated'})
    eventually(lambda:worker.query('download.resolve.status',{'job_id':first['job_id']})['state']=='succeeded')
    page=worker.query('download.resolve.list',{'session_id':first['session_id'],'page':1,'filter':FILTER})
    assert page['source_kind']=='douyin_profile' and page['total_read']==20 and page['completion']=='partial' and page['has_more']
    cursor=page['next_cursor'];assert len(cursor)==32 and cursor!='native-page'
    second=command(worker,'resolve.start',{'session_id':first['session_id'],'cursor':cursor})
    eventually(lambda:worker.query('download.resolve.status',{'job_id':second['job_id']})['state']=='succeeded')
    page=worker.query('download.resolve.list',{'session_id':first['session_id'],'page':2,'filter':FILTER})
    assert len(page['items'])==10 and page['total_read']==page['total']==30 and page['total_pages']==2
    assert page['completion']=='complete' and page['has_more'] is False and page['next_cursor'] is None
    with pytest.raises(DownloadError) as error:command(worker,'resolve.start',{'session_id':first['session_id'],'cursor':cursor})
    assert error.value.code=='source_missing'


def test_background_preview_ttl_uses_owner_cleanup_and_revokes_cached_url(runtime):
    worker,*_=runtime;ids,resolved=candidates(worker)
    preview=command(worker,'preview.prepare',{'candidate_id':ids[0]})
    result=eventually(lambda:worker.query('download.preview.status',preview) if worker.query('download.preview.status',preview)['state']=='succeeded' else None)
    identity=result['resource_id'];path=worker.files.path_for(identity)
    authorized=command(worker,'preview',{'resource_id':identity})
    eventually(lambda:worker.query('download.preview.status',authorized)['state']=='succeeded')
    journal=worker.files._journal(identity);journal['last_access']=worker.files.clock()-86401
    with worker.engine.begin() as connection:worker.files._save(connection,'journal',identity,journal)
    worker.clock=lambda:time.monotonic()+3601;worker.wake()
    eventually(lambda:worker.files._journal(identity)['state']=='deleted')
    assert not path.exists() and identity not in worker._urls
    page=worker.query('download.resolve.list',{'session_id':resolved['session_id'],'page':1,'filter':FILTER})
    assert page['items'][0]['state']=='not_downloaded'


def test_legacy_unfinished_auxiliary_without_control_is_interrupted(runtime):
    worker,provider,fetcher,models,asr=runtime
    assert eventually(lambda:worker.close(.05))
    with worker.engine.begin() as connection:old=worker.repository.create_auxiliary_job(connection,kind='model_download',model_id=MODEL)
    other=reopen(worker,provider,fetcher,models,asr)
    try:
        assert other.query('download.detail',{'job_id':old})['state']=='interrupted'
        time.sleep(.1);assert not models.install_entered.is_set()
    finally:assert eventually(lambda:other.close(.05))


def actual_recognition(worker,tmp_path):
    from yijianjifa.features.downloads.models import ModelStore
    from yijianjifa.features.downloads.asr import AsrEngine
    from yijianjifa.features.downloads.ocr import OcrEngine,DEFAULT_OCR_MODEL_IDS
    prep=Path(__file__).resolve().parents[3]/'build/recognition-preparation'
    cli=prep/'macos-whisper-cpu-build/bin/whisper-cli'
    if sys.platform!='darwin' or not cli.is_file():pytest.skip('prepared Mac CPU CLI unavailable: real Runtime acceptance pending')
    store=ModelStore(prep/'assets')
    try:
        for identity in (MODEL,*DEFAULT_OCR_MODEL_IDS):store.require(identity)
    except DownloadError:pytest.skip('explicit verified model assets missing: real Runtime acceptance pending')
    worker.models=store;store.network_slots=worker.network_slots
    worker.asr=AsrEngine(store,tool=cli,scratch_root=tmp_path/'real-asr-scratch',timeout=180)
    worker.ocr=OcrEngine(store,timeout=180)
    return prep


def test_actual_cpu_asr_and_ocr_runtime_owned_outputs_and_originals(runtime,tmp_path,monkeypatch):
    if not importlib.util.find_spec('rapidocr') or not importlib.util.find_spec('onnxruntime'):
        pytest.skip('independent recognition runtime absent: actual CPU composition pending')
    from tests.integration.downloads.test_ocr_engine import burned_video
    from tests.integration.downloads.fixtures import probe
    from yijianjifa.os_integration.resources import ffmpeg_path
    from yijianjifa.app_services.download_commands import MODEL_IDS
    worker,_,fetcher,_,_=runtime;actual_recognition(worker,tmp_path)
    burned=burned_video(tmp_path/'burned');speech=tmp_path/'reference.aiff'
    reference='今天我们测试本地语音识别。'
    subprocess.run(['/usr/bin/say','-v','Tingting','-r','155','-o',str(speech),reference],capture_output=True,check=True,timeout=30)
    duration=float(probe(speech)['format']['duration'])
    sample=tmp_path/'runtime-synthetic.mp4'
    subprocess.run([str(ffmpeg_path()),'-nostdin','-v','error','-y','-stream_loop','-1','-i',str(burned),'-i',str(speech),
                    '-t',str(duration),'-c:v','libx264','-c:a','aac',str(sample)],capture_output=True,check=True,timeout=30)
    assert abs(float(probe(sample)['format']['duration'])-duration)<.15
    fetcher.source=sample;before=digest(sample);children=[];popen=subprocess.Popen
    def tracked(*args,**kwargs):
        child=popen(*args,**kwargs);children.append(child);return child
    monkeypatch.setattr(subprocess,'Popen',tracked)
    ids,_=candidates(worker)
    task=batch(worker,ids,asr_language='zh',ocr_enabled=True,model_ids=tuple(MODEL_IDS))
    result=eventually(lambda:detail(worker,task) if detail(worker,task)['state'] in ('completed','completed_with_errors','paused','paused_auth') else None,timeout=60)
    assert result['state']=='completed',result
    item=result['items'][0]
    assert {row['kind'] for row in item['outputs']}=={'video','asr','ocr'}
    assert {row['origin'] for row in item['subtitles']}=={'asr','ocr'}
    originals={row['origin']:row['resource_id'] for row in item['subtitles']}
    texts={origin:worker.query('download.subtitle.read',{'resource_id':identity,'origin':origin,'cursor':None}) for origin,identity in originals.items()}
    assert '本地语音识别' in ''.join(row['text'] for row in texts['asr']['segments'])
    assert '中文识别测试' in [row['text'] for row in texts['ocr']['segments']]
    assert all(row['draft_id'] and row['revision']==0 for row in texts.values())
    assert digest(sample)==before and worker.files._values('temporary')==[]
    assert children and all(child.poll() is not None for child in children)
    for child in children:
        with pytest.raises(ProcessLookupError):os.killpg(child.pid,0)
    assert worker.ocr.last_diagnostics['providers']==['CPUExecutionProvider']


def test_actual_cpu_cancel_reaps_runtime_child_and_preserves_video(runtime,tmp_path,monkeypatch):
    worker,_,fetcher,_,_=runtime;actual_recognition(worker,tmp_path)
    ids,_=candidates(worker);children=[];entered=threading.Event();popen=subprocess.Popen
    def tracked(*args,**kwargs):
        child=popen(*args,**kwargs);children.append(child)
        if args and str(args[0][0]).endswith('whisper-cli'):entered.set()
        return child
    monkeypatch.setattr(subprocess,'Popen',tracked)
    task=batch(worker,ids,asr_language='zh',model_ids=(MODEL,))
    assert entered.wait(10)
    video=next(row['id'] for row in detail(worker,task)['items'][0]['outputs'] if row['kind']=='video')
    path=worker.files.path_for(video);before=digest(path)
    command(worker,'cancel',{'task_id':task})
    eventually(lambda:detail(worker,task)['state']=='cancelled',timeout=15)
    assert digest(path)==before and worker.files._values('temporary')==[]
    assert children and all(child.poll() is not None for child in children)
    for child in children:
        with pytest.raises(ProcessLookupError):os.killpg(child.pid,0)
    assert not list((tmp_path/'real-asr-scratch').glob('*'))
