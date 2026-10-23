import json
from uuid import uuid4
import pytest
from sqlalchemy import select
from yijianjifa.storage.db import create_engine_for
from yijianjifa.storage.db import migrate_to_head
from yijianjifa.storage.models import Setting
from yijianjifa.app_services.download_commands import DownloadCommands

OPTIONS=dict(save_video=True,prefer_clean=True,audio_format=None,silent_video=False,asr_language=None,ocr_enabled=False,model_ids=[])
class Runtime:
    def __init__(self):self.calls=[];self.wakes=0;self.native_calls=[]
    def enqueue(self,connection,operation,params,native=None):
        self.calls.append((operation,params,native));connection.execute(Setting.__table__.insert().values(key='testjob:'+str(len(self.calls)),value_json='{}'))
        if operation=='download.start':return dict(task_id='task1',count=len(params['candidate_ids']))
        return {'job_id':'job1'}
    def query(self,operation,params):return {'items':[], 'total':0}
    def prepare_native(self,operation,params,selected):self.native_calls.append(selected);return {'grant_id':'grant1'}
    def wake(self):self.wakes+=1
    def start(self):pass
    def close(self,timeout=0):return True
@pytest.fixture
def setup(tmp_path):
    engine=create_engine_for(tmp_path/'app.db');migrate_to_head(engine);runtime=Runtime();api=DownloadCommands(engine,runtime);yield engine,runtime,api;engine.dispose()
def raw(op,p,rid='intent'):
    return json.dumps(dict(v=1,request_id=rid,operation=op,params=p),ensure_ascii=False)
def test_download_start_replays_receipt(setup):
    engine,runtime,api=setup;p=dict(candidate_ids=['a'],options=OPTIONS,directory_grant_id='g');request=raw('download.start',p)
    first=api.dispatch(request,trusted_origin=True);assert first['ok'];assert api.dispatch(request,trusted_origin=True)==first;assert len(runtime.calls)==1
    changed=api.dispatch(raw('download.start',{**p,'candidate_ids':['b']}),trusted_origin=True);assert changed['error']['code']=='request_conflict'
    with engine.connect() as c:assert len(list(c.execute(select(Setting).where(Setting.key.like('testjob:%')))))==1
@pytest.mark.parametrize('op,params',[
 ('download.start',dict(candidate_ids=['a']*201,options=OPTIONS,directory_grant_id='g')),
 ('download.start',dict(candidate_ids=['a'],options={**OPTIONS,'bad':True},directory_grant_id='g')),
 ('download.preview',dict(resource_id='r',path='/tmp/private')),
 ('download.models.download',dict(model_id='missing')),
 ('download.resolve.list',dict(session_id='s',page=True,filter={'query':'','state':''})),
 ('download.retry',dict(task_id='t',item_ids=['i'],stages=['publish'])),
])
def test_unknown_and_unbounded_inputs_rejected(setup,op,params):
    _,r,api=setup;result=api.dispatch(raw(op,params),trusted_origin=True);assert not result['ok'];assert r.calls==[]
def test_origin_and_envelope_are_required(setup):
    _,_,api=setup;assert api.dispatch(raw('download.models.list',{}))['error']['code']=='origin_denied';assert not api.dispatch('{"v":1,"v":1}',trusted_origin=True)['ok']
def test_200_uuid_candidates_fit_bridge(setup):
    _,r,api=setup;request=raw('download.start',dict(candidate_ids=[str(uuid4()) for _ in range(200)],options=OPTIONS,directory_grant_id=str(uuid4())));assert len(request.encode())<=16384;assert api.dispatch(request,trusted_origin=True)['ok'];assert len(r.calls[0][1]['candidate_ids'])==200

def test_native_replay_does_not_reopen_picker(setup,tmp_path):
    _,r,api=setup;calls=[]
    def picker(op,p):calls.append(op);return [str(tmp_path/'selected.bin')]
    request=raw('download.models.import',{'model_id':'whisper-small-multilingual'})
    first=api.dispatch_native(request,picker,trusted_origin=True);assert first['ok'];assert api.dispatch_native(request,picker,trusted_origin=True)==first;assert len(calls)==1;assert len(r.calls)==1

def test_native_cancel_receipt_is_replayed(setup):
    _,r,api=setup;calls=[]
    def picker(*_):calls.append(1);return None
    request=raw('download.output.choose',{})
    first=api.dispatch_native(request,picker,trusted_origin=True);assert first['data']=={'cancelled':True};assert api.dispatch_native(request,picker,trusted_origin=True)==first;assert len(calls)==1;assert not r.calls

