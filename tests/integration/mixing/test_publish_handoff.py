from datetime import datetime,timedelta,timezone
import json
from sqlalchemy import select,func
from test_desktop_service import service_for
from test_publish_after_plan import account
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.storage.db import session_scope


def setup(engine,paths,tmp,linkage):
    service,renderer,_,_=service_for(engine,paths,tmp);a=account(engine)
    start=datetime.now(timezone.utc)+timedelta(hours=4)
    task=service.submit(quantity=3,publish_after=dict(account_id=a.id,start=start.isoformat(),interval_hours=3,linkage=linkage))
    return service,renderer,a,task


def test_per_item_handoff_preserves_index_gaps_and_replays(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.mix_publish_handoff import MixPublishHandoff
    service,renderer,a,task=setup(db_engine,app_paths,tmp_path,'per_item')
    handoff=MixPublishHandoff(db_engine,app_paths)
    service.queue._run_single_item(task.id)
    first=handoff.reconcile(task.id);assert len(first['tasks'])==1
    original=renderer.render
    renderer.render=lambda request:(_ for _ in ()).throw(RuntimeError('fixture failed'))
    service.queue._run_single_item(task.id)
    renderer.render=original;service.queue._run_single_item(task.id)
    result=handoff.reconcile(task.id);assert len(result['tasks'])==2
    assert MixPublishHandoff(db_engine,app_paths).reconcile(task.id)==result
    with session_scope(db_engine) as s:
        rows=list(s.scalars(select(PublishTaskRow)));assert len(rows)==2
        snapshots=[json.loads(r.snapshot_json) for r in rows]
        assert sorted(d['source_mix']['indices'][0] for d in snapshots)==[0,2]
        for data in snapshots:
            index=data['source_mix']['indices'][0]
            assert data['items'][0]['time']==task.planned_publish_times[index]
            assert not data['execution_enabled'] and data['source_mix']['policy_recheck_required']
            assert data['items'][0]['title']=='原文'


def test_whole_batch_waits_for_terminal_results_then_creates_one_paused_task(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.mix_publish_handoff import MixPublishHandoff
    service,_,_,task=setup(db_engine,app_paths,tmp_path,'whole_batch')
    handoff=MixPublishHandoff(db_engine,app_paths)
    service.queue._run_single_item(task.id)
    assert handoff.reconcile(task.id)['tasks']==[]
    service.queue.run_once_until_idle()
    result=handoff.reconcile(task.id);assert len(result['tasks'])==1 and result['tasks'][0]['quantity']==3
    assert handoff.reconcile(task.id)==result
    with session_scope(db_engine) as s:assert s.scalar(select(func.count()).select_from(PublishTaskRow))==1


def test_account_session_change_blocks_handoff_without_creating_task(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.mix_publish_handoff import MixPublishHandoff
    from yijianjifa.features.accounts.service import AccountRow
    service,_,a,task=setup(db_engine,app_paths,tmp_path,'per_item')
    service.queue._run_single_item(task.id)
    with session_scope(db_engine) as s:s.get(AccountRow,a.id).session_ref='changed'
    result=MixPublishHandoff(db_engine,app_paths).reconcile(task.id)
    assert result['tasks']==[] and result['blocked']
    with session_scope(db_engine) as s:assert s.scalar(select(func.count()).select_from(PublishTaskRow))==0


def test_runtime_hook_hands_off_each_item_and_persists_blocked_status(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.mix_publish_handoff import MixPublishHandoff
    from yijianjifa.app_services.task_runtime import TaskRuntime
    from yijianjifa.features.accounts.service import AccountRow
    from yijianjifa.storage.models import Setting
    service,renderer,a,task=setup(db_engine,app_paths,tmp_path,'per_item')
    runtime=TaskRuntime(service,handoff=MixPublishHandoff(db_engine,app_paths))
    # Restart recovery holds work; this test explicitly resumes the isolated task.
    service.queue.resume(task.id)
    original=renderer.render
    observed=[]
    def render(request):
        with session_scope(db_engine) as s:observed.append(s.scalar(select(func.count()).select_from(PublishTaskRow)))
        if len(observed)==2:
            with session_scope(db_engine) as s:s.get(AccountRow,a.id).session_ref='changed-mid-run'
        return original(request)
    renderer.render=render
    service.queue.run_once_until_idle()
    assert observed==[0,1,1]
    with session_scope(db_engine) as s:
        status=json.loads(s.get(Setting,'mix-publish-status:'+task.id).value_json)
        assert len(status['tasks'])==1 and len(status['blocked'])==2
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    detail=TaskCenterAccess(db_engine).detail(task.id)
    assert len(detail['publish_handoff']['blocked'])==2
    with session_scope(db_engine) as s:s.get(AccountRow,a.id).session_ref='isolated-session'
    retried=runtime.handoff(task.id)
    assert len(retried['tasks'])==3 and retried['blocked']==[]
    assert runtime.handoff(task.id)==retried
    assert runtime.close(.1)


def test_restart_can_explicitly_recover_handoff_without_status_row(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    service,_,_,task=setup(db_engine,app_paths,tmp_path,'whole_batch')
    service.queue.run_once_until_idle()
    detail=TaskCenterAccess(db_engine).detail(task.id)
    assert detail['publish_handoff']['blocked']
    api=DesktopApi(db_engine,app_paths)
    raw=json.dumps(dict(v=1,request_id='explicit-recovery',operation='mix.publish.retry',params={'id':task.id}))
    try:
        assert not api.dispatch(raw)['ok']
        result=api.dispatch(raw,trusted_origin=True)
        assert result['ok'] and len(result['data']['tasks'])==1
        assert api.dispatch(raw,trusted_origin=True)==result
    finally:api.close(.1)


def test_real_ffmpeg_api_mix_to_paused_publish_resource_copy(db_engine,app_paths,tmp_path):
    import hashlib
    import subprocess
    from pathlib import Path
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.features.library.media_storage import MediaStorageService
    from yijianjifa.features.library.copy_service import CopyRepository,CopyDraft
    from yijianjifa.features.finished.service import FinishedVideo
    from yijianjifa.os_integration.resources import ffmpeg_path
    a=account(db_engine)
    source=tmp_path/'chain-source.mp4'
    subprocess.run([str(ffmpeg_path()),'-v','error','-f','lavfi','-i','testsrc2=s=160x90:r=10:d=2','-pix_fmt','yuv420p',str(source)],check=True)
    MediaStorageService(db_engine,app_paths,kind='video').import_media(source,keep_copy=False,category_ids=[])
    CopyRepository(db_engine).create(id='real-chain-copy',title='真实本地链路',voice='',body='隔离测试文案',tags='#测试',full='')
    api=DesktopApi(db_engine,app_paths)
    try:
        start=datetime.now(timezone.utc)+timedelta(hours=4)
        params=dict(quantity=1,total_ms=1000,segment_ms=1000,resolution='720p',spoken_enabled=False,full_enabled=False,keep_original_audio=False,publish_after=dict(account_id=a.id,start=start.isoformat(),interval_hours=3,linkage='whole_batch'))
        request=json.dumps(dict(v=1,request_id='real-chain-submit',operation='mix.submit',params=params))
        created=api.dispatch(request,trusted_origin=True);assert created['ok'],created
        api._mix.queue.run_once_until_idle()
        assert api._mix.queue.get(created['data']['id']).success_count==1
        with session_scope(db_engine) as s:
            row=s.scalar(select(PublishTaskRow));assert row is not None and row.state=='paused'
            data=json.loads(row.snapshot_json);finished=s.scalar(select(FinishedVideo))
            copied=Path(data['videos'][0]['path'])
            assert copied.exists() and copied!=Path(finished.stored_path)
            assert hashlib.sha256(copied.read_bytes()).hexdigest()==finished.content_hash
            assert data['items'][0]['time']==start.isoformat() and data['items'][0]['title']=='真实本地链路'
            assert data['source_mix']['task_id']==created['data']['id'] and not data['execution_enabled']
        assert api.dispatch(request,trusted_origin=True)==created
        with session_scope(db_engine) as s:assert s.scalar(select(func.count()).select_from(PublishTaskRow))==1
    finally:api.close(.1)
