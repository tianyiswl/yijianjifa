import json
from datetime import datetime,timezone,timedelta
import pytest
from yijianjifa.app_services.publish_preparation import PublishPreparation
from yijianjifa.features.accounts.service import AccountService,AccountRow
from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.storage.models import Setting
from yijianjifa.storage.db import session_scope


def setup(engine,paths,tmp_path):
    a=AccountService(engine).create('douyin','fixture-identity','测试账号')
    with session_scope(engine) as s:s.get(AccountRow,a.id).session_ref='fixture-only-session'
    AccountService(engine).update_capabilities(a.id,CapabilitySnapshot(ordinary='available'))
    video=tmp_path/'input.mp4';video.write_bytes(b'local-test-fixture')
    v=FinishedVideoService(engine).register_generated(video,duration_ms=3000,title='原始标题')
    now=datetime.now(timezone.utc)
    p=dict(account_ids=[a.id],video_ids=[v.id],distribution='split_across_accounts',start=(now+timedelta(hours=4)).isoformat(),mode='daily',interval_hours=3,daily_count=1,days=1)
    return PublishPreparation(engine,paths),a,v,p,now


def verify_optional_declarations(api,params):
    """Explicit offline policy fixture, through the same declaration query API."""
    api._publish_declarations._fetch=lambda account:dict(required=False,items=[])
    params['declarations']={}
    for aid in params['account_ids']:
        result=api.dispatch(json.dumps(dict(v=1,request_id='optional-policy',operation='publish.declarations.query',params=dict(account_id=aid))),trusted_origin=True)
        assert result['ok'],result
        params['declarations'][aid]=dict(token=result['data']['token'],batch=None,items={})


def test_preparation_freezes_current_metadata_and_detects_source_replacement(db_engine,app_paths,tmp_path):
    service,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    r=service.prepare(p,now=now);assert r['token'] and not r['submitted']
    assert 'fixture-only-session' not in json.dumps(r) and str(tmp_path) not in json.dumps(r)
    frozen=service.resolve(r['token'],now=now).data
    assert frozen['items'][0]['title']=='原始标题'
    assert frozen['videos'][0]['content_hash']==v.content_hash
    assert frozen['videos'][0]['cover']['kind']=='first_frame'
    v.effective_path.write_bytes(b'replaced-video')
    with pytest.raises(ValueError,match='changed'):service.resolve(r['token'],now=now)


def test_preparation_checks_saved_cover_and_account_identity_again(db_engine,app_paths,tmp_path):
    import hashlib
    service,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    image=b'fixture-cover';digest=hashlib.sha256(image).hexdigest();folder=app_paths.data_dir/'covers';folder.mkdir();(folder/(digest+'.png')).write_bytes(image)
    with session_scope(db_engine) as s:s.add(Setting(key='finished-cover:'+v.id,value_json=json.dumps(dict(kind='uploaded',frame_ms=None,sha256=digest))))
    r=service.prepare(p,now=now)
    assert service.resolve(r['token'],now=now).data['videos'][0]['cover']['sha256']==digest
    with session_scope(db_engine) as s:s.get(AccountRow,a.id).platform_user_id='different-identity'
    with pytest.raises(ValueError,match='changed'):service.resolve(r['token'],now=now)
    (folder/(digest+'.png')).write_bytes(b'bad')
    with pytest.raises(ValueError,match='cover'):service.prepare(p,now=now)


def test_preparation_refuses_blocked_plan_and_expired_token(db_engine,app_paths,tmp_path):
    service,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    r=service.prepare(p,now=now)
    with pytest.raises(ValueError,match='expired'):service.resolve(r['token'],now=now+timedelta(minutes=21))
    AccountService(db_engine).set_login_state(a.id,'expired')
    r=service.prepare(p,now=now);assert r['token'] is None and not r['locally_valid']

def test_prepare_api_returns_private_token_without_paths_or_confirming_task(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.desktop_api import DesktopApi
    from sqlalchemy import select,func
    from yijianjifa.features.publish.normal_service import PublishTaskRow
    service,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    api=DesktopApi(db_engine,app_paths)
    verify_optional_declarations(api,p)
    response=api.dispatch(json.dumps(dict(v=1,request_id='prepare',operation='publish.prepare',params=p)),trusted_origin=True)
    assert response['ok'],response
    assert response['data']['token'] and not response['data']['submitted']
    assert str(tmp_path) not in json.dumps(response)
    with session_scope(db_engine) as s:assert s.scalar(select(func.count()).select_from(PublishTaskRow))==0
    api.close(.1)

def test_preparation_rejects_metadata_and_new_history_after_preview(db_engine,app_paths,tmp_path):
    from yijianjifa.features.publish.history import PublishHistoryRepository
    service,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    first=service.prepare(p,now=now)
    FinishedVideoService(db_engine).update_metadata(v.id,title='后来修改',body='',tags='')
    with pytest.raises(ValueError,match='changed'):service.resolve(first['token'],now=now)
    second=service.prepare(p,now=now)
    PublishHistoryRepository(db_engine).record(v.content_hash,a.id,'result_unknown')
    with pytest.raises(ValueError,match='changed'):service.resolve(second['token'],now=now)


def test_daily_clock_account_exception_freezes_separate_times(db_engine,app_paths,tmp_path):
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    service=AccountService(db_engine);other=service.create('douyin','other-clocks','第二账号')
    with session_scope(db_engine) as s:s.get(AccountRow,other.id).session_ref='other-fixture-only-session'
    service.update_capabilities(other.id,CapabilitySnapshot(ordinary='available'))
    tomorrow=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
    p.update(account_ids=[a.id,other.id],distribution='whole_batch',start=tomorrow,daily_times=['09:15'],account_schedules={other.id:{'mode':'daily','start':tomorrow,'interval_hours':3,'daily_count':1,'days':1,'daily_times':['11:30']}})
    result=prep.prepare(p,now=now);assert result['token']
    frozen=prep.resolve(result['token'],now=now).data
    times={x['account_id']:datetime.fromisoformat(x['time']).strftime('%H:%M') for x in frozen['items']}
    assert times=={a.id:'09:15',other.id:'11:30'}
    assert frozen['params']['daily_times']==['09:15'] and frozen['params']['account_schedules'][other.id]['daily_times']==['11:30']


def test_real_session_refresh_invalidates_existing_preparation(db_engine,app_paths,tmp_path):
    from yijianjifa.features.accounts.session_store import SessionStore
    service,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    accounts=AccountService(db_engine);store=SessionStore(tmp_path/'isolated-profiles')
    accounts.attach_session(a.id,store,b'synthetic initial session')
    prepared=service.prepare(p,now=now)
    assert prepared['token']
    accounts.attach_session(a.id,store,b'synthetic refreshed session')
    with pytest.raises(ValueError,match='changed'):service.resolve(prepared['token'],now=now)
    assert service.prepare(p,now=now)['token']