def test_all_finite_download_errors_have_chinese_messages(setup):
    from typing import get_args
    from yijianjifa.features.downloads.contracts import ErrorCode
    _,_,api=setup
    for code in get_args(ErrorCode):
        result=api.error(code,'r');assert result['error']['code']==code;assert any('\u4e00'<=c<='\u9fff' for c in result['error']['message'])

def test_desktop_routes_download_lifecycle_without_expiry_gate(db_engine,app_paths):
    from yijianjifa.app_services.desktop_api import DesktopApi
    runtime=Runtime();api=DesktopApi(db_engine,app_paths,download_runtime=runtime)
    assert api.dispatch(raw('download.list',{'page':1,'filter':{'query':'','state':''}}),trusted_origin=True)['ok']
    assert api.dispatch(raw('download.start',dict(candidate_ids=['a'],options=OPTIONS,directory_grant_id='g')),trusted_origin=True)['ok']
    api.start_local_checks();assert api.close(.2)

def download_batch(engine):
    from yijianjifa.features.downloads.contracts import Candidate,DownloadOptions,Region
    from yijianjifa.features.downloads.repository import DownloadRepository
    repo=DownloadRepository(engine)
    with engine.begin() as c:
        cid=repo.save_candidate(c,Candidate('c','direct','direct','direct:'+'a'*64,None,title='下载测试 cookie=hidden'))
        return repo.create_batch(c,(cid,),DownloadOptions(),'g',{cid:Region()})

def test_task_center_classifies_download_without_publish_review(db_engine,monkeypatch):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.app_services.publish_review import PublishReview
    def forbidden(*_):raise AssertionError('download touched publish')
    monkeypatch.setattr(PublishReview,'read',forbidden)
    tid=download_batch(db_engine);rows=TaskCenterAccess(db_engine).list('','download','',0,20)
    assert rows['total']==1;assert rows['items'][0]['kind']=='download'
    detail=TaskCenterAccess(db_engine).detail(tid);assert detail['settings']['kind']=='download';assert detail['controls']==['cancel'];assert 'hidden' not in json.dumps(detail)

def test_task_center_exposes_partially_completed_download_items(db_engine,tmp_path):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.features.downloads.contracts import Candidate,DownloadOptions,Region,OutputResource,StageResult
    from yijianjifa.features.downloads.repository import DownloadRepository
    repo=DownloadRepository(db_engine)
    with db_engine.begin() as c:
        cid=repo.save_candidate(c,Candidate('partial-c','direct','direct','direct:'+'b'*64,None,title='部分完成'))
        tid=repo.create_batch(c,(cid,),DownloadOptions(audio_format='mp3'),'g',{cid:Region()})
    item=repo.summary(tid)['items'][0]['id']
    with db_engine.begin() as c:
        output=OutputResource('audio-result',cid,item,'audio_mp3','c'*64,100,1000)
        repo.save_output(c,output,backend_path=str(tmp_path/'audio.mp3'))
        repo.record_stage(c,item,StageResult('audio','succeeded','audio-result'))
        repo.record_stage(c,item,StageResult('video','failed',error_code='media_invalid'))
    row=TaskCenterAccess(db_engine).list('','download','',0,20)['items'][0]
    assert row['partial']==1
    assert row['completed']==1 and row['succeeded']==0 and row['failed']==0
    assert TaskCenterAccess(db_engine).detail(tid)['task']['partial']==1

def test_task_center_download_hide_retains_outputs_and_never_cancels_publish(db_engine,monkeypatch,tmp_path):
    from yijianjifa.app_services.task_removal import remove_task
    from yijianjifa.app_services.publish_control import cancel_local
    from yijianjifa.features.tasks.service import TaskCenterRow
    from yijianjifa.storage.db import session_scope
    tid=download_batch(db_engine)
    from yijianjifa.features.downloads.repository import DownloadRepository
    from yijianjifa.features.downloads.contracts import OutputResource
    repo=DownloadRepository(db_engine);item=repo.summary(tid)['items'][0]
    artifact=tmp_path/'保留视频.mp4';artifact.write_bytes(b'retained fixture')
    resource=OutputResource('retained-output',item['candidate_id'],item['id'],'video','a'*64,artifact.stat().st_size)
    with db_engine.begin() as c:repo.save_output(c,resource,backend_path=str(artifact))
    def forbidden(*_):raise AssertionError('download touched publish')
    monkeypatch.setattr('yijianjifa.app_services.publish_control.cancel_local',forbidden)
    with pytest.raises(ValueError):
        with db_engine.begin() as c:remove_task(c,tid)
    with session_scope(db_engine) as s:s.get(TaskCenterRow,tid).state='partial'
    with db_engine.begin() as c:assert remove_task(c,tid)['deleted']
    with db_engine.connect() as c:assert c.execute(select(Setting).where(Setting.key=='task-hidden:'+tid)).first()
    assert repo.get_output(resource.id)==resource
    assert artifact.read_bytes()==b'retained fixture'
    assert repo.summary(tid)['total']==1

