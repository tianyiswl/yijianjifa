from legacy_schedule_fixture import legacy_window
import json
from pathlib import Path
import pytest
from sqlalchemy import select,func
from yijianjifa.app_services.publish_confirmation import PublishConfirmation
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
from yijianjifa.storage.db import session_scope
from test_publish_preparation import setup,verify_optional_declarations


def test_confirm_freezes_files_and_atomically_registers_paused_local_task(db_engine,app_paths,tmp_path):
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path);token=prep.prepare(p,now=now)['token']
    service=PublishConfirmation(db_engine,app_paths,prep)
    result=service.confirm(token,now=now)
    assert result['state']=='paused' and not result['submitted']
    with session_scope(db_engine) as s:
        task=s.get(PublishTaskRow,result['id']);center=s.get(TaskCenterRow,result['id'])
        frozen=json.loads(task.snapshot_json);copy=Path(frozen['videos'][0]['path'])
        assert task.state==center.state=='paused' and copy!=v.effective_path
        assert copy.read_bytes()==v.effective_path.read_bytes()
        assert s.scalar(select(func.count()).select_from(TaskItemRow))==1
        assert s.scalar(select(PublishHistoryRow.state))=='queued'
    v.effective_path.write_bytes(b'changed-after-confirm')
    assert copy.read_bytes()==b'local-test-fixture'
    prep.close()
    assert PublishConfirmation(db_engine,app_paths,prep).confirm(token,now=now)==result


def test_confirm_transaction_failure_leaves_no_partial_records(db_engine,app_paths,tmp_path,monkeypatch):
    from yijianjifa.features.publish.history import PublishHistoryRepository
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path);token=prep.prepare(p,now=now)['token']
    def fail(*args,**kwargs):raise RuntimeError('injected history failure')
    monkeypatch.setattr(PublishHistoryRepository,'record',fail)
    with pytest.raises(RuntimeError,match='injected'):PublishConfirmation(db_engine,app_paths,prep).confirm(token,now=now)
    with session_scope(db_engine) as s:
        for model in (PublishTaskRow,TaskCenterRow,TaskItemRow,PublishHistoryRow):assert s.scalar(select(func.count()).select_from(model))==0
    assert v.effective_path.exists()

def test_confirm_api_replays_after_reopen_without_platform_execution(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.desktop_api import DesktopApi
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path);api=DesktopApi(db_engine,app_paths)
    def call(api,op,params):return api.dispatch(json.dumps(dict(v=1,request_id='local',operation=op,params=params)),trusted_origin=True)
    verify_optional_declarations(api,p)
    prepared=call(api,'publish.prepare',p);token=prepared['data']['token']
    result=call(api,'publish.confirm',{'token':token});assert result['ok'],result
    api.close(.1);api=DesktopApi(db_engine,app_paths)
    assert call(api,'publish.confirm',{'token':token})==result
    assert not call(api,'publish.confirm',{'token':token,'execute':True})['ok']
    api.close(.1)

