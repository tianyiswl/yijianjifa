from datetime import datetime,timedelta,timezone
import json
import pytest
from test_desktop_service import service_for
from yijianjifa.features.accounts.service import AccountService,AccountRow
from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.storage.db import session_scope


def account(engine):
    service=AccountService(engine);a=service.create('douyin','chain-fixture','衔接账号')
    with session_scope(engine) as s:s.get(AccountRow,a.id).session_ref='isolated-session'
    service.update_capabilities(a.id,CapabilitySnapshot(ordinary='available'))
    return a


def test_mix_publish_plan_freezes_slots_and_preserves_gaps_after_render_failure(db_engine,app_paths,tmp_path):
    service,renderer,_,_=service_for(db_engine,app_paths,tmp_path);a=account(db_engine)
    start=datetime.now(timezone.utc)+timedelta(hours=4)
    settings=dict(account_id=a.id,start=start.isoformat(),interval_hours=3,linkage='per_item')
    task=service.submit(quantity=3,publish_after=settings)
    plan=task.snapshot['publish_after'];assert plan['account']['id']==a.id
    assert plan['linkage']=='per_item' and not plan['execution_enabled']
    assert [x['index'] for x in plan['slots']]==[0,1,2]
    assert [x['time'] for x in plan['slots']]==[(start+timedelta(hours=i*3)).isoformat() for i in range(3)]
    assert 'isolated-session' not in json.dumps(task.snapshot)
    settings['start']=(start+timedelta(days=2)).isoformat()
    original=renderer.render
    calls=[]
    def render(request):
        calls.append(1)
        if len(calls)==2:raise RuntimeError('isolated failure')
        return original(request)
    renderer.render=render
    service.queue.run_once_until_idle()
    current=service.queue.get(task.id)
    assert current.success_count==2 and current.failure_count==1
    assert current.snapshot['publish_after']==plan
    assert current.planned_publish_times==[x['time'] for x in plan['slots']]
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    detail=TaskCenterAccess(db_engine).detail(task.id)
    assert detail['settings']['publish_after']['account_id']==a.id
    assert [x['planned_publish_time'] for x in detail['items']]==current.planned_publish_times
    assert 'session_fingerprint' not in json.dumps(detail) and 'chain-fixture' not in json.dumps(detail)



def test_mix_publish_plan_rejects_unsupported_fields_and_invalid_window(db_engine,app_paths,tmp_path):
    service,_,_,_=service_for(db_engine,app_paths,tmp_path);a=account(db_engine)
    base=dict(account_id=a.id,start=(datetime.now(timezone.utc)+timedelta(hours=4)).isoformat(),interval_hours=3,linkage='whole_batch')
    for changes in [{'mode':'immediate'},{'account_ids':[a.id]},{'location':'x'},{'declaration':'x'},{'linkage':'unknown'},{'interval_hours':True},{'start':datetime.now(timezone.utc).isoformat()},{'start':(datetime.now(timezone.utc)+timedelta(days=15)).isoformat()}]:
        with pytest.raises(ValueError):service.submit(quantity=2,publish_after={**base,**changes})
    assert service.tasks()==[]
    AccountService(db_engine).set_login_state(a.id,'expired')
    with pytest.raises(ValueError):service.submit(quantity=1,publish_after=base)


def test_short_window_cannot_be_bypassed_by_software_consent(db_engine,app_paths,tmp_path):
    service,_,_,_=service_for(db_engine,app_paths,tmp_path);a=account(db_engine)
    p=dict(account_id=a.id,start=(datetime.now(timezone.utc)+timedelta(minutes=45)).isoformat(),interval_hours=3,linkage='per_item')
    with pytest.raises(ValueError):service.submit(quantity=1,publish_after=p)
    with pytest.raises(ValueError,match=r'2\.5小时后至13天后'):service.submit(quantity=1,publish_after={**p,'software_consent':True})


@pytest.mark.parametrize('ordinary',['pending','not_found'])
def test_post_mix_ordinary_schedule_uses_valid_login_without_a_probe(db_engine,ordinary):
    from yijianjifa.features.mixing.publish_after import freeze
    a=account(db_engine)
    AccountService(db_engine).update_capabilities(a.id,CapabilitySnapshot(ordinary=ordinary))
    now=datetime.now(timezone.utc)
    settings=dict(account_id=a.id,start=(now+timedelta(hours=4)).isoformat(),interval_hours=3,linkage='per_item')
    result=freeze(db_engine,2,settings,now=now)
    assert result['account']['id']==a.id and len(result['slots'])==2
    assert result['execution_enabled'] is False


@pytest.mark.parametrize('state',['expired','pending_verification','missing_session'])
def test_post_mix_schedule_still_requires_valid_login_and_saved_session(db_engine,state):
    from yijianjifa.features.mixing.publish_after import freeze
    a=account(db_engine)
    with session_scope(db_engine) as s:
        row=s.get(AccountRow,a.id)
        if state=='missing_session':row.session_ref=None
        else:row.login_state=state
    now=datetime.now(timezone.utc)
    settings=dict(account_id=a.id,start=(now+timedelta(hours=4)).isoformat(),interval_hours=3,linkage='per_item')
    with pytest.raises(ValueError,match='当前账号需要登录'):
        freeze(db_engine,1,settings,now=now)
