from datetime import timedelta
import pytest
from test_commerce_catalog import fixture


def test_declarations_unknown_policy_is_not_optional(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_declarations import CommerceDeclarations
    a,now=fixture(db_engine,app_paths,tmp_path)
    service=CommerceDeclarations(db_engine)
    r=service.query({'account_id':a.id},now=now)
    assert not r['available'] and r['required'] is None and r['items']==[]
    with pytest.raises(ValueError):service.resolve(a.id,None,['v'],None,{},now=now)


def test_declarations_require_explicit_choice_and_preserve_per_item_clear(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_declarations import CommerceDeclarations
    a,now=fixture(db_engine,app_paths,tmp_path)
    service=CommerceDeclarations(db_engine,fetch=lambda a:dict(required=True,items=[{'id':'d','label':'平台实际选项'}]))
    q=service.query({'account_id':a.id},now=now)
    r=service.resolve(a.id,q['token'],['v1','v2'],'d',{'v2':None},now=now)
    assert r['items']['v1']=={'id':'d','label':'平台实际选项'}
    assert r['items']['v2'] is None and r['blocking_video_ids']==['v2']
    r=service.resolve(a.id,q['token'],['v1'],None,{},now=now)
    assert r['blocking_video_ids']==['v1']
    for batch,overrides in [('invented',{}),('d',{'unselected':'d'}),('d',{'v1':'invented'})]:
        with pytest.raises(ValueError):service.resolve(a.id,q['token'],['v1'],batch,overrides,now=now)
    with pytest.raises(ValueError):service.resolve('other',q['token'],['v1'],'d',{},now=now)
    with pytest.raises(ValueError):service.resolve(a.id,q['token'],['v1'],'d',{},now=now+timedelta(minutes=6))


def test_optional_declaration_can_stay_empty_and_refresh_revokes_old_policy(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_declarations import CommerceDeclarations
    a,now=fixture(db_engine,app_paths,tmp_path)
    service=CommerceDeclarations(db_engine,fetch=lambda a:dict(required=False,items=[]))
    q=service.query({'account_id':a.id},now=now)
    assert service.resolve(a.id,q['token'],['v'],None,{},now=now)['blocking_video_ids']==[]
    service.query({'account_id':a.id},now=now)
    with pytest.raises(ValueError):service.resolve(a.id,q['token'],['v'],None,{},now=now)


def test_preview_blocks_unknown_policy_and_freezes_resolved_declaration(db_engine,app_paths,tmp_path):
    from test_commerce_preparation import configured
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    from yijianjifa.app_services.commerce_declarations import CommerceDeclarations
    service,music,a,v,p,now=configured(db_engine,app_paths,tmp_path)
    planner=service._planning
    unknown=planner.preview({k:value for k,value in p.items() if k!='declarations'},now=now)
    assert 'declaration_policy_unverified' in unknown['items'][0]['blockers']
    declarations=CommerceDeclarations(db_engine,fetch=lambda a:dict(required=True,items=[{'id':'d','label':'实际声明'}]))
    q=declarations.query({'account_id':a.id},now=now)
    planner=CommercePlanning(db_engine,catalog=planner.catalog,music=music,declarations=declarations)
    p={**p,'declarations':dict(token=q['token'],batch=None,items={})}
    assert 'declaration_required' in planner.preview(p,now=now)['items'][0]['blockers']
    p['declarations']['items']={v.id:'d'}
    r=planner.preview(p,now=now)
    assert r['locally_valid'] and r['items'][0]['declaration']=={'id':'d','label':'实际声明'}