def test_task_detail_shows_frozen_publish_text_without_paths_or_session_fingerprints(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.features.finished.service import FinishedVideoService
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    FinishedVideoService(db_engine).update_metadata(v.id,title='changed later',body='',tags='')
    detail=TaskCenterAccess(db_engine).detail(task['id'])
    assert detail['settings']['platform']=='douyin'
    assert detail['settings']['hold_reason']=='platform_execution_not_authorized'
    assert detail['items'][0]['title']=='原始标题'
    assert detail['items'][0]['account_id']==a.id and detail['items'][0]['time']==p['start']
    raw=json.dumps(detail)
    assert str(tmp_path) not in raw and 'session_fingerprint' not in raw and 'fixture-only-session' not in raw

def test_cancel_local_task_releases_queued_history_preserves_files_and_replays(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.features.publish.normal_service import NormalPublishService
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    api=DesktopApi(db_engine,app_paths)
    raw=json.dumps(dict(v=1,request_id='cancel-local',operation='publish.cancel',params={'id':task['id']}))
    r=api.dispatch(raw,trusted_origin=True);assert r['ok'],r
    assert r['data']['state']=='cancelled' and r['data']['cancelled']==1
    assert api.dispatch(raw,trusted_origin=True)==r
    assert v.id in NormalPublishService(db_engine).quick_candidate_ids()
    with session_scope(db_engine) as s:
        snapshot=json.loads(s.get(PublishTaskRow,task['id']).snapshot_json)
        assert Path(snapshot['videos'][0]['path']).is_file()
        assert s.scalar(select(PublishHistoryRow.state))=='cancelled'
        assert s.scalar(select(TaskItemRow.state))=='cancelled'
    api.close(.1)


def test_cancel_does_not_relabel_unknown_platform_outcome(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.desktop_api import DesktopApi
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    with session_scope(db_engine) as s:
        s.scalar(select(PublishHistoryRow)).state='result_unknown'
        s.scalar(select(TaskItemRow)).state='result_unknown'
    api=DesktopApi(db_engine,app_paths)
    r=api.dispatch(json.dumps(dict(v=1,request_id='cancel-protected',operation='publish.cancel',params={'id':task['id']})),trusted_origin=True)
    assert r['ok'] and r['data']['cancelled']==0 and r['data']['state']=='result_unknown'
    with session_scope(db_engine) as s:assert s.scalar(select(PublishHistoryRow.state))=='result_unknown'
    api.close(.1)


def test_repeat_success_requires_pair_confirmation_and_freezes_choice(db_engine,app_paths,tmp_path):
    from yijianjifa.features.publish.history import PublishHistoryRepository
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    history=PublishHistoryRepository(db_engine);history.record(v.content_hash,a.id,'published')
    blocked=prep.prepare(p,now=now)
    assert blocked['token'] is None
    assert blocked['items'][0]['repeat_confirmation_required'] is True
    key=a.id+':'+v.id
    p['repeat_confirmations']=[key]
    ready=prep.prepare(p,now=now);assert ready['token']
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(ready['token'],now=now)
    with session_scope(db_engine) as s:
        frozen=json.loads(s.get(PublishTaskRow,task['id']).snapshot_json)
        assert frozen['params']['repeat_confirmations']==[key]
        assert frozen['items'][0]['repeat_confirmed'] is True
        assert list(s.scalars(select(PublishHistoryRow.attempt).order_by(PublishHistoryRow.attempt)))==[1,2]
    assert prep.prepare(p,now=now)['token'] is None  # consent cannot bypass the queued attempt


def test_repeat_consent_is_strictly_scoped_and_never_overrides_unknown(db_engine,app_paths,tmp_path):
    from yijianjifa.features.publish.history import PublishHistoryRepository
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    key=a.id+':'+v.id
    for bad in (True,'all',['other:video'],[key,key],[True]):
        with pytest.raises(ValueError):prep.prepare(dict(p,repeat_confirmations=bad),now=now)
    PublishHistoryRepository(db_engine).record(v.content_hash,a.id,'result_unknown')
    result=prep.prepare(dict(p,repeat_confirmations=[key]),now=now)
    assert result['token'] is None and 'duplicate_blocked' in result['items'][0]['blockers']
    assert not result['items'][0]['repeat_confirmation_required']


def test_short_window_is_blocked_even_with_software_consent(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    p['start']=(now+timedelta(minutes=30)).isoformat();key=a.id+':'+v.id
    assert prep.prepare(p,now=now)['token'] is None
    p['time_choices']={key:{'mode':'software_schedule','software_consent':False}}
    assert prep.prepare(p,now=now)['token'] is None
    p['time_choices'][key]['software_consent']=True
    ready=prep.prepare(p,now=now)
    assert ready['token'] is None
    assert 'before_schedule_window' in ready['items'][0]['blockers']


def test_time_choices_cannot_bypass_three_hour_or_thirteen_day_limit(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path);key=a.id+':'+v.id
    for mode in ('publish_now','software_schedule'):
        p['time_choices']={key:{'mode':mode,'software_consent':mode=='software_schedule'}}
        for when,code in ((now-timedelta(minutes=1),'scheduled_time_in_past'),(now+timedelta(days=15),'beyond_platform_window')):
            p['start']=when.isoformat();r=prep.prepare(p,now=now)
            assert r['token'] is None and code in r['items'][0]['blockers']
    p['start']=(now+timedelta(minutes=5)).isoformat()
    p['time_choices']={key:{'mode':'publish_now','software_consent':False}}
    r=prep.prepare(p,now=now);assert r['token'] is None
    assert 'before_schedule_window' in r['items'][0]['blockers']


def test_time_choices_reject_unknown_scope_and_loose_consent(db_engine,app_paths,tmp_path):
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path);key=a.id+':'+v.id
    for bad in (True,{'other:video':{'mode':'publish_now','software_consent':False}}, {key:{'mode':'unknown','software_consent':True}}, {key:{'mode':'software_schedule','software_consent':1}}, {key:{'mode':'publish_now'}}, {key:{'mode':'publish_now','software_consent':False,'execute':True}}):
        with pytest.raises(ValueError):prep.prepare(dict(p,time_choices=bad),now=now)


@pytest.mark.parametrize('mode',['platform_schedule','software_schedule','publish_now'])
@legacy_window()
def test_task_details_distinguish_frozen_execution_modes(db_engine,app_paths,tmp_path,mode):
    from datetime import timedelta
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    if mode!='platform_schedule':
        p['start']=(now+timedelta(minutes=30)).isoformat()
        p['time_choices']={a.id+':'+v.id:{'mode':mode,'software_consent':mode=='software_schedule'}}
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    detail=TaskCenterAccess(db_engine).detail(task['id'])
    assert detail['items'][0]['execution_mode']==mode
    assert detail['items'][0]['software_consent']==(mode=='software_schedule')
    assert detail['items'][0]['repeat_confirmed'] is False
    assert 'path' not in json.dumps(detail['items'])


@legacy_window()
def test_local_software_restart_marks_missed_without_releasing_history(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    from yijianjifa.app_services.publish_control import recover_local_software, cancel_local
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    p['start']=(now+timedelta(minutes=5)).isoformat()
    p['time_choices']={a.id+':'+v.id:{'mode':'software_schedule','software_consent':True}}
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    with session_scope(db_engine) as s:before=s.get(PublishTaskRow,task['id']).snapshot_json
    assert recover_local_software(db_engine,now=now)==[]
    assert recover_local_software(db_engine,now=now+timedelta(minutes=6))==[task['id']]
    assert recover_local_software(db_engine,now=now+timedelta(minutes=7))==[]
    from yijianjifa.features.notifications.service import NotificationService
    notices=NotificationService(db_engine).history_for(task['id'])
    assert len(notices)==1 and notices[0].status=='action_required'
    assert notices[0].deep_link=='task:'+task['id']
    detail=TaskCenterAccess(db_engine).detail(task['id'])
    assert detail['task']['state']==detail['items'][0]['state']=='missed_waiting_user'
    assert 'cancel' in detail['controls']
    assert TaskCenterAccess(db_engine).list('', 'action','',0,20)['total']==1
    with session_scope(db_engine) as s:
        assert s.get(PublishTaskRow,task['id']).snapshot_json==before
        assert s.scalar(select(PublishHistoryRow.state))=='queued'
    with db_engine.begin() as connection:assert cancel_local(connection,task['id'])['state']=='cancelled'


@legacy_window()
def test_local_recovery_never_changes_unknown_or_platform_scheduled_history(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    from yijianjifa.app_services.publish_control import recover_local_software
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    p['start']=(now+timedelta(minutes=5)).isoformat()
    p['time_choices']={a.id+':'+v.id:{'mode':'software_schedule','software_consent':True}}
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    for state in ('result_unknown','platform_scheduled','published'):
        with session_scope(db_engine) as s:s.scalar(select(PublishHistoryRow)).state=state
        assert recover_local_software(db_engine,now=now+timedelta(minutes=6))==[]
        with session_scope(db_engine) as s:
            assert s.scalar(select(TaskItemRow.state))=='paused'
            assert s.scalar(select(PublishHistoryRow.state))==state


@legacy_window()
def test_desktop_startup_recovers_expired_local_software_task(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    from yijianjifa.app_services.desktop_api import DesktopApi
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    earlier=now-timedelta(hours=1)
    p['start']=(earlier+timedelta(minutes=5)).isoformat()
    p['time_choices']={a.id+':'+v.id:{'mode':'software_schedule','software_consent':True}}
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=earlier)['token'],now=earlier)
    api=DesktopApi(db_engine,app_paths)
    with session_scope(db_engine) as s:assert s.get(PublishTaskRow,task['id']).state=='missed_waiting_user'
    api.close(.1)


@pytest.mark.parametrize('mode',['exact','immediate'])
def test_normal_explicit_schedule_modes_freeze_after_confirmation_wait(db_engine,app_paths,tmp_path,mode):
    from datetime import timedelta
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    p['mode']=mode
    if mode=='immediate':p['start']=None
    token=prep.prepare(p,now=now)['token'];assert token
    result=PublishConfirmation(db_engine,app_paths,prep).confirm(token,now=now+timedelta(seconds=10))
    with session_scope(db_engine) as s:
        task=s.get(PublishTaskRow,result['id']);data=json.loads(task.snapshot_json)
        assert task.state=='paused' and data['execution_enabled'] is False
        assert data['items'][0]['time']==p['start']
        assert data['items'][0]['execution_mode']==('publish_now' if mode=='immediate' else 'platform_schedule')
        assert data['params']['mode']==mode
        assert s.scalar(select(PublishHistoryRow.state))=='queued'


def test_normal_whole_batch_item_time_override_is_scoped_to_one_account(db_engine,app_paths,tmp_path):
    from yijianjifa.features.accounts.service import AccountService,AccountRow
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    accounts=AccountService(db_engine);other=accounts.create('douyin','second-time-scope','第二账号')
    with session_scope(db_engine) as s:s.get(AccountRow,other.id).session_ref='fixture-second-only'
    accounts.update_capabilities(other.id,CapabilitySnapshot(ordinary='available'))
    p.update(account_ids=[a.id,other.id],distribution='whole_batch',item_schedules={a.id+':'+v.id:{'mode':'immediate','start':None}})
    token=prep.prepare(p,now=now)['token'];assert token
    result=PublishConfirmation(db_engine,app_paths,prep).confirm(token,now=now)
    with session_scope(db_engine) as s:
        task=s.get(PublishTaskRow,result['id']);data=json.loads(task.snapshot_json)
        items={x['account_id']:x for x in data['items']}
        assert items[a.id]['time'] is None and items[a.id]['execution_mode']=='publish_now'
        assert items[other.id]['time']==p['start'] and items[other.id]['execution_mode']=='platform_schedule'
        assert items[a.id]['time_overridden'] and not items[other.id]['time_overridden']
        assert task.state=='paused' and data['execution_enabled'] is False
        assert list(s.scalars(select(PublishHistoryRow.state)))==['queued','queued']


def test_multi_account_schedule_survives_api_restart_with_frozen_pair_order(db_engine,app_paths,tmp_path):
    """Common clocks, account clocks and one item override stay distinct after reopen."""
    from datetime import timedelta
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.features.accounts.service import AccountService,AccountRow
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.features.library.media_identity import sha256_file
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    accounts=AccountService(db_engine)
    other=accounts.create('douyin','restart-time-scope','第二账号')
    with session_scope(db_engine) as s:s.get(AccountRow,other.id).session_ref='fixture-restart-only'
    accounts.update_capabilities(other.id,CapabilitySnapshot(ordinary='available'))
    source=tmp_path/'second.mp4';source.write_bytes(b'second-local-fixture')
    second=FinishedVideoService(db_engine).register_generated(source,duration_ms=3000,title='第二视频')
    tomorrow=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    p.update(account_ids=[a.id,other.id],video_ids=[v.id,second.id],distribution='whole_batch',
        start=tomorrow.isoformat(),daily_count=2,daily_times=['18:45','09:15'],
        account_schedules={other.id:dict(mode='daily',start=tomorrow.isoformat(),interval_hours=3,
            daily_count=2,days=1,daily_times=['20:30','11:30'])},
        item_schedules={a.id+':'+v.id:dict(mode='immediate',start=None)})
    def call(api,op,params):
        response=api.dispatch(json.dumps(dict(v=1,request_id='restart-chain',operation=op,params=params)),trusted_origin=True)
        assert response['ok'],response
        return response['data']
    api=DesktopApi(db_engine,app_paths)
    try:
        verify_optional_declarations(api,p)
        token=call(api,'publish.prepare',p)['token'];assert token
        result=call(api,'publish.confirm',dict(token=token))
        before=call(api,'task.center.detail',dict(id=result['id']))
    finally:api.close(.1)
    # The detail must come from frozen data, not the now-edited finished-video metadata.
    FinishedVideoService(db_engine).update_metadata(v.id,title='保存后改名',body='',tags='')
    api=DesktopApi(db_engine,app_paths)
    try:
        assert call(api,'publish.confirm',dict(token=token))==result
        detail=call(api,'task.center.detail',dict(id=result['id']))
        assert detail==before
        items=detail['items']
        assert [(x['account_id'],x['video_id']) for x in items]==[(a.id,v.id),(a.id,second.id),(other.id,v.id),(other.id,second.id)]
        assert items[0]['execution_mode']=='publish_now' and items[0].get('time') is None
        assert [x['time'][11:16] for x in items[1:]]==['18:45','11:30','20:30']
        assert items[0]['title']==items[2]['title']=='原始标题'
        assert all(x['state']=='paused' and not x['software_consent'] for x in items)
        assert detail['settings']['execution_enabled'] is False
        assert detail['settings']['hold_reason']=='platform_execution_not_authorized'
        raw=json.dumps(detail)
        assert str(tmp_path) not in raw and 'session_fingerprint' not in raw and 'fixture-restart-only' not in raw
        with session_scope(db_engine) as s:
            assert s.scalar(select(func.count()).select_from(PublishTaskRow))==1
            assert s.scalar(select(func.count()).select_from(TaskItemRow))==4
            assert list(s.scalars(select(PublishHistoryRow.state)))==['queued']*4
            snapshot=json.loads(s.get(PublishTaskRow,result['id']).snapshot_json)
            assert len(snapshot['videos'])==2
            for video in snapshot['videos']:assert sha256_file(Path(video['path']))==video['content_hash']
    finally:api.close(.1)

@pytest.mark.parametrize('protected', [False, True])
def test_cancel_one_account_preserves_other_account_and_unknown_history(db_engine,app_paths,tmp_path,protected):
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.features.accounts.service import AccountService,AccountRow
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    accounts=AccountService(db_engine);other=accounts.create('douyin','second-cancel-fixture','第二账号')
    with session_scope(db_engine) as s:s.get(AccountRow,other.id).session_ref='synthetic-only'
    accounts.update_capabilities(other.id,CapabilitySnapshot(ordinary='available'))
    p.update(account_ids=[a.id,other.id],distribution='whole_batch')
    task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    with session_scope(db_engine) as s:
        frozen=s.get(PublishTaskRow,task['id']).snapshot_json
        if protected:s.scalar(select(PublishHistoryRow).where(PublishHistoryRow.account_id==a.id)).state='result_unknown'
    api=DesktopApi(db_engine,app_paths)
    def call(aid,rid):
        return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation='publish.cancel',params={'id':task['id'],'account_id':aid})),trusted_origin=True)
    try:
        assert not call('not-in-this-task','invalid-account')['ok']
        result=call(a.id,'cancel-account');assert result['ok'],result
        assert result['data']['cancelled']==(0 if protected else 1)
        assert result['data']['state']==('result_unknown' if protected else 'paused')
        assert call(a.id,'cancel-account')==result
        with session_scope(db_engine) as s:
            assert s.get(PublishTaskRow,task['id']).snapshot_json==frozen
            states={h.account_id:h.state for h in s.scalars(select(PublishHistoryRow))}
            assert states=={a.id:'result_unknown' if protected else 'cancelled',other.id:'queued'}
            for row in s.scalars(select(TaskItemRow)):
                aid=json.loads(row.detail_json)['account_id']
                assert row.state==('cancelled' if aid==a.id and not protected else 'paused')
            assert Path(json.loads(frozen)['videos'][0]['path']).is_file()
        detail=TaskCenterAccess(db_engine).detail(task['id'])
        assert detail['account_controls']==[{'account_id':other.id,'label':'第二账号','controls':['cancel']}]
    finally:api.close(.1)
