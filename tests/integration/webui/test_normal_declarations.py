import json
from copy import deepcopy
import pytest
from sqlalchemy import select
from test_publish_preparation import setup
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.storage.db import session_scope


def call(api,op,params):
    r=api.dispatch(json.dumps(dict(v=1,request_id='declaration-check',operation=op,params=params)),trusted_origin=True)
    assert r['ok'],r
    return r['data']


class NoPlatformMetadata:
    def __init__(self):self.calls=[]
    def __call__(self,*args):
        self.calls.append(args)
        pytest.fail('ordinary built-in declarations must not read a platform provider')
    def close(self):pass


class MetadataProvider:
    def __init__(self):self.metadata=NoPlatformMetadata()


def test_normal_builtin_declarations_allow_none_reject_unknown_and_freeze_choice(db_engine,app_paths,tmp_path):
    _,a,v,p,now=setup(db_engine,app_paths,tmp_path);provider=MetadataProvider();api=DesktopApi(db_engine,app_paths,login_provider=provider)
    try:
        unselected=call(api,'publish.prepare',p)
        assert unselected['token'] and unselected['items'][0]['declaration'] is None
        q=call(api,'publish.declarations.query',dict(account_id=a.id,cache_only=True))
        assert q['source']=='builtin' and q['available'] and q['required'] is False
        assert len(q['items'])==6 and q['catalog_version']
        p['declarations']={a.id:dict(token=q['token'],batch=None,items={})}
        explicit_none=call(api,'publish.prepare',p)
        assert explicit_none['token'] and explicit_none['items'][0]['declaration'] is None
        invalid=deepcopy(p);invalid['declarations'][a.id]['batch']='invented-option'
        bad=api.dispatch(json.dumps(dict(v=1,request_id='unknown-option',operation='publish.prepare',params=invalid)),trusted_origin=True)
        assert not bad['ok']
        choice=q['items'][0]
        p['declarations'][a.id]['batch']=choice['id']
        ready=call(api,'publish.prepare',p);assert ready['token']
        assert ready['items'][0]['declaration']==choice and not ready['submitted']
        result=call(api,'publish.confirm',dict(token=ready['token']))
        detail=call(api,'task.center.detail',dict(id=result['id']))
        assert detail['items'][0]['declaration']==choice
        with session_scope(db_engine) as s:
            data=json.loads(s.scalar(select(PublishTaskRow)).snapshot_json)
            assert not data['execution_enabled'] and data['items'][0]['declaration']==choice
        assert provider.metadata.calls==[]
    finally:api.close(.1)


def test_normal_declarations_are_account_scoped_and_refresh_revokes_preparation(db_engine,app_paths,tmp_path):
    from yijianjifa.features.accounts.service import AccountService,AccountRow
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    _,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    service=AccountService(db_engine);b=service.create('douyin','normal-second','第二账号')
    with session_scope(db_engine) as s:s.get(AccountRow,b.id).session_ref='fixture-only-b'
    service.update_capabilities(b.id,CapabilitySnapshot(ordinary='available'))
    provider=MetadataProvider();api=DesktopApi(db_engine,app_paths,login_provider=provider)
    try:
        qa=call(api,'publish.declarations.query',dict(account_id=a.id,cache_only=True));qb=call(api,'publish.declarations.query',dict(account_id=b.id,refresh=True))
        assert qa['source']==qb['source']=='builtin' and qa['items']==qb['items']
        assert qa['token']!=qb['token'] and qa['required'] is qb['required'] is False
        choice=qb['items'][0]
        p.update(account_ids=[a.id,b.id],distribution='whole_batch',declarations={a.id:dict(token=qa['token'],batch=choice['id'],items={v.id:None}),b.id:dict(token=qb['token'],batch=choice['id'],items={})})
        ready=call(api,'publish.prepare',p);assert ready['token']
        by_account={item['account_id']:item['declaration'] for item in ready['items']}
        assert by_account=={a.id:None,b.id:choice}
        foreign=deepcopy(p);foreign['declarations'][b.id]['token']=qa['token']
        bad=api.dispatch(json.dumps(dict(v=1,request_id='foreign-choice',operation='publish.prepare',params=foreign)),trusted_origin=True)
        assert not bad['ok']
        refreshed=call(api,'publish.declarations.query',dict(account_id=b.id,refresh=True))
        assert refreshed['token']!=qb['token'] and refreshed['items']==qb['items']
        bad=api.dispatch(json.dumps(dict(v=1,request_id='expired-choice',operation='publish.confirm',params=dict(token=ready['token']))),trusted_origin=True)
        assert not bad['ok']
        bad=api.dispatch(json.dumps(dict(v=1,request_id='revoked-choice',operation='publish.prepare',params=p)),trusted_origin=True)
        assert not bad['ok']
        p['declarations'][b.id]['token']=refreshed['token']
        renewed=call(api,'publish.prepare',p)
        assert renewed['token'] and {item['account_id']:item['declaration'] for item in renewed['items']}==by_account
        # Catalog refresh can replace a local reference even when publishing is
        # blocked by an expired account; it does not restore or read a session.
        service.set_login_state(b.id,'expired')
        with session_scope(db_engine) as s:s.get(AccountRow,b.id).session_ref=None
        offline=call(api,'publish.declarations.query',dict(account_id=b.id,refresh=True))
        assert offline['source']=='builtin' and offline['available'] and offline['required'] is False
        assert offline['items']==qb['items'] and offline['token']!=refreshed['token']
        account=service.get(b.id)
        assert account.login_state=='expired' and account.session_ref is None
        assert provider.metadata.calls==[]
    finally:api.close(.1)
