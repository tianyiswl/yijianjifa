from legacy_schedule_fixture import legacy_window
from datetime import timedelta
import json
from sqlalchemy import select,func
from test_commerce_preparation import configured
from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
from yijianjifa.app_services.publish_control import recover_local_software,cancel_local
from yijianjifa.features.commerce.service import CommerceTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
from yijianjifa.features.notifications.service import NotificationRow
from yijianjifa.storage.db import session_scope


def held(engine,paths,tmp):
    prep,music,a,v,p,now=configured(engine,paths,tmp)
    p['plan']['start']=(now+timedelta(minutes=30)).isoformat()
    p['plan']['time_choices']={a.id+':'+v.id:{'mode':'software_schedule','software_consent':True}}
    with legacy_window():  # Existing short-window records remain recoverable.
        task=CommerceConfirmation(engine,paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    return task['id'],now


def test_commerce_missed_schedule_preserves_snapshot_and_notifies_once(db_engine,app_paths,tmp_path):
    tid,now=held(db_engine,app_paths,tmp_path)
    with session_scope(db_engine) as s:original=s.get(CommerceTaskRow,tid).snapshot_json
    assert recover_local_software(db_engine,now=now)==[]
    assert recover_local_software(db_engine,now=now+timedelta(minutes=31))==[tid]
    assert recover_local_software(db_engine,now=now+timedelta(minutes=32))==[]
    with session_scope(db_engine) as s:
        task=s.get(CommerceTaskRow,tid)
        assert task.snapshot_json==original and task.state=='missed_waiting_user'
        assert s.get(TaskCenterRow,tid).state=='missed_waiting_user'
        assert s.scalar(select(TaskItemRow.state))=='missed_waiting_user'
        assert s.scalar(select(PublishHistoryRow.state))=='queued'
        assert s.scalar(select(func.count()).select_from(NotificationRow))==1
        assert json.loads(task.snapshot_json)['execution_enabled'] is False
    with db_engine.begin() as c:assert cancel_local(c,tid)['state']=='cancelled'


def test_commerce_missed_check_never_relabels_unknown_submission(db_engine,app_paths,tmp_path):
    tid,now=held(db_engine,app_paths,tmp_path)
    with session_scope(db_engine) as s:s.scalar(select(PublishHistoryRow)).state='result_unknown'
    assert recover_local_software(db_engine,now=now+timedelta(minutes=31))==[]
    with session_scope(db_engine) as s:
        assert s.get(CommerceTaskRow,tid).state=='paused'
        assert s.scalar(select(PublishHistoryRow.state))=='result_unknown'
        assert s.scalar(select(func.count()).select_from(NotificationRow))==0


def test_commerce_missed_reschedule_preserves_commerce_fields_and_audits_revision(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.publish_reschedule import reschedule_local
    from yijianjifa.storage.models import Setting
    tid,now=held(db_engine,app_paths,tmp_path)
    later=now+timedelta(minutes=31);recover_local_software(db_engine,now=later)
    with session_scope(db_engine) as s:
        before=s.get(CommerceTaskRow,tid).snapshot_json
        row=s.scalar(select(TaskItemRow));key=row.item_key;fields=json.loads(row.detail_json)
    p=dict(id=tid,item_key=key,mode='platform_schedule',when=(later+timedelta(hours=3)).isoformat(),software_consent=False)
    with db_engine.begin() as c:r=reschedule_local(c,p,now=later)
    assert r['state']=='paused' and r['revision']==1 and not r['submitted']
    with session_scope(db_engine) as s:
        snapshot=json.loads(s.get(CommerceTaskRow,tid).snapshot_json)
        assert s.get(Setting,'publish-revision:'+tid+':0').value_json==before
        for field in ['store','declaration','platform_music']:assert snapshot['items'][0][field]==fields[field]
        assert snapshot['execution_enabled'] is False
        assert s.scalar(select(PublishHistoryRow.state))=='queued'


def test_commerce_reschedule_requires_commerce_capability(db_engine,app_paths,tmp_path):
    import pytest
    from yijianjifa.app_services.publish_reschedule import reschedule_local
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    tid,now=held(db_engine,app_paths,tmp_path);later=now+timedelta(minutes=31)
    recover_local_software(db_engine,now=later)
    with session_scope(db_engine) as s:
        row=s.scalar(select(TaskItemRow));key=row.item_key;aid=json.loads(row.detail_json)['account_id']
    AccountService(db_engine).update_capabilities(aid,CapabilitySnapshot(ordinary='available',commerce='not_found'))
    p=dict(id=tid,item_key=key,mode='publish_now',when=None,software_consent=False)
    with db_engine.begin() as c:
        with pytest.raises(ValueError,match='account needs verification'):reschedule_local(c,p,now=later)
