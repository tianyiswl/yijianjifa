"""Local intake uses real media and owned files; no browser/network or user DB."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from yijianjifa.app_services.download_commands import DownloadCommands
from yijianjifa.app_services.download_runtime import DownloadRuntime
from yijianjifa.app_services.media_access import MediaAccess
from yijianjifa.features.downloads.contracts import Candidate, DownloadError, DownloadOptions, Region, SubtitleSegment
from yijianjifa.features.downloads.repository import DownloadRepository
from yijianjifa.features.downloads.files import OwnedFiles
from yijianjifa.os_integration.resources import ffmpeg_path
from yijianjifa.storage.db import create_engine_for, migrate_to_head, migrate_to_revision
from yijianjifa.storage.models import DownloadCandidateRow, DownloadOutputRow, Setting
from yijianjifa.webui.host_commands import make_host
from yijianjifa.webui.media_registry import DropRegistry
from tests.integration.downloads.fixtures import make_video, digest, probe
from tests.integration.downloads.test_runtime import Models, Asr, eventually, command

FILTER={'query':'','state':''}

def options_dict(options):return json.loads(json.dumps(asdict(options)))

class Ocr:
    def recognize(self, video, **kwargs):
        assert probe(video)['format']['duration']
        return (SubtitleSegment('screen_text', 0, 500, '受控画面识别文字', .9),)

class Forbidden:
    transport=SimpleNamespace(connector=None)
    def __getattr__(self, name):
        raise AssertionError('local intake touched a provider/network')

@pytest.fixture
def local_runtime(db_engine, app_paths, tmp_path):
    models=Models();media=MediaAccess(db_engine,app_paths)
    worker=DownloadRuntime(db_engine,app_paths,media=media,providers={'direct':Forbidden()},
        fetcher=Forbidden(),cover_fetcher=Forbidden(),models=models,asr=Asr(),ocr=Ocr())
    worker.start();eventually(lambda:worker.recovery_complete)
    yield worker,media
    assert eventually(lambda:worker.close(.05));media.close()

def receive(worker, paths, *, timeout=6):
    api=DownloadCommands(worker.engine,worker)
    raw=json.dumps(dict(v=1,request_id=os.urandom(8).hex(),operation='download.local.choose',params={}))
    result=api.dispatch_native(raw,lambda *_:paths,trusted_origin=True)
    assert result['ok'],result
    info=result['data'];eventually(lambda:worker.query('download.resolve.status',{'job_id':info['job_id']})['state'] in ('succeeded','failed'),timeout=timeout)
    return info,worker.query('download.resolve.list',{'session_id':info['session_id'],'page':1,'filter':FILTER})

def output(worker,tmp_path):
    target=tmp_path/'outputs';target.mkdir()
    native=worker.prepare_native('download.output.choose',{},target)
    with worker.engine.begin() as c:worker.enqueue(c,'download.output.choose',{},native=native)
    return worker.access.read_output()['grant_id']

@pytest.mark.parametrize('suffix,container',[('.mp4','mp4'),('.mov','mov'),('.m4v','ipod')])
def test_actual_local_container_copy_then_audio_silent_and_subtitles(local_runtime,tmp_path,suffix,container):
    worker,_=local_runtime;source=make_video(tmp_path/'input')
    selected=tmp_path/('中文 video'+suffix)
    subprocess.run([str(ffmpeg_path()),'-nostdin','-v','error','-i',str(source),'-map','0','-c','copy','-f',container,str(selected)],check=True,capture_output=True,timeout=15)
    before=digest(selected)
    info,page=receive(worker,[selected]);assert page['source_kind']=='local' and page['total']==1
    row=page['items'][0];assert row['platform']=='local' and row['state']=='received'
    rid=row['source_resource_id'];assert worker.repository.get_output(rid).kind=='source_video'
    assert digest(selected)==before
    selected.unlink()  # Stable owned copy is the processing input after intake.
    grant=output(worker,tmp_path)
    options=DownloadOptions(save_video=False,prefer_clean=False,audio_format='mp3',silent_video=True,asr_language='zh',ocr_enabled=True,
        model_ids=('whisper-small-multilingual','rapidocr-cls-mobile-v2','rapidocr-det-ppocrv6-small','rapidocr-rec-ppocrv6-small'))
    start=command(worker,'start',{'candidate_ids':[row['id']],'options':options_dict(options),'directory_grant_id':grant})
    assert not start.get('confirmation_required'), 'intake alone is not a previous processing batch'
    detail=eventually(lambda:(d if d['state']=='completed' else None) if (d:=worker.query('download.detail',{'task_id':start['task_id']})) else None,timeout=10)
    assert detail['source_mode']=='local';assert detail['title'].startswith('本地提取')
    assert set(detail['items'][0]['stages'])=={'audio','silent','asr','ocr'}
    audio=next(o for o in detail['items'][0]['outputs'] if o['kind']=='audio_mp3')
    assert probe(worker.files.path_for(audio['id']))['streams'][0]['codec_name']=='mp3'
    assert {s['origin'] for s in detail['items'][0]['subtitles']}=={'asr','ocr'}
    assert all(s['state']=='succeeded' for s in detail['items'][0]['stages'].values())

def test_intake_partial_errors_deduplicate_and_hide_external_paths(local_runtime,tmp_path):
    worker,_=local_runtime;valid=make_video(tmp_path/'valid');other=tmp_path/'duplicate.mp4';shutil.copyfile(valid,other)
    unsupported=tmp_path/'not-supported.mkv';unsupported.write_bytes(b'bytes')
    broken=tmp_path/'broken.mp4';broken.write_bytes(b'not media')
    info,page=receive(worker,[valid,other,unsupported,broken])
    status=worker.query('download.resolve.status',{'job_id':info['job_id']})
    assert status['state']=='succeeded' and page['total']==1
    assert status['received']==2 and status['failed']==2
    assert {e['code'] for e in status['errors']}=={'local_format_unsupported','media_invalid'}
    assert str(tmp_path) not in json.dumps({'page':page,'status':status})
    with worker.engine.connect() as c:
        assert all(str(tmp_path/'valid') not in r for r in c.execute(select(Setting.value_json)).scalars())

def test_local_modes_and_options_reject_platform_and_copy_only(local_runtime,tmp_path):
    worker,_=local_runtime;_,page=receive(worker,[make_video(tmp_path/'input')]);cid=page['items'][0]['id'];grant=output(worker,tmp_path)
    for options in (DownloadOptions(),DownloadOptions(save_video=False,prefer_clean=True,audio_format='mp3')):
        with pytest.raises(DownloadError):command(worker,'start',{'candidate_ids':[cid],'options':options_dict(options),'directory_grant_id':grant})
    with worker.engine.begin() as c:
        other=worker.repository.save_candidate(c,Candidate('remote','direct','remote','direct:'+'a'*64,None))
    opts=options_dict(DownloadOptions(save_video=False,prefer_clean=False,audio_format='wav'))
    with pytest.raises(DownloadError):command(worker,'start',{'candidate_ids':[cid,other],'options':opts,'directory_grant_id':grant})

def test_native_drop_receipt_replay_consumes_once_including_concurrency(local_runtime,tmp_path):
    worker,_=local_runtime;drops=DropRegistry();selected=make_video(tmp_path/'input');token=drops.grant([selected])
    commands=DownloadCommands(worker.engine,worker)
    class Api:
        _error=staticmethod(DownloadCommands.error)
        dispatch_download_native=commands.dispatch_native
        dispatch=commands.dispatch
    host=make_host(api=Api(),engine=None,window=None,webview=None,exit_started=threading.Event(),finished_files=None,
        finished_deletion=None,deletion_recovery_pending=False,cover_response=None,drops=drops,drop_access=None,media=None,excel=None,
        output=None,voice_catalog=None,volc_config=None,voices=None,play_sound=None,copy_text=None,request_exit=None)
    request=json.dumps(dict(v=1,request_id='same_drop',operation='download.local.drop',params={'token':token}))
    results=[];threads=[threading.Thread(target=lambda:results.append(host.dispatch(request,trusted_origin=True))) for _ in range(2)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(5)
    assert len(results)==2 and results[0]['ok'] and results[0]==results[1]
    with pytest.raises(ValueError):drops.consume(token)
    assert host.dispatch(request,trusted_origin=True)==results[0]
    assert not host.dispatch(request,trusted_origin=False)['ok']

def test_native_cancel_and_old_settings_shape(local_runtime):
    worker,_=local_runtime;api=DownloadCommands(worker.engine,worker)
    request=json.dumps(dict(v=1,request_id='cancel-local',operation='download.local.choose',params={}))
    calls=[]
    def picker(*_):calls.append(1);return []
    result=api.dispatch_native(request,picker,trusted_origin=True)
    assert result['data']=={'cancelled':True};assert api.dispatch_native(request,picker,trusted_origin=True)==result and calls==[1]
    params={'options':options_dict(DownloadOptions()),'filter':FILTER,'source_mode':'local'}
    command(worker,'settings.save',params);assert worker.query('download.settings.read',{})['source_mode']=='local'
    command(worker,'settings.save',{'options':options_dict(DownloadOptions()),'filter':FILTER})
    assert worker.query('download.settings.read',{})['source_mode']=='local'

def native_host(worker,*,window=None,drops=None):
    commands=DownloadCommands(worker.engine,worker)
    class Api:
        _error=staticmethod(DownloadCommands.error)
        dispatch_download_native=commands.dispatch_native
        dispatch=commands.dispatch
    return make_host(api=Api(),engine=None,window=window,webview=SimpleNamespace(FileDialog=SimpleNamespace(OPEN='open')),
        exit_started=threading.Event(),finished_files=None,finished_deletion=None,deletion_recovery_pending=False,
        cover_response=None,drops=drops,drop_access=None,media=None,excel=None,output=None,voice_catalog=None,volc_config=None,
        voices=None,play_sound=None,copy_text=None,request_exit=None)

def test_native_local_picker_formats_cancel_and_replay_keep_old_session(local_runtime,tmp_path):
    worker,_=local_runtime;info,page=receive(worker,[make_video(tmp_path/'existing')]);calls=[]
    def picker(kind,**kwargs):calls.append((kind,kwargs));return None
    host=native_host(worker,window=SimpleNamespace(create_file_dialog=picker))
    request=json.dumps(dict(v=1,request_id='host-picker',operation='download.local.choose',params={}))
    assert not host.dispatch(request,trusted_origin=False)['ok'] and calls==[]
    cancelled=host.dispatch(request,trusted_origin=True)
    assert cancelled['data']=={'cancelled':True} and host.dispatch(request,trusted_origin=True)==cancelled
    assert calls==[('open',{'allow_multiple':True,'file_types':('Video (*.mp4;*.mov;*.m4v)',)})]
    retained=worker.query('download.resolve.list',{'session_id':info['session_id'],'page':1,'filter':FILTER})
    assert retained['items'][0]['source_resource_id']==page['items'][0]['source_resource_id']

def test_native_expired_drop_actionable_and_no_frontend_path_intake(local_runtime,tmp_path):
    worker,_=local_runtime;source=make_video(tmp_path/'input');drops=DropRegistry();token=drops.grant([source]);host=native_host(worker,drops=drops)
    raw=lambda rid,p:json.dumps(dict(v=1,request_id=rid,operation='download.local.drop',params=p))
    assert not host.dispatch(raw('untrusted',{'token':token}),trusted_origin=False)['ok']
    assert token in drops._grants
    assert not host.dispatch(raw('path-injection',{'token':token,'paths':[str(source)]}),trusted_origin=True)['ok']
    assert token in drops._grants
    with drops._lock:
        stamp,paths=drops._grants[token];drops._grants[token]=(stamp-61,paths)
    expired=host.dispatch(raw('expired',{'token':token}),trusted_origin=True)
    assert not expired['ok'] and expired['error']['code']=='local_reselect_required'
    assert '重新选择' in expired['error']['message'] and '本地视频' in expired['error']['message']
    with worker.engine.connect() as c:assert c.execute(select(DownloadCandidateRow.id)).first() is None

def test_0014_migration_preserves_candidate_and_owned_references(tmp_path):
    engine=create_engine_for(tmp_path/'upgrade.db');migrate_to_revision(engine,'0014_downloads')
    repo=DownloadRepository(engine)
    with engine.begin() as c:
        cid=repo.save_candidate(c,Candidate('old','direct','old','direct:'+'b'*64,None,title='原记录'))
        tid=repo.create_batch(c,(cid,),DownloadOptions(),'old-grant',{cid:Region()})
    with engine.connect() as c:before={name:list(c.execute(text('SELECT * FROM '+name))) for name in ('download_candidates','download_items','download_stages','task_center_records')}
    migrate_to_head(engine)
    with engine.connect() as c:
        for name,rows in before.items():assert list(c.execute(text('SELECT * FROM '+name)))==rows
        assert not list(c.execute(text('PRAGMA foreign_key_check')))
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='0019_single_category'
    with engine.begin() as c:
        local=repo.save_candidate(c,Candidate('local','local','local:'+'c'*64,'local:'+'c'*64,None))
        assert repo.save_candidate(c,Candidate('other','local','local:'+'c'*64,'local:'+'c'*64,None))==local
    engine.dispose()

@pytest.mark.parametrize('change',['replace','in-place'])
def test_native_selection_identity_changes_rejected_before_receive(local_runtime,tmp_path,change):
    worker,_=local_runtime;selected=make_video(tmp_path/'selected')
    worker.local_slot.acquire()
    try:
        native=worker.prepare_native('download.local.choose',{},[selected])
        with worker.engine.begin() as c:info=worker.enqueue(c,'download.local.choose',{},native=native)
        if change=='replace':
            replacement=make_video(tmp_path/'replacement');os.replace(replacement,selected)
        else:
            with selected.open('ab') as out:out.write(b'changed')
    finally:worker.local_slot.release()
    worker.wake();eventually(lambda:worker.query('download.resolve.status',{'job_id':info['job_id']})['state']=='failed')
    status=worker.query('download.resolve.status',{'job_id':info['job_id']})
    assert status['errors'][0]['code']=='identity_mismatch' and status['received']==0
    with worker.engine.connect() as c:assert c.execute(select(DownloadOutputRow.id)).first() is None

def test_symlink_directory_empty_and_oversize_rejected_per_file(local_runtime,tmp_path):
    worker,_=local_runtime;source=make_video(tmp_path/'input');link=tmp_path/'link.mp4';link.symlink_to(source)
    folder=tmp_path/'folder.mp4';folder.mkdir();empty=tmp_path/'empty.mp4';empty.touch()
    large=tmp_path/'large.mp4'
    with large.open('wb') as stream:stream.truncate(2*1024*1024*1024+1)
    # Windows media validation includes real child processes and guarded IO.
    # Admission limits below stay exact; this is only the test observer budget.
    info,page=receive(worker,[source,link,folder,empty,large],timeout=30)
    status=worker.query('download.resolve.status',{'job_id':info['job_id']})
    assert page['total']==1 and status['received']==1 and status['failed']==4
    assert {e['code'] for e in status['errors']}=={'unsafe_source','media_invalid','size_limit'}
    assert link.is_symlink() and folder.is_dir() and large.stat().st_size==2*1024*1024*1024+1

def test_cancel_during_actual_owned_copy_preserves_original_and_unrelated(local_runtime,tmp_path,monkeypatch):
    worker,_=local_runtime;source=make_video(tmp_path/'input');before=digest(source)
    unrelated=worker.paths.cache_dir/'unrelated.txt';unrelated.write_text('preserve')
    entered=threading.Event();release=threading.Event();original=worker._progress_for
    def progress_for(key):
        callback=original(key)
        def callback_(progress):
            if progress.stage=='local_copy':entered.set();assert release.wait(3)
            callback(progress)
        return callback_
    monkeypatch.setattr(worker,'_progress_for',progress_for)
    native=worker.prepare_native('download.local.choose',{},[source])
    with worker.engine.begin() as c:info=worker.enqueue(c,'download.local.choose',{},native=native)
    worker.wake()
    try:
        assert entered.wait(2)
        command(worker,'cancel',{'job_id':info['job_id']})
        eventually(lambda:worker._events[('job',info['job_id'])].is_set())
    finally:release.set()
    eventually(lambda:worker.query('download.resolve.status',{'job_id':info['job_id']})['state']=='cancelled')
    assert digest(source)==before and unrelated.read_text()=='preserve'
    assert worker.files._values('temporary')==[]
    with worker.engine.connect() as c:assert c.execute(select(DownloadOutputRow.id)).first() is None

def test_disk_budget_stops_copy_without_touching_original(local_runtime,tmp_path,monkeypatch):
    worker,_=local_runtime;source=make_video(tmp_path/'input');before=digest(source)
    monkeypatch.setattr('yijianjifa.features.downloads.local.shutil.disk_usage',lambda _:SimpleNamespace(free=0))
    info,page=receive(worker,[source]);status=worker.query('download.resolve.status',{'job_id':info['job_id']})
    assert status['state']=='failed' and status['errors'][0]['code']=='disk_space' and page['total']==0
    assert digest(source)==before and worker.files._values('temporary')==[]

def test_200_selection_and_error_metadata_are_bounded(local_runtime,tmp_path):
    worker,_=local_runtime
    paths=[tmp_path/(('😀'*70)+str(i)+'.mkv') for i in range(200)]
    info,page=receive(worker,paths,timeout=30);status=worker.query('download.resolve.status',{'job_id':info['job_id']})
    assert status['state']=='failed' and status['failed']==200 and len(status['errors'])==200 and page['total']==0
    with worker.engine.connect() as c:
        values=list(c.execute(select(Setting.value_json).where(Setting.key.like('download-runtime:%'))).scalars())
        assert all(len(value.encode('utf-8'))<=8192 for value in values)
        assert all(str(tmp_path) not in value for value in values)
    with pytest.raises(DownloadError):worker.prepare_native('download.local.choose',{},paths+[paths[0]])

def test_restart_retains_validated_owned_source_without_original(local_runtime,tmp_path):
    worker,_=local_runtime;source=make_video(tmp_path/'input');info,page=receive(worker,[source]);original_id=page['items'][0]['source_resource_id']
    source.unlink();assert eventually(lambda:worker.close(.05))
    media=MediaAccess(worker.engine,worker.paths)
    reopened=DownloadRuntime(worker.engine,worker.paths,media=media,providers={'direct':Forbidden()},
        fetcher=Forbidden(),cover_fetcher=Forbidden(),models=Models(),asr=Asr(),ocr=Ocr())
    reopened.start()
    try:
        eventually(lambda:reopened.recovery_complete)
        restored=reopened.query('download.resolve.list',{'session_id':info['session_id'],'page':1,'filter':FILTER})
        assert restored['items'][0]['source_resource_id']==original_id and restored['items'][0]['state']=='received'
        assert reopened.repository.get_output(original_id).sha256==digest(reopened.files.path_for(original_id))
        assert reopened._local_inputs=={}
    finally:
        assert eventually(lambda:reopened.close(.05));media.close()

def test_expired_local_cache_restart_reuses_snapshot_without_original(local_runtime,tmp_path):
    worker,_=local_runtime;now=[1000.0];worker.files.clock=lambda:now[0]
    source=make_video(tmp_path/'input');info,page=receive(worker,[source]);row=page['items'][0];rid=row['source_resource_id']
    grant=worker.files._read('current','local')['grant_id'];old_identity=worker.files._read('grant',grant)['identity']
    original_hash=digest(source);assert worker.repository.get_output(rid).sha256==original_hash
    source.unlink();assert eventually(lambda:worker.close(.05));now[0]+=31*86400
    media=MediaAccess(worker.engine,worker.paths)
    files=OwnedFiles(worker.repository,worker.paths,clock=lambda:now[0])
    reopened=DownloadRuntime(worker.engine,worker.paths,media=media,files=files,providers={'direct':Forbidden()},
        fetcher=Forbidden(),cover_fetcher=Forbidden(),models=Models(),asr=Asr(),ocr=Ocr())
    reopened.start()
    try:
        eventually(lambda:reopened.recovery_complete)
        restored=reopened.query('download.resolve.list',{'session_id':info['session_id'],'page':1,'filter':FILTER})['items'][0]
        assert restored['state']=='received' and restored['source_resource_id']==rid
        assert files._read('grant',grant)['identity']==old_identity and files._read('grant',grant)['expires']>now[0]
        assert digest(files.path_for(rid))==original_hash and not source.exists()
        output_grant=output(reopened,tmp_path)
        start=command(reopened,'start',{'candidate_ids':[row['id']],
            'options':options_dict(DownloadOptions(save_video=False,prefer_clean=False,audio_format='mp3')),
            'directory_grant_id':output_grant})
        detail=eventually(lambda:(d if d['state']=='completed' else None) if (d:=reopened.query('download.detail',{'task_id':start['task_id']})) else None)
        audio=next(o for o in detail['items'][0]['outputs'] if o['kind']=='audio_mp3')
        assert probe(files.path_for(audio['id']))['streams'][0]['codec_name']=='mp3'
    finally:
        assert eventually(lambda:reopened.close(.05));media.close()

@pytest.mark.parametrize('replacement',['directory','symlink'])
def test_expired_local_cache_restart_rejects_replaced_directory(local_runtime,tmp_path,replacement):
    worker,_=local_runtime;now=[1000.0];worker.files.clock=lambda:now[0]
    source=make_video(tmp_path/'input');info,page=receive(worker,[source]);rid=page['items'][0]['source_resource_id']
    grant=worker.files._read('current','local')['grant_id'];before=worker.files._read('grant',grant)
    assert eventually(lambda:worker.close(.05));now[0]+=31*86400
    original_cache=tmp_path/'original-cache';worker.paths.cache_dir.rename(original_cache)
    replacement_dir=tmp_path/'replacement';replacement_dir.mkdir();sentinel=replacement_dir/'untouched.mp4';sentinel.write_bytes(b'preserve')
    if replacement=='symlink':worker.paths.cache_dir.symlink_to(replacement_dir,target_is_directory=True)
    else:replacement_dir.rename(worker.paths.cache_dir);sentinel=worker.paths.cache_dir/'untouched.mp4'
    media=MediaAccess(worker.engine,worker.paths);files=OwnedFiles(worker.repository,worker.paths,clock=lambda:now[0])
    reopened=DownloadRuntime(worker.engine,worker.paths,media=media,files=files,providers={'direct':Forbidden()},
        fetcher=Forbidden(),cover_fetcher=Forbidden(),models=Models(),asr=Asr(),ocr=Ocr())
    reopened.start()
    try:
        eventually(lambda:reopened.recovery_complete)
        row=reopened.query('download.resolve.list',{'session_id':info['session_id'],'page':1,'filter':FILTER})['items'][0]
        assert row['state']=='interrupted' and row['source_resource_id'] is None
        assert files._read('grant',grant)==before and sentinel.read_bytes()==b'preserve'
        assert original_cache.exists() and source.exists()
        with pytest.raises(DownloadError):files.path_for(rid)
    finally:
        assert eventually(lambda:reopened.close(.05));media.close()

def test_restart_incomplete_intake_requires_reselect_never_reopens_original(db_engine,app_paths,tmp_path,monkeypatch):
    source=make_video(tmp_path/'input');media=MediaAccess(db_engine,app_paths)
    old=DownloadRuntime(db_engine,app_paths,media=media,providers={'direct':Forbidden()},fetcher=Forbidden(),cover_fetcher=Forbidden(),models=Models())
    native=old.prepare_native('download.local.choose',{},[source])
    with db_engine.begin() as c:info=old.enqueue(c,'download.local.choose',{},native=native)
    assert old.close();media.close()
    def forbidden(*a,**kw):raise AssertionError('startup attempted external local selection')
    monkeypatch.setattr('yijianjifa.app_services.download_runtime.receive_file',forbidden)
    media2=MediaAccess(db_engine,app_paths)
    fresh=DownloadRuntime(db_engine,app_paths,media=media2,providers={'direct':Forbidden()},fetcher=Forbidden(),cover_fetcher=Forbidden(),models=Models())
    fresh.start()
    try:
        eventually(lambda:fresh.recovery_complete)
        status=fresh.query('download.resolve.status',{'job_id':info['job_id']})
        assert status['state']=='interrupted' and status['error_code']=='local_reselect_required'
        assert fresh._local_inputs=={}
        assert fresh.query('download.resolve.list',{'session_id':info['session_id'],'page':1,'filter':FILTER})['total']==0
    finally:
        assert eventually(lambda:fresh.close(.05));media2.close()

def test_missing_models_pause_only_recognition_and_keep_audio(local_runtime,tmp_path):
    worker,_=local_runtime;worker.models.ready=False;source=make_video(tmp_path/'input');_,page=receive(worker,[source]);grant=output(worker,tmp_path)
    opts=options_dict(DownloadOptions(save_video=False,prefer_clean=False,audio_format='wav',asr_language='zh',model_ids=('whisper-small-multilingual',)))
    start=command(worker,'start',{'candidate_ids':[page['items'][0]['id']],'options':opts,'directory_grant_id':grant})
    detail=eventually(lambda:(d if d['state']=='paused' else None) if (d:=worker.query('download.detail',{'task_id':start['task_id']})) else None)
    item=detail['items'][0]
    assert item['stages']['audio']['state']=='succeeded' and item['stages']['asr']['error_code']=='model_required'
    assert not detail['verification_required']
    audio=next(o for o in item['outputs'] if o['kind']=='audio_wav')
    assert probe(worker.files.path_for(audio['id']))['streams'][0]['codec_name']=='pcm_s16le'
    assert source.exists()

@pytest.mark.parametrize('fk',[0,1])
def test_populated_0014_upgrade_preserves_all_references_and_restores_flag(tmp_path,fk):
    from yijianjifa.features.downloads.contracts import OutputResource,StageResult,SourceInput
    engine=create_engine_for(tmp_path/'fk-upgrade.db');migrate_to_revision(engine,'0014_downloads');repo=DownloadRepository(engine)
    with engine.begin() as c:
        cid=repo.save_candidate(c,Candidate('old','direct','old','direct:'+'d'*64,None,title='原记录'))
        tid=repo.create_batch(c,(cid,),DownloadOptions(),'old-grant',{cid:Region()})
        item=c.execute(text('SELECT id FROM download_items')).scalar_one()
        repo.save_output(c,OutputResource('old-output',cid,item,'video','e'*64,100,1400),backend_path=str(Path('/isolated/old.mp4').resolve()))
        repo.record_stage(c,item,StageResult('video','succeeded','old-output'))
        repo.save_output(c,OutputResource('old-cover',cid,None,'cover','f'*64,100,None),backend_path=str(Path('/isolated/old.png').resolve()))
        repo.attach_cover(c,cid,'old-cover')
        original=repo.save_subtitle_original(c,cid,item,'asr',(SubtitleSegment('segment',0,1000,'原字幕'),))
        repo.create_subtitle_draft(c,original)
        source=SourceInput('direct','direct:'+'d'*64,'https://example.com/video.mp4')
        sid=repo.create_resolution(c,source);repo.link_source(c,cid,source,session_id=sid)
    tables=('download_candidates','download_items','download_outputs','download_stages','download_subtitle_originals','download_subtitle_drafts','download_sources','download_resolutions','task_center_records')
    with engine.connect() as c:
        before={name:list(c.execute(text('SELECT * FROM '+name))) for name in tables}
        triggers=list(c.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))
        indexes=set(c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'").scalars())
        legacy_tables=tuple(c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
        c.exec_driver_sql('PRAGMA foreign_keys=ON' if fk else 'PRAGMA foreign_keys=OFF');c.commit()
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==fk
    migrate_to_head(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==fk
        assert not list(c.exec_driver_sql('PRAGMA foreign_key_check'))
        for name,rows in before.items():assert list(c.execute(text('SELECT * FROM '+name)))==rows
        assert list(c.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))==triggers
        # Compare indexes of tables which actually existed before this upgrade.
        # New image/V2 tables have their own indexes and cannot weaken this
        # assertion that every legacy table's indexes were preserved.
        placeholders=','.join('?' for _ in legacy_tables)
        assert set(c.exec_driver_sql(f"SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN ({placeholders})",legacy_tables).scalars())==indexes|{'uq_download_local_source','uq_category_target_single'}
        with pytest.raises(Exception):c.execute(text("UPDATE download_outputs SET size_bytes=101 WHERE id='old-output'"))
        c.rollback()
        c.exec_driver_sql('PRAGMA foreign_keys=ON')
        with pytest.raises(Exception):c.execute(text("DELETE FROM download_candidates WHERE id='old'"))
    engine.dispose()

@pytest.mark.parametrize('fk',[0,1])
def test_0013_to_head_migration_restores_connection_flag(tmp_path,fk):
    engine=create_engine_for(tmp_path/'upgrade-from-0013.db');migrate_to_revision(engine,'0013_account_login_time')
    with engine.connect() as c:
        c.exec_driver_sql('PRAGMA foreign_keys=ON' if fk else 'PRAGMA foreign_keys=OFF');c.commit()
    migrate_to_head(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==fk
        assert not list(c.exec_driver_sql('PRAGMA foreign_key_check'))
        assert c.exec_driver_sql('SELECT version_num FROM alembic_version').scalar_one()=='0019_single_category'
    engine.dispose()

@pytest.mark.parametrize('fk',[0,1])
def test_failed_local_migration_rolls_back_copy_and_restores_same_connection(tmp_path,fk):
    engine=create_engine_for(tmp_path/'failed-migration.db');migrate_to_revision(engine,'0014_downloads')
    repo=DownloadRepository(engine)
    with engine.begin() as c:
        repo.save_candidate(c,Candidate('old','direct','old','direct:'+'d'*64,None,title='原记录'))
        # Existing invalid reference deliberately causes the post-copy FK check
        # to fail. This is isolated corrupt test data, never a user database.
        c.exec_driver_sql("INSERT INTO download_outputs(id,candidate_id,kind,sha256,size_bytes,backend_path,created_at) VALUES ('broken','missing','video','"+'e'*64+"',10,'/isolated/old.mp4','2026-10-06')")
    with engine.connect() as c:
        before=list(c.exec_driver_sql('SELECT * FROM download_candidates'))
        schema=list(c.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type IN ('trigger','index') ORDER BY name"))
        c.exec_driver_sql('PRAGMA foreign_keys=ON' if fk else 'PRAGMA foreign_keys=OFF');c.commit()
    with pytest.raises(RuntimeError,match='invalid reference'):migrate_to_head(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==fk
        assert c.exec_driver_sql('SELECT version_num FROM alembic_version').scalar_one()=='0014_downloads'
        assert list(c.exec_driver_sql('SELECT * FROM download_candidates'))==before
        assert list(c.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type IN ('trigger','index') ORDER BY name"))==schema
        assert c.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='_alembic_tmp_download_candidates'").first() is None
    engine.dispose()

@pytest.mark.parametrize('downgrade',[False,True])
def test_direct_alembic_fk_on_copy_refused_before_change(tmp_path,downgrade):
    from alembic import command as alembic_command
    from yijianjifa.storage.db import _alembic_config
    engine=create_engine_for(tmp_path/'direct-migration.db');migrate_to_revision(engine,'head' if downgrade else '0014_downloads')
    with engine.connect() as c:
        before_schema=list(c.exec_driver_sql("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
        c.exec_driver_sql('PRAGMA foreign_keys=ON');c.commit()
    cfg=_alembic_config()
    with pytest.raises(RuntimeError,match='guarded migration runner'):
        with engine.begin() as c:
            cfg.attributes['connection']=c
            (alembic_command.downgrade if downgrade else alembic_command.upgrade)(cfg,'0014_downloads' if downgrade else 'head')
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==1
        # Downgrade encounters the single-category transaction guard first;
        # direct unguarded migration must refuse before any DDL.
        assert c.exec_driver_sql('SELECT version_num FROM alembic_version').scalar_one()==('0019_single_category' if downgrade else '0014_downloads')
        assert list(c.exec_driver_sql("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))==before_schema
        assert c.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='_alembic_tmp_download_candidates'").first() is None
        assert not list(c.exec_driver_sql('PRAGMA foreign_key_check'))
    engine.dispose()