def test_parallel_native_receipt_only_opens_picker_once(setup,tmp_path):
    import threading
    _,r,api=setup;entered=threading.Event();release=threading.Event();calls=[];results=[]
    def picker(*_):calls.append(1);entered.set();assert release.wait(2);return [str(tmp_path/'model.bin')]
    request=raw('download.models.import',{'model_id':'whisper-small-multilingual'})
    def run():results.append(api.dispatch_native(request,picker,trusted_origin=True))
    first=threading.Thread(target=run);second=threading.Thread(target=run)
    first.start();assert entered.wait(1);second.start();release.set();first.join(2);second.join(2)
    assert len(results)==2 and results[0]==results[1];assert len(calls)==1;assert len(r.calls)==1


def test_committed_job_receipt_survives_scheduler_wake_exception(setup):
    _,r,api=setup
    def broken():raise RuntimeError('private wake error')
    r.wake=broken
    request=raw('download.start',dict(candidate_ids=['a'],options=OPTIONS,directory_grant_id='g'))
    first=api.dispatch(request,trusted_origin=True)
    assert first['ok'] and first['data']['task_id']=='task1'
    assert api.dispatch(request,trusted_origin=True)==first and len(r.calls)==1


def test_native_host_validates_before_picker_and_replays_selection(setup,tmp_path):
    import threading
    from types import SimpleNamespace
    from yijianjifa.webui.host_commands import make_host
    _,r,commands=setup;calls=[]
    class Api:
        _error=staticmethod(DownloadCommands.error)
        dispatch_download_native=commands.dispatch_native
    class Window:
        def create_file_dialog(self,*a,**kw):calls.append((a,kw));return [str(tmp_path/'selected')]
    host=make_host(api=Api(),engine=None,window=Window(),webview=SimpleNamespace(FileDialog=SimpleNamespace(FOLDER='folder',OPEN='open',SAVE='save')),
        exit_started=threading.Event(),finished_files=None,finished_deletion=None,deletion_recovery_pending=False,cover_response=None,drops=None,drop_access=None,
        media=None,excel=None,output=None,voice_catalog=None,volc_config=None,voices=None,play_sound=None,copy_text=None,request_exit=None)
    bad=raw('download.subtitle.export',dict(resource_id='r',origin='asr',format='../../bad'))
    assert not host.dispatch(bad,trusted_origin=True)['ok'] and not calls
    request=raw('download.output.choose',{},'valid')
    first=host.dispatch(request,trusted_origin=True)
    assert first['ok'] and host.dispatch(request,trusted_origin=True)==first and len(calls)==1
    assert calls[0][0]==('folder',)

@pytest.mark.parametrize('state',['paused','paused_auth','completed_with_errors','interrupted'])
def test_download_waiting_and_partial_records_offer_continue(db_engine,state):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.features.tasks.service import TaskCenterRow
    from yijianjifa.storage.db import session_scope
    tid=download_batch(db_engine)
    with session_scope(db_engine) as session:session.get(TaskCenterRow,tid).state=state
    assert 'resume' in TaskCenterAccess(db_engine).detail(tid)['controls']


def test_long_subtitle_patch_budget_matches_compact_frontend_json(setup):
    _,r,api=setup
    params=dict(resource_id='original',origin='asr',expected_revision=0,
        patches=[dict(id=f's{i:03}',text='a'*34) for i in range(200)])
    compact=json.dumps(params,ensure_ascii=False,separators=(',',':')).encode()
    assert len(compact)<=12288<len(json.dumps(params,ensure_ascii=False).encode())
    request=raw('download.subtitle.save',params)
    assert len(request.encode())<=16384
    result=api.dispatch(request,trusted_origin=True)
    assert result['ok'],result
    assert len(r.calls)==1 and r.calls[0][0]=='download.subtitle.save'
