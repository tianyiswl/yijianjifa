import json
from pathlib import Path
import pytest
from sqlalchemy import select,func
from test_commerce_preparation import configured
from yijianjifa.features.commerce.service import CommerceTaskRow
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
from yijianjifa.storage.db import session_scope


def test_commerce_confirmation_copies_resources_and_freezes_music_once(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
    prep,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    token=prep.prepare(p,now=now)['token'];calls=[]
    def choose(rows):calls.append(1);return rows[-1]
    confirm=CommerceConfirmation(db_engine,app_paths,prep,choose=choose)
    result=confirm.confirm(token,now=now)
    with session_scope(db_engine) as s:
        task=s.get(CommerceTaskRow,result['id']);center=s.get(TaskCenterRow,result['id'])
        frozen=json.loads(task.snapshot_json)
        assert task.state==center.state=='paused' and center.kind=='commerce'
        assert frozen['kind']=='commerce_publish' and frozen['execution_enabled'] is False
        assert frozen['items'][0]['platform_music']=={'id':'m','title':'收藏曲目'}
        assert frozen['items'][0]['store']['id']=='s'
        assert Path(frozen['videos'][0]['path']).read_bytes()==v.effective_path.read_bytes()
        assert Path(frozen['videos'][0]['path'])!=v.effective_path
        assert s.scalar(select(func.count()).select_from(PublishTaskRow))==0
        assert s.scalar(select(func.count()).select_from(TaskItemRow))==1
        assert s.scalar(select(PublishHistoryRow.state))=='queued'
    prep.close()
    assert confirm.confirm(token,now=now)==result and len(calls)==1
    assert not result['submitted']


def test_commerce_confirmation_rolls_back_all_records_on_registration_error(db_engine,app_paths,tmp_path,monkeypatch):
    from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
    from yijianjifa.features.publish.history import PublishHistoryRepository
    prep,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    token=prep.prepare(p,now=now)['token']
    def fail(*args,**kwargs):raise RuntimeError('injected registration failure')
    monkeypatch.setattr(PublishHistoryRepository,'record',fail)
    with pytest.raises(RuntimeError):CommerceConfirmation(db_engine,app_paths,prep).confirm(token,now=now)
    with session_scope(db_engine) as s:
        for model in (CommerceTaskRow,PublishTaskRow,TaskCenterRow,TaskItemRow,PublishHistoryRow):assert s.scalar(select(func.count()).select_from(model))==0
    assert not list((app_paths.data_dir/'publish_resources').iterdir())


def test_ordinary_confirmation_cannot_accept_commerce_preparation(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.publish_confirmation import PublishConfirmation
    prep,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    with pytest.raises(ValueError,match='kind'):PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)


def test_commerce_confirm_api_replays_after_reopen_without_execution(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.desktop_api import DesktopApi
    prep,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    api=DesktopApi(db_engine,app_paths)
    # Isolated prepared snapshot is injected; real bridge and durable receipt are exercised.
    api._commerce_preparation=prep
    token=prep.prepare(p,now=now)['token']
    def call(api,params):return api.dispatch(json.dumps(dict(v=1,request_id='commerce-confirm',operation='commerce.confirm',params=params)),trusted_origin=True)
    try:
        result=call(api,{'token':token})
        assert result['ok'],result
        assert result['data']['state']=='paused' and not result['data']['submitted']
        assert not call(api,{'token':token,'execute':True})['ok']
    finally:api.close()
    reopened=DesktopApi(db_engine,app_paths)
    try:assert call(reopened,{'token':token})==result
    finally:reopened.close()


def test_commerce_task_detail_reads_frozen_platform_fields_without_private_resources(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    prep,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    result=CommerceConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    detail=TaskCenterAccess(db_engine).detail(result['id'])
    assert detail['settings']['kind']=='commerce_publish'
    assert 'cancel' in detail['controls']
    assert detail['items'][0]['store']=={'id':'s','name':'店名','region':'广西','scope':'local','commission':'unknown','address':'','favorite':False}
    assert detail['items'][0]['platform_music']=={'id':'m','title':'收藏曲目'}
    assert detail['items'][0]['declaration'] is None
    assert str(tmp_path) not in json.dumps(detail) and 'session_fingerprint' not in json.dumps(detail)


def test_commerce_local_cancel_releases_only_unstarted_history_and_keeps_resources(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
    from yijianjifa.app_services.desktop_api import DesktopApi
    prep,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    result=CommerceConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    api=DesktopApi(db_engine,app_paths)
    raw=json.dumps(dict(v=1,request_id='commerce-cancel',operation='publish.cancel',params={'id':result['id']}))
    try:
        r=api.dispatch(raw,trusted_origin=True);assert r['ok'],r
        assert r['data']['cancelled']==1 and r['data']['state']=='cancelled'
        assert api.dispatch(raw,trusted_origin=True)==r
    finally:api.close()
    with session_scope(db_engine) as s:
        task=s.get(CommerceTaskRow,result['id']);assert task.state=='cancelled'
        assert s.scalar(select(PublishHistoryRow.state))=='cancelled'
        assert Path(json.loads(task.snapshot_json)['videos'][0]['path']).is_file()


def test_commerce_cancel_keeps_unknown_result_and_occupancy(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
    from yijianjifa.app_services.publish_control import cancel_local
    prep,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    result=CommerceConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=now)['token'],now=now)
    with session_scope(db_engine) as s:s.scalar(select(PublishHistoryRow)).state='result_unknown'
    with db_engine.begin() as c:r=cancel_local(c,result['id'])
    assert r['cancelled']==0 and r['state']=='result_unknown'
    with session_scope(db_engine) as s:assert s.scalar(select(PublishHistoryRow.state))=='result_unknown'


def test_explicit_immediate_commerce_freezes_without_clock_drift_or_platform_execution(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
    prep,_,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    p['plan'].update(mode='immediate',start=None)
    result=prep.prepare(p,now=now);assert result['token']
    item=result['items'][0]
    assert item['execution_mode']=='publish_now' and item['time'] is None
    assert not item['required_choice'] and not item['software_consent']
    saved=CommerceConfirmation(db_engine,app_paths,prep).confirm(result['token'],now=now+timedelta(seconds=10))
    with session_scope(db_engine) as s:
        task=s.get(CommerceTaskRow,saved['id']);data=json.loads(task.snapshot_json)
        assert task.state=='paused' and data['execution_enabled'] is False
        assert data['items'][0]['time'] is None and data['items'][0]['execution_mode']=='publish_now'
        assert s.scalar(select(PublishHistoryRow.state))=='queued'
    assert saved['submitted'] is False


@pytest.mark.parametrize('patch',[{'start':'2026-09-26T12:00:00+08:00'},{'time_choices':'software'}])
def test_immediate_commerce_rejects_ambiguous_timing_inputs(db_engine,app_paths,tmp_path,patch):
    prep,_,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    p['plan'].update(mode='immediate',start=None)
    if 'time_choices' in patch:patch={'time_choices':{a.id+':'+v.id:{'mode':'software_schedule','software_consent':True}}}
    p['plan'].update(patch)
    with pytest.raises(ValueError):prep.prepare(p,now=now)


def test_commerce_confirmation_freezes_per_item_time_override(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
    prep,_,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    p['plan']['item_schedules']={a.id+':'+v.id:{'mode':'immediate','start':None}}
    token=prep.prepare(p,now=now)['token'];assert token
    result=CommerceConfirmation(db_engine,app_paths,prep).confirm(token,now=now)
    with session_scope(db_engine) as s:
        data=json.loads(s.get(CommerceTaskRow,result['id']).snapshot_json)
        assert data['params']['plan']['mode']=='daily'
        assert data['items'][0]['execution_mode']=='publish_now' and data['items'][0]['time_overridden']
        assert data['items'][0]['store']['id']=='s' and data['execution_enabled'] is False


def test_item_music_overrides_freeze_without_changing_batch_random_pool(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_confirmation import CommerceConfirmation
    from yijianjifa.features.finished.service import FinishedVideoService
    prep,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    music._fetch=lambda account:[{'id':'m','title':'收藏一'},{'id':'m2','title':'收藏二'}]
    token=music.query({'account_id':a.id},now=now)['token']
    extra=[]
    for i in range(2):
        source=tmp_path/f'extra-{i}.mp4';source.write_bytes(f'fixture-{i}'.encode())
        extra.append(FinishedVideoService(db_engine).register_generated(source,duration_ms=3000))
    from datetime import timedelta
    # Keep this music test away from the daily schedule midnight boundary.
    start=(now+timedelta(days=1)).replace(hour=9,minute=0,second=0,microsecond=0)
    p['plan'].update(video_ids=[v.id,*[x.id for x in extra]],daily_count=3,start=start.isoformat())
    p['music_choice']=dict(mode='random',ids=['m','m2'],token=token)
    p['music_overrides']={v.id:dict(mode='none',ids=[],token=None),extra[0].id:dict(mode='fixed',ids=['m2'],token=token)}
    ready=prep.prepare(p,now=now);assert ready['token'],ready
    calls=[]
    def choose(rows):calls.append(rows);return rows[0]
    confirm=CommerceConfirmation(db_engine,app_paths,prep,choose=choose)
    result=confirm.confirm(ready['token'],now=now)
    with session_scope(db_engine) as s:
        data=json.loads(s.get(CommerceTaskRow,result['id']).snapshot_json)
        assert [x['platform_music'] for x in data['items']]==[None,{'id':'m2','title':'收藏二'},{'id':'m','title':'收藏一'}]
        assert data['music_choice']['mode']=='random' and len(data['music_choice']['candidates'])==2
        assert data['execution_enabled'] is False
        assert [Path(x['path']).read_bytes() for x in data['videos']]==[b'local-test-fixture',b'fixture-0',b'fixture-1']
    prep.close();assert confirm.confirm(ready['token'],now=now)==result and len(calls)==1
