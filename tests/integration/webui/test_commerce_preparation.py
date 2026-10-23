from datetime import timedelta
import pytest
from test_publish_preparation import setup
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.commerce.stores import StoreCandidate


def configured(engine,paths,tmp):
    from yijianjifa.app_services.commerce_catalog import CommerceCatalog
    from yijianjifa.app_services.commerce_music import CommerceMusic
    from yijianjifa.app_services.commerce_preparation import CommercePreparation
    prep,a,v,p,now=setup(engine,paths,tmp)
    AccountService(engine).update_capabilities(a.id,CapabilitySnapshot(commerce='available',location_scope='local'))
    catalog=CommerceCatalog(engine,fetch=lambda *args:[StoreCandidate('s','店名','广西','local','unknown')])
    music=CommerceMusic(engine,fetch=lambda a:[{'id':'m','title':'收藏曲目'}])
    q=catalog.search(dict(account_id=a.id,scope='local',query='',region='',commission='all',favorite_only=False),now=now)
    m=music.query({'account_id':a.id},now=now)
    p.pop('account_ids');p.pop('distribution')
    params=dict(account_id=a.id,plan=p,store_pool=['s'],manual_store_overrides={},store_query_token=q['token'],music_choice=dict(mode='random',ids=['m'],token=m['token']))
    from yijianjifa.app_services.commerce_declarations import CommerceDeclarations
    declarations=CommerceDeclarations(engine,fetch=lambda a:dict(required=False,items=[]))
    d=declarations.query({'account_id':a.id},now=now)
    params['declarations']=dict(token=d['token'],batch=None,items={})
    return CommercePreparation(engine,paths,catalog=catalog,music=music,declarations=declarations),music,a,v,params,now


def test_commerce_preparation_freezes_store_music_and_existing_resources(db_engine,app_paths,tmp_path):
    service,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    r=service.prepare(p,now=now)
    assert r['token'] and not r['submitted'] and not r['ready_to_submit']
    assert str(tmp_path) not in str(r) and 'fixture-only-session' not in str(r)
    frozen=service.resolve(r['token'],now=now).data
    assert frozen['kind']=='commerce_publish'
    assert frozen['items'][0]['store']['commission']=='unknown'
    assert frozen['music_choice']=={'mode':'random','candidates':[{'id':'m','title':'收藏曲目'}]}
    assert frozen['videos'][0]['content_hash']==v.content_hash
    assert frozen['accounts'][0]['id']==a.id
    v.effective_path.write_bytes(b'changed')
    with pytest.raises(ValueError):service.resolve(r['token'],now=now)


def test_commerce_preparation_revokes_when_music_refreshes_or_catalog_expires(db_engine,app_paths,tmp_path):
    service,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    r=service.prepare(p,now=now)
    with pytest.raises(ValueError):service.resolve(r['token'],now=now+timedelta(minutes=6))
    music.query({'account_id':a.id},now=now)
    with pytest.raises(ValueError):service.resolve(r['token'],now=now)


def test_commerce_preparation_does_not_issue_token_for_missing_store(db_engine,app_paths,tmp_path):
    service,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    r=service.prepare(dict(p,store_pool=[]),now=now)
    assert r['token'] is None and not r['submitted']


def test_commerce_prepare_api_is_private_and_never_creates_task(db_engine,app_paths,tmp_path):
    import json
    from sqlalchemy import text
    from yijianjifa.app_services.desktop_api import DesktopApi
    service,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    api=DesktopApi(db_engine,app_paths)
    try:
        api._commerce_catalog._fetch=lambda *args:[StoreCandidate('s','店名','广西','local','unknown')]
        api._commerce_music._fetch=lambda a:[{'id':'m','title':'收藏曲目'}]
        q=api._commerce_catalog.search(dict(account_id=a.id,scope='local',query='',region='',commission='all',favorite_only=False))
        m=api._commerce_music.query({'account_id':a.id})
        api._commerce_declarations._fetch=lambda a:dict(required=False,items=[])
        d=api._commerce_declarations.query({'account_id':a.id})
        p={**p,'declarations':dict(token=d['token'],batch=None,items={}),'store_query_token':q['token'],'music_choice':dict(mode='random',ids=['m'],token=m['token'])}
        result=api.dispatch(json.dumps(dict(v=1,request_id='commerce-prepare',operation='commerce.prepare',params=p)),trusted_origin=True)
        assert result['ok'],result
        assert result['data']['token'] and not result['data']['submitted']
        assert str(tmp_path) not in json.dumps(result)
        with db_engine.connect() as c:
            for table in ['commerce_tasks','publish_tasks','publish_history']:
                assert c.execute(text('select count(*) from '+table)).scalar_one()==0
    finally:api.close()


def test_exact_preparation_expires_when_current_two_and_half_hour_boundary_moves(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    prep,_,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    p['plan']['mode']='exact'
    p['plan']['start']=(now+timedelta(hours=2.5,seconds=1)).isoformat()
    result=prep.prepare(p,now=now)
    assert result['token']
    frozen=prep.resolve(result['token'],now=now).data
    assert frozen['params']['plan']['mode']=='exact'
    with pytest.raises(ValueError):prep.resolve(result['token'],now=now+timedelta(seconds=2))


def test_custom_daily_prepare_uses_clock_list_and_freezes_it(db_engine,app_paths,tmp_path):
    from datetime import datetime,timezone,timedelta
    prep,_,a,v,p,_=configured(db_engine,app_paths,tmp_path)
    now=datetime.now(timezone.utc)
    tomorrow=(now+timedelta(days=1)).replace(hour=23,minute=0,second=0,microsecond=0)
    p['plan'].update(start=tomorrow.isoformat(),daily_count=1,days=1,daily_times=['08:15'])
    result=prep.prepare(p,now=now)
    assert result['token'] and not result['issues']
    assert result['items'][0]['time']==tomorrow.replace(hour=8,minute=15).isoformat()
    frozen=prep.resolve(result['token'],now=now).data
    assert frozen['params']['plan']['daily_times']==['08:15']


@pytest.mark.parametrize('mode',['interval','exact','immediate'])
def test_custom_times_cannot_be_silently_ignored_in_other_modes(db_engine,app_paths,tmp_path,mode):
    prep,_,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    p['plan'].update(mode=mode,daily_times=['08:15'])
    if mode=='immediate':p['plan']['start']=None
    with pytest.raises(ValueError):prep.prepare(p,now=now)


def test_item_music_rejects_foreign_video_track_random_and_expired_reference(db_engine,app_paths,tmp_path):
    service,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    choice=dict(p['music_choice'],mode='fixed')
    p['music_choice']=dict(mode='none',ids=[],token=None)
    for patch in ({'unknown':choice},{v.id:dict(choice,ids=['foreign-track'])},{v.id:dict(choice,mode='random')},{v.id:None}):
        with pytest.raises(ValueError):service.prepare(dict(p,music_overrides=patch),now=now)
    p['music_overrides']={v.id:choice}
    ready=service.prepare(p,now=now);assert ready['token']
    music.query({'account_id':a.id},now=now)
    with pytest.raises(ValueError):service.resolve(ready['token'],now=now)
