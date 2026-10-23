from datetime import datetime, timezone, timedelta
import pytest
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.history import PublishHistoryRepository
from yijianjifa.app_services.publish_planning import PublishPlanning

NOW=datetime(2026,9,25,8,tzinfo=timezone.utc)
def fixture(engine,tmp_path):
    a=AccountService(engine).create('douyin','test','测试账号')
    AccountService(engine).set_default('douyin',a.id)
    videos=[]
    for i in range(3):
        p=tmp_path/f'{i}.mp4';p.write_bytes(f'fixture{i}'.encode())
        videos.append(FinishedVideoService(engine).register_generated(p,duration_ms=1000,title=f'标题{i}'))
    return a,videos

def params(a, videos):
    return dict(account_ids=[a.id],video_ids=[v.id for v in videos],distribution='split_across_accounts',
                start=(NOW+timedelta(hours=3)).isoformat(),mode='daily',interval_hours=3,daily_count=2,days=2)

def test_quick_candidates_obey_global_history_and_read_is_side_effect_free(db_engine,tmp_path):
    a,v=fixture(db_engine,tmp_path);h=PublishHistoryRepository(db_engine)
    h.record(v[0].content_hash,'another-account','published');h.record(v[1].content_hash,a.id,'pre_submit_failed')
    planner=PublishPlanning(db_engine)
    r=planner.candidates(query='',offset=0,limit=20)
    assert [x['id'] for x in r['items']]==[v[2].id]
    assert r['default_account_id']==a.id
    assert 'path' not in str(r)
    assert not h.states_for_video(v[2].content_hash)

def test_schedule_preview_uses_each_accounts_slots_and_reports_time_choices(db_engine,tmp_path):
    a,v=fixture(db_engine,tmp_path);b=AccountService(db_engine).create('douyin','other','测试B')
    planner=PublishPlanning(db_engine);p=params(a,v);p['account_ids'].append(b.id)
    result=planner.preview(p,now=NOW)
    assert [x['account_id'] for x in result['items']]==[a.id,b.id,a.id]
    assert [x['time'] for x in result['items']]==[p['start'],p['start'],(NOW+timedelta(hours=6)).isoformat()]
    assert all('account_needs_verification' in x['blockers'] for x in result['items'])
    p['start']=(NOW+timedelta(hours=1)).isoformat()
    result=planner.preview(p,now=NOW)
    assert 'before_schedule_window' in result['items'][0]['blockers']
    assert result['items'][0]['required_choice']==[]
    assert not result['ready_to_submit']
    assert PublishHistoryRepository(db_engine).states_for_video(v[0].content_hash)==set()

def test_plan_rejects_invalid_numbers_and_reports_duplicate_and_missing_file(db_engine,tmp_path):
    a,v=fixture(db_engine,tmp_path);planner=PublishPlanning(db_engine);p=params(a,v)
    for bad in (float('nan'),float('inf'),True,0,-1):
        with pytest.raises(ValueError):planner.preview(dict(p,interval_hours=bad),now=NOW)
    PublishHistoryRepository(db_engine).record(v[0].content_hash,a.id,'result_unknown')
    v[1].effective_path.unlink()
    r=planner.preview(p,now=NOW)
    assert 'duplicate_blocked' in r['items'][0]['blockers']
    assert 'file_missing' in r['items'][1]['blockers']
    assert r['items'][0]['title']=='标题0'

def test_planning_api_is_finite_read_only(db_engine,app_paths,tmp_path):
    import json
    from yijianjifa.app_services.desktop_api import DesktopApi
    a,v=fixture(db_engine,tmp_path);api=DesktopApi(db_engine,app_paths)
    def call(op,p):return api.dispatch(json.dumps(dict(v=1,request_id='preview',operation=op,params=p)),trusted_origin=True)
    r=call('publish.candidates',dict(query='',offset=0,limit=20));assert r['ok'],r
    assert r['data']['total']==3
    r=call('publish.preview',params(a,v));assert r['ok'],r
    assert r['data']['preview_only'] and not r['data']['ready_to_submit']
    assert not call('publish.preview',dict(params(a,v),execute=True))['ok']
    api.close(.1)

def test_advanced_candidates_show_success_without_reviving_unresolved(db_engine,tmp_path):
    a,v=fixture(db_engine,tmp_path);history=PublishHistoryRepository(db_engine)
    history.record(v[0].content_hash,a.id,'published');history.record(v[1].content_hash,a.id,'result_unknown')
    planner=PublishPlanning(db_engine)
    hidden=planner.candidates(query='',offset=0,limit=20,advanced=True,include_published=False)
    shown=planner.candidates(query='',offset=0,limit=20,advanced=True,include_published=True)
    assert [r['id'] for r in hidden['items']]==[v[2].id]
    assert {r['id'] for r in shown['items']}=={v[0].id,v[2].id}
    assert next(r for r in shown['items'] if r['id']==v[0].id)['published']
    with pytest.raises(ValueError):planner.candidates(query='',offset=0,limit=20,include_published=True)

def test_preview_reports_underfilled_accounts_and_daily_time_overflow(db_engine,tmp_path):
    a,v=fixture(db_engine,tmp_path);b=AccountService(db_engine).create('douyin','b','测试B')
    p=params(a,v[:1]);p['account_ids'].append(b.id)
    r=PublishPlanning(db_engine).preview(p,now=NOW)
    assert {i['account_id'] for i in r['issues']}=={a.id,b.id}
    assert all(i['code']=='schedule_underfilled' for i in r['issues'])
    p=params(a,v);p['start']='2026-09-25T23:00:00+08:00'
    r=PublishPlanning(db_engine).preview(p,now=NOW)
    assert not any(i['code'] in ('daily_crosses_midnight','daily_groups_overlap') for i in r['issues'])
    p['interval_hours']=24
    r=PublishPlanning(db_engine).preview(p,now=NOW)
    assert any(i['code']=='daily_groups_overlap' for i in r['issues'])

