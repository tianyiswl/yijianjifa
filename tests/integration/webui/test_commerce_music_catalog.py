from datetime import timedelta
import pytest
from test_commerce_catalog import fixture
from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.storage.db import session_scope


def test_music_catalog_defaults_disconnected_and_none_needs_no_query(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_music import CommerceMusic
    a,now=fixture(db_engine,app_paths,tmp_path)
    music=CommerceMusic(db_engine)
    assert music.query({'account_id':a.id},now=now)=={'available':False,'token':None,'items':[],'reason':'favorite_music_not_connected'}
    assert music.resolve(a.id,{'mode':'none','ids':[],'token':None},now=now)=={'mode':'none','candidates':[]}


def test_music_choice_keeps_only_current_account_favorites_and_does_not_roll_random(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_music import CommerceMusic
    a,now=fixture(db_engine,app_paths,tmp_path)
    music=CommerceMusic(db_engine,fetch=lambda account:[{'id':'m1','title':'收藏一'},{'id':'m2','title':'收藏二'}])
    q=music.query({'account_id':a.id},now=now)
    choice={'mode':'random','ids':['m2','m1'],'token':q['token']}
    assert music.resolve(a.id,choice,now=now)=={'mode':'random','candidates':[{'id':'m2','title':'收藏二'},{'id':'m1','title':'收藏一'}]}
    for patch in [{'ids':['foreign']},{'ids':['m1','m1']},{'mode':'fixed'},{'mode':'none'}]:
        with pytest.raises(ValueError):music.resolve(a.id,{**choice,**patch},now=now)
    with pytest.raises(ValueError):music.resolve('another-account',choice,now=now)
    with pytest.raises(ValueError):music.resolve(a.id,choice,now=now+timedelta(minutes=6))
    with session_scope(db_engine) as s:s.get(AccountRow,a.id).session_ref='new-session'
    with pytest.raises(ValueError):music.resolve(a.id,choice,now=now)


def test_music_refresh_revokes_old_snapshot_and_close_clears_queries(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_music import CommerceMusic
    a,now=fixture(db_engine,app_paths,tmp_path)
    music=CommerceMusic(db_engine,fetch=lambda account:[{'id':'m','title':'收藏'}])
    old=music.query({'account_id':a.id},now=now);new=music.query({'account_id':a.id},now=now)
    with pytest.raises(ValueError):music.resolve(a.id,{'mode':'fixed','ids':['m'],'token':old['token']},now=now)
    assert music.resolve(a.id,{'mode':'fixed','ids':['m'],'token':new['token']},now=now)['candidates'][0]['id']=='m'
    music.close()
    with pytest.raises(ValueError):music.resolve(a.id,{'mode':'fixed','ids':['m'],'token':new['token']},now=now)


def test_music_query_and_preview_bridge_preserve_platform_only_choice(db_engine,app_paths,tmp_path):
    import json
    from test_publish_preparation import setup
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    AccountService(db_engine).update_capabilities(a.id,CapabilitySnapshot(commerce='available',location_scope='local'))
    api=DesktopApi(db_engine,app_paths)
    def call(op,params):return api.dispatch(json.dumps(dict(v=1,request_id='music-test',operation=op,params=params)),trusted_origin=True)
    try:
        api._commerce_music._fetch=lambda account:[{'id':'m','title':'当前账号收藏'}]
        q=call('commerce.music.query',{'account_id':a.id})
        assert q['ok'],q
        p.pop('account_ids');p.pop('distribution')
        request=dict(account_id=a.id,plan=p,store_pool=[],manual_store_overrides={},music_choice={'mode':'fixed','ids':['m'],'token':q['data']['token']})
        before=v.effective_path.read_bytes()
        result=call('commerce.preview',request)
        assert result['ok'],result
        assert result['data']['music_choice']=={'mode':'fixed','candidates':[{'id':'m','title':'当前账号收藏'}]}
        assert not result['data']['ready_to_submit'] and v.effective_path.read_bytes()==before
        assert 'fixture-only-session' not in json.dumps(result)
    finally:api.close()
