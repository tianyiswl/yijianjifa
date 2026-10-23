import json
from datetime import timedelta
import pytest
from test_publish_preparation import setup,verify_optional_declarations
from test_normal_declarations import call
from yijianjifa.app_services.desktop_api import DesktopApi


def test_location_query_is_offline_optional_and_account_scoped(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.publish_locations import PublishLocations
    _,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    missing=PublishLocations(db_engine).search(dict(account_id=a.id,query='地点'),now=now)
    assert not missing['available'] and missing['items']==[]
    locations=PublishLocations(db_engine,fetch=lambda account,query:[dict(id='s',name='地点名称',address='完整地址')])
    q=locations.search(dict(account_id=a.id,query='地点'),now=now)
    assert locations.resolve(a.id,None,now=now) is None
    assert locations.resolve(a.id,dict(token=q['token'],id='s'),now=now)==dict(id='s',name='地点名称',address='完整地址')
    for owner,ref,when in [('other',dict(token=q['token'],id='s'),now),(a.id,dict(token=q['token'],id='bad'),now),(a.id,dict(token=q['token'],id='s',name='invented'),now)]:
        with pytest.raises(ValueError):locations.resolve(owner,ref,now=when)


def test_optional_location_selection_clear_and_freeze_through_desktop_api(db_engine,app_paths,tmp_path):
    _,a,v,p,now=setup(db_engine,app_paths,tmp_path);api=DesktopApi(db_engine,app_paths)
    try:
        verify_optional_declarations(api,p)
        assert call(api,'publish.prepare',p)['token']  # optional, no query needed
        api._publish_locations._fetch=lambda account,query:[dict(id='s',name='实际地点',address='实际地址')]
        q=call(api,'publish.locations.search',dict(account_id=a.id,query='实际地点'))
        p['locations']={a.id:dict(batch=dict(token=q['token'],id='s'),items={v.id:None})}
        ready=call(api,'publish.prepare',p);assert ready['token'] and ready['items'][0]['location'] is None
        p['locations'][a.id]['items']={}
        ready=call(api,'publish.prepare',p);assert ready['items'][0]['location']['name']=='实际地点'
        result=call(api,'publish.confirm',dict(token=ready['token']))
        detail=call(api,'task.center.detail',dict(id=result['id']))
        assert detail['items'][0]['location']==dict(id='s',name='实际地点',address='实际地址')
        assert detail['task']['state']=='paused' and not detail['settings']['execution_enabled']
        api._publish_locations.close()
        assert call(api,'publish.confirm',dict(token=ready['token']))==result
    finally:api.close(.1)