def test_manual_distribution_and_item_text_are_scoped_and_do_not_mutate_library(db_engine,tmp_path):
    a,v=fixture(db_engine,tmp_path);b=AccountService(db_engine).create('douyin','b','B');p=params(a,v[:2]);p['account_ids'].append(b.id)
    p['moves']={v[0].id:b.id};p['item_overrides']={b.id+':'+v[0].id:{'title':'仅B本次标题','body':'','tags':'#本次'}}
    result=PublishPlanning(db_engine).preview(p,now=NOW)
    assert [x['account_id'] for x in result['items']]==[b.id,b.id]
    assert result['items'][0]['title']=='仅B本次标题' and result['items'][0]['body']==''
    assert FinishedVideoService(db_engine).get(v[0].id).title=='标题0'
    p['distribution']='whole_batch';p['moves']={}
    result=PublishPlanning(db_engine).preview(p,now=NOW)
    assert next(x for x in result['items'] if x['account_id']==a.id and x['video_id']==v[0].id)['title']=='标题0'
    assert next(x for x in result['items'] if x['account_id']==b.id and x['video_id']==v[0].id)['title']=='仅B本次标题'
    for bad in ({'outside':b.id},{v[0].id:'outside'}):
        with pytest.raises(ValueError):PublishPlanning(db_engine).preview(dict(p,distribution='split_across_accounts',moves=bad),now=NOW)
    with pytest.raises(ValueError):PublishPlanning(db_engine).preview(dict(p,item_overrides={b.id+':'+v[0].id:{'session_ref':'secret'}}),now=NOW)

def test_manual_move_over_capacity_is_an_explicit_issue_not_missing_preview(db_engine,tmp_path):
    a,v=fixture(db_engine,tmp_path);b=AccountService(db_engine).create('douyin','b','B');p=params(a,v[:2]);p.update(account_ids=[a.id,b.id],daily_count=1,days=1,moves={v[0].id:b.id})
    r=PublishPlanning(db_engine).preview(p,now=NOW)
    assert len(r['items'])==2
    assert any(x['code']=='schedule_overfilled' and x['account_id']==b.id for x in r['issues'])
    assert not r['locally_valid']

def test_account_schedule_exception_only_affects_its_account_and_is_validated(db_engine,tmp_path):
    a,v=fixture(db_engine,tmp_path);b=AccountService(db_engine).create('douyin','b','B');p=params(a,v[:2]);p['account_ids'].append(b.id)
    exception=dict(start=(NOW+timedelta(hours=1)).isoformat(),mode='interval',interval_hours=4,daily_count=1,days=1)
    p['account_schedules']={b.id:exception}
    r=PublishPlanning(db_engine).preview(p,now=NOW)
    assert r['items'][0]['time']==p['start']
    assert r['items'][1]['time']==exception['start']
    assert 'before_schedule_window' not in r['items'][0]['blockers'] and 'before_schedule_window' in r['items'][1]['blockers']
    for patch in ({'outside':exception},{b.id:dict(exception,interval_hours=True)},{b.id:dict(exception,start='2026-09-25T12:00:00')},{b.id:dict(exception,execute=True)}):
        with pytest.raises(ValueError):PublishPlanning(db_engine).preview(dict(p,account_schedules=patch),now=NOW)


def test_publish_candidates_include_library_presentation_without_paths(db_engine,app_paths,tmp_path):
    from test_publish_preparation import setup
    from yijianjifa.features.finished.service import FinishedVideo
    from yijianjifa.storage.db import session_scope
    from yijianjifa.app_services.publish_planning import PublishPlanning
    _,_,video,_,_=setup(db_engine,app_paths,tmp_path)
    with session_scope(db_engine) as session:session.get(FinishedVideo,video.id).note='发布页备注'
    row=PublishPlanning(db_engine).candidates(query='',offset=0,limit=20)['items'][0]
    assert row['note']=='发布页备注' and row['name']=='input' and row['format']=='MP4'
    assert row['category_ids']==[] and row['category_names']==[]
    assert 'original_path' not in row and 'stored_path' not in row

@pytest.mark.parametrize('minutes,blocked',[(149,True),(150,False),(151,False),(180,False)])
def test_minimum_lead_time_is_two_and_half_hours(db_engine,tmp_path,minutes,blocked):
    a,v=fixture(db_engine,tmp_path);p=params(a,v[:1]);p.update(mode='exact',start=(NOW+timedelta(minutes=minutes)).isoformat())
    r=PublishPlanning(db_engine).preview(p,now=NOW)
    assert ('before_schedule_window' in r['items'][0]['blockers']) is blocked

@pytest.mark.parametrize('state',['pre_submit_failed','published','result_unknown','platform_scheduled'])
def test_simple_user_publication_lists_history_and_prepares_a_new_request(db_engine,tmp_path,state):
    account,videos=fixture(db_engine,tmp_path)
    history=PublishHistoryRepository(db_engine)
    history.record(videos[0].content_hash,account.id,state)
    planner=PublishPlanning(db_engine)
    candidates=planner.candidates(query='',offset=0,limit=20,allow_new_request=True)
    assert videos[0].id in [v['id'] for v in candidates['items']]
    value=params(account,[videos[0]])
    value['allow_new_request']=True
    result=planner.preview(value,now=NOW)
    assert 'duplicate_blocked' not in result['items'][0]['blockers']
    assert not result['items'][0]['repeat_confirmation_required']
    assert result['items'][0]['allow_new_request'] is True
    assert history.states_for_video(videos[0].content_hash)=={state}
