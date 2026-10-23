from legacy_schedule_fixture import legacy_window
import json
from datetime import timedelta
from sqlalchemy import select
from test_publish_preparation import setup
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.app_services.publish_confirmation import PublishConfirmation
from yijianjifa.app_services.publish_control import recover_local_software
from yijianjifa.app_services.desktop_api import DesktopApi


def missed(engine,paths,tmp):
    prep,a,v,p,now=setup(engine,paths,tmp);past=now-timedelta(hours=1)
    p['start']=(past+timedelta(minutes=10)).isoformat()
    p['time_choices']={a.id+':'+v.id:{'mode':'software_schedule','software_consent':True}}
    with legacy_window():  # Existing short-window records remain recoverable.
        task=PublishConfirmation(engine,paths,prep).confirm(prep.prepare(p,now=past)['token'],now=past)
    recover_local_software(engine,now=now)
    return task,a,v,now


def call(api,params,rid='reschedule'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation='publish.reschedule',params=params)),trusted_origin=True)


def test_reschedule_preserves_old_snapshot_history_and_idempotent_receipt(db_engine,app_paths,tmp_path):
    task,a,v,now=missed(db_engine,app_paths,tmp_path);api=DesktopApi(db_engine,app_paths)
    with session_scope(db_engine) as s:original=s.get(PublishTaskRow,task['id']).snapshot_json
    p=dict(id=task['id'],item_key=a.id+':'+v.id,mode='platform_schedule',when=(now+timedelta(hours=4)).isoformat(),software_consent=False)
    r=call(api,p);assert r['ok'],r
    assert r['data']['state']=='paused' and not r['data']['submitted']
    assert call(api,p)==r
    with session_scope(db_engine) as s:
        new=json.loads(s.get(PublishTaskRow,task['id']).snapshot_json)
        assert new['items'][0]['execution_mode']=='platform_schedule'
        assert new['items'][0]['time']==p['when']
        assert s.get(Setting,'publish-revision:'+task['id']+':0').value_json==original
        assert list(s.scalars(select(PublishHistoryRow.state)))==['queued']
        assert new['execution_enabled'] is False
    api.close(.1)


def test_reschedule_blocks_invalid_time_consent_identity_and_unresolved(db_engine,app_paths,tmp_path):
    task,a,v,now=missed(db_engine,app_paths,tmp_path);api=DesktopApi(db_engine,app_paths)
    base=dict(id=task['id'],item_key=a.id+':'+v.id,mode='software_schedule',when=(now+timedelta(minutes=30)).isoformat(),software_consent=False)
    for i,p in enumerate((base,dict(base,software_consent=True,when=(now+timedelta(days=15)).isoformat()),dict(base,mode='platform_schedule'),dict(base,item_key='other:video'),dict(base,software_consent=1))):
        assert not call(api,p,'bad'+str(i))['ok']
    with session_scope(db_engine) as s:s.scalar(select(PublishHistoryRow)).state='result_unknown'
    assert not call(api,dict(base,mode='publish_now',when=None),'unknown')['ok']
    with session_scope(db_engine) as s:
        assert s.get(PublishTaskRow,task['id']).state=='missed_waiting_user'
        assert s.scalar(select(PublishHistoryRow.state))=='result_unknown'
    api.close(.1)


def test_missed_item_explicit_immediate_remains_paused(db_engine,app_paths,tmp_path):
    task,a,v,now=missed(db_engine,app_paths,tmp_path);api=DesktopApi(db_engine,app_paths)
    p=dict(id=task['id'],item_key=a.id+':'+v.id,mode='publish_now',when=None,software_consent=False)
    assert call(api,p)['ok']
    with session_scope(db_engine) as s:
        row=s.get(PublishTaskRow,task['id']);snap=json.loads(row.snapshot_json)
        assert row.state=='paused' and snap['execution_enabled'] is False
        assert snap['items'][0]['execution_mode']=='publish_now'
        assert snap['items'][0]['software_consent'] is False
    api.close(.1)


def test_reschedule_rechecks_identity_and_frozen_resource(db_engine,app_paths,tmp_path):
    from pathlib import Path
    from yijianjifa.features.accounts.service import AccountRow
    task,a,v,now=missed(db_engine,app_paths,tmp_path);api=DesktopApi(db_engine,app_paths)
    p=dict(id=task['id'],item_key=a.id+':'+v.id,mode='platform_schedule',when=(now+timedelta(hours=4)).isoformat(),software_consent=False)
    with session_scope(db_engine) as s:
        account=s.get(AccountRow,a.id);original=account.platform_user_id;account.platform_user_id='different'
    assert not call(api,p,'identity')['ok']
    with session_scope(db_engine) as s:
        s.get(AccountRow,a.id).platform_user_id=original
        snap=json.loads(s.get(PublishTaskRow,task['id']).snapshot_json)
    Path(snap['videos'][0]['path']).write_bytes(b'corrupt-test-copy')
    assert not call(api,p,'resource')['ok']
    with session_scope(db_engine) as s:assert s.get(Setting,'publish-revision:'+task['id']+':0') is None
    api.close(.1)


def test_cancel_one_missed_account_keeps_other_account_reschedulable(db_engine,app_paths,tmp_path):
    from yijianjifa.features.accounts.service import AccountService,AccountRow
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    accounts=AccountService(db_engine);b=accounts.create('douyin','other-missed-fixture','另一个账号')
    with session_scope(db_engine) as s:s.get(AccountRow,b.id).session_ref='offline-fixture'
    accounts.update_capabilities(b.id,CapabilitySnapshot(ordinary='available'))
    past=now-timedelta(hours=1)
    p.update(account_ids=[a.id,b.id],distribution='whole_batch',start=(past+timedelta(minutes=10)).isoformat(),
             time_choices={aid+':'+v.id:{'mode':'software_schedule','software_consent':True} for aid in [a.id,b.id]})
    with legacy_window():  # Existing short-window records remain recoverable.
        task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=past)['token'],now=past)
    recover_local_software(db_engine,now=now)
    api=DesktopApi(db_engine,app_paths)
    try:
        result=api.dispatch(json.dumps(dict(v=1,request_id='cancel-missed-account',operation='publish.cancel',params={'id':task['id'],'account_id':a.id})),trusted_origin=True)
        assert result['ok'],result
        assert result['data']['state']=='missed_waiting_user'
        access=TaskCenterAccess(db_engine)
        assert task['id'] in [t['id'] for t in access.list('','action','',0,20)['items']]
        revised=call(api,dict(id=task['id'],item_key=b.id+':'+v.id,mode='platform_schedule',when=(now+timedelta(hours=4)).isoformat(),software_consent=False))
        assert revised['ok'],revised
        assert revised['data']['state']=='paused'
        with session_scope(db_engine) as s:
            assert s.get(TaskCenterRow,task['id']).state==s.get(PublishTaskRow,task['id']).state=='paused'
            states={r.item_key:r.state for r in s.scalars(select(TaskItemRow))}
            assert states=={a.id+':'+v.id:'cancelled',b.id+':'+v.id:'paused'}
            assert {h.account_id:h.state for h in s.scalars(select(PublishHistoryRow))}=={a.id:'cancelled',b.id:'queued'}
    finally:api.close(.1)
