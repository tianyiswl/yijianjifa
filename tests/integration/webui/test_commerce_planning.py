import pytest
from test_publish_preparation import setup
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.models import CapabilitySnapshot


def test_commerce_context_never_substitutes_another_account(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path);accounts=AccountService(db_engine)
    accounts.set_default('douyin',a.id)
    other=accounts.create('douyin','other','Other')
    accounts.update_capabilities(other.id,CapabilitySnapshot(commerce='available'))
    service=CommercePlanning(db_engine)
    assert service.context()['default_account_id'] is None
    accounts.update_capabilities(a.id,CapabilitySnapshot(commerce='available',location_scope='local'))
    ctx=service.context();assert ctx['default_account_id']==a.id
    assert next(x for x in ctx['accounts'] if x['id']==a.id)['store_scopes']==['local']
    assert ctx['stores_available'] is False
    assert 'fixture-only-session' not in str(ctx)


def test_commerce_preview_requires_store_and_uses_commerce_capability(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    AccountService(db_engine).update_capabilities(a.id,CapabilitySnapshot(ordinary='not_found',commerce='available',location_scope='local'))
    p.pop('account_ids');p.pop('distribution')
    request=dict(account_id=a.id,plan=p,store_pool=[],manual_store_overrides={})
    result=CommercePlanning(db_engine).preview(request,now=now)
    assert not result['locally_valid'] and result['items'][0]['store_id'] is None
    assert 'store_required' in result['items'][0]['blockers']
    assert 'ordinary_capability_unverified' not in result['items'][0]['blockers']
    assert result['kind']=='commerce' and not result['ready_to_submit']
    with pytest.raises(ValueError):CommercePlanning(db_engine).preview(dict(request,account_id=[a.id]),now=now)


def test_commerce_allocation_preserves_stale_manual_store_and_does_not_trust_ui(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path);p.pop('account_ids');p.pop('distribution')
    request=dict(account_id=a.id,plan=p,store_pool=['store-a'],manual_store_overrides={v.id:'removed-store'})
    r=CommercePlanning(db_engine).preview(request,now=now)
    assert r['items'][0]['store_id']=='removed-store'
    assert 'store_outside_pool' in r['items'][0]['blockers']
    assert 'store_availability_unverified' in r['items'][0]['blockers']
    assert not r['locally_valid']


def test_commerce_text_overrides_are_account_scoped_and_never_edit_finished(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    from yijianjifa.features.finished.service import FinishedVideoService
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path);p.pop('account_ids');p.pop('distribution')
    p['item_overrides']={a.id+':'+v.id:{'title':'本次团购标题','body':'','tags':'本次标签'}}
    request=dict(account_id=a.id,plan=p,store_pool=[],manual_store_overrides={})
    r=CommercePlanning(db_engine).preview(request,now=now)
    assert r['items'][0]['title']=='本次团购标题' and r['items'][0]['body']==''
    assert FinishedVideoService(db_engine).get(v.id).title=='原始标题'
    p['item_overrides']={'other:'+v.id:{'title':'错误账号'}}
    with pytest.raises(ValueError):CommercePlanning(db_engine).preview(request,now=now)


@pytest.mark.parametrize('hour,expected_crossing',[(23,True),(7,False)])
def test_commerce_daily_boundary_uses_submitted_calendar_offset(db_engine,app_paths,tmp_path,hour,expected_crossing):
    from datetime import datetime,timezone,timedelta
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    _,a,v,p,_=setup(db_engine,app_paths,tmp_path)
    p.pop('account_ids');p.pop('distribution')
    local=timezone(timedelta(hours=8))
    start=datetime(2026,9,26,hour,30,tzinfo=local)
    p.update(start=start.isoformat(),interval_hours=1,daily_count=2,days=1)
    result=CommercePlanning(db_engine).preview(dict(account_id=a.id,plan=p,store_pool=[],manual_store_overrides={}),now=start-timedelta(hours=3))
    assert not any(x['code'] in ('daily_crosses_midnight','daily_groups_overlap') for x in result['issues'])
    assert result['items'][0]['time']==start.isoformat()


@pytest.mark.parametrize('hours,expected_blocker',[(2.5,None),(336,'beyond_platform_window')])
def test_commerce_interval_continues_over_midnight_and_checks_each_platform_window(db_engine,app_paths,tmp_path,hours,expected_blocker):
    from datetime import datetime,timezone,timedelta
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    _,a,v,p,_=setup(db_engine,app_paths,tmp_path)
    other=tmp_path/'other.mp4';other.write_bytes(b'other-video-fixture')
    second=FinishedVideoService(db_engine).register_generated(other,duration_ms=3000,title='第二条')
    p.pop('account_ids');p.pop('distribution')
    start=datetime(2026,9,26,23,30,tzinfo=timezone(timedelta(hours=8)))
    p.update(video_ids=[v.id,second.id],start=start.isoformat(),mode='interval',interval_hours=hours,daily_count=1,days=1)
    result=CommercePlanning(db_engine).preview(dict(account_id=a.id,plan=p,store_pool=[],manual_store_overrides={}),now=start-timedelta(hours=3))
    assert result['issues']==[]
    assert result['items'][1]['time']==(start+timedelta(hours=hours)).isoformat()
    assert ('beyond_platform_window' in result['items'][1]['blockers']) is (expected_blocker is not None)
    assert 'schedule_capacity_exceeded' not in result['items'][1]['blockers']


@pytest.mark.parametrize('offset,required,error',[(3,False,None),(1,False,'before_schedule_window'),(-1,False,'scheduled_time_in_past'),(337,False,'beyond_platform_window')])
def test_commerce_exact_time_retains_platform_gates(db_engine,app_paths,tmp_path,offset,required,error):
    from datetime import timedelta
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    _,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    p.pop('account_ids');p.pop('distribution');p.update(mode='exact',start=(now+timedelta(hours=offset)).isoformat())
    result=CommercePlanning(db_engine).preview(dict(account_id=a.id,plan=p,store_pool=[],manual_store_overrides={}),now=now)
    item=result['items'][0]
    assert result['issues']==[] and item['time']==p['start']
    assert bool(item['required_choice']) is required
    if error:assert error in item['blockers']


def test_per_item_time_override_changes_only_its_own_slot(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    _,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    other=tmp_path/'override-other.mp4';other.write_bytes(b'override-other')
    second=FinishedVideoService(db_engine).register_generated(other,duration_ms=3000,title='第二条')
    p.pop('account_ids');p.pop('distribution')
    p.update(mode='interval',video_ids=[v.id,second.id],item_schedules={a.id+':'+v.id:{'mode':'exact','start':(now+timedelta(hours=1)).isoformat()}})
    request=dict(account_id=a.id,plan=p,store_pool=[],manual_store_overrides={})
    r=CommercePlanning(db_engine).preview(request,now=now)
    assert 'before_schedule_window' in r['items'][0]['blockers'] and r['items'][0]['time_overridden']
    assert r['items'][1]['time']==(now+timedelta(hours=7)).isoformat()
    assert not r['items'][1]['time_overridden']
    p['item_schedules'][a.id+':'+v.id]={'mode':'immediate','start':None}
    r=CommercePlanning(db_engine).preview(request,now=now)
    assert r['items'][0]['execution_mode']=='publish_now' and r['items'][0]['time'] is None
    assert r['items'][1]['execution_mode']=='platform_schedule'
    del p['item_schedules']
    assert CommercePlanning(db_engine).preview(request,now=now)['items'][0]['time']==p['start']


def test_scheduled_item_override_on_immediate_batch_keeps_time_gates(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    _,a,v,p,now=setup(db_engine,app_paths,tmp_path);p.pop('account_ids');p.pop('distribution')
    key=a.id+':'+v.id
    p.update(mode='immediate',start=None,item_schedules={key:{'mode':'exact','start':(now+timedelta(days=15)).isoformat()}})
    request=dict(account_id=a.id,plan=p,store_pool=[],manual_store_overrides={})
    r=CommercePlanning(db_engine).preview(request,now=now)
    assert 'beyond_platform_window' in r['items'][0]['blockers'] and r['items'][0]['execution_mode']=='platform_schedule'
    p['item_schedules'][key]['start']=(now+timedelta(hours=1)).isoformat()
    p['time_choices']={key:{'mode':'software_schedule','software_consent':True}}
    r=CommercePlanning(db_engine).preview(request,now=now)
    assert not r['items'][0]['required_choice'] and 'before_schedule_window' in r['items'][0]['blockers']
    p['item_schedules']['wrong:video']=p['item_schedules'][key]
    with pytest.raises(ValueError):CommercePlanning(db_engine).preview(request,now=now)
