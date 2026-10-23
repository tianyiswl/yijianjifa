import json
from sqlalchemy import select
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.tasks.service import TaskCenterService
from yijianjifa.storage.db import session_scope


def call(api,op,p,rid='read'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=p)),trusted_origin=True)


def test_accounts_read_filters_do_not_expose_sessions_or_claim_unsaved_login(db_engine,app_paths):
    core=AccountService(db_engine);a=core.create('douyin','uid-a','账号A',group_name='门店');b=core.create('douyin','uid-b','账号B')
    with session_scope(db_engine) as s:s.get(AccountRow,a.id).session_ref='/private/session-cookie-token'
    api=DesktopApi(db_engine,app_paths)
    params=dict(query='',platform='',group='',state='',offset=0,limit=20)
    r=call(api,'account.list',params);assert r['ok'],r
    assert r['data']['total']==2 and r['data']['groups']==['门店']
    assert 'session_ref' not in json.dumps(r) and 'private' not in json.dumps(r)
    assert call(api,'account.list',dict(params,state='pending'))['data']['items'][0]['id']==b.id
    assert call(api,'account.list',dict(params,query='A',group='门店'))['data']['total']==1
    assert call(api,'account.get',{'id':b.id})['data']['login_state']=='pending_verification'
    api.close(.1)


def test_account_local_edit_and_default_replay_preserve_identity_and_frozen_tasks(db_engine,app_paths):
    core=AccountService(db_engine);a=core.create('douyin','uid-a','平台昵称A');b=core.create('douyin','uid-b','平台昵称B');core.set_default('douyin',a.id)
    tasks=TaskCenterService(db_engine);tasks.register_task('frozen',kind='publish',name='旧任务',state='waiting',account_id=a.id,snapshot_json=json.dumps({'account_id':a.id}))
    api=DesktopApi(db_engine,app_paths)
    p={'id':b.id,'remark':'  备注B  ','group_name':'  二组 '}
    r=call(api,'account.save',p,'save');assert r['ok'],r
    assert call(api,'account.save',p,'save')==r
    r=call(api,'account.default',{'id':b.id},'default');assert r['ok'],r
    assert call(api,'account.default',{'id':b.id},'default')==r
    item=core.get(b.id);assert item.remark=='备注B' and item.group_name=='二组'
    assert item.nickname=='平台昵称B' and item.platform_user_id=='uid-b'
    assert core.default('douyin').id==b.id and not core.get(a.id).is_default
    assert tasks.get('frozen').account_id==a.id
    assert not call(api,'account.save',dict(p,login_state='valid'),'injected')['ok']
    assert not call(api,'account.save',dict(p,remark='x'*256),'oversized')['ok']
    api.close(.1)


def test_remove_account_preserves_history_and_readd_identity(db_engine,app_paths):
    import pytest
    from yijianjifa.features.accounts.login_flow import LoginFlow
    from yijianjifa.features.accounts.session_store import SessionStore
    core=AccountService(db_engine);a=core.create('douyin','remove-test','待删除账号')
    core.set_default('douyin',a.id)
    TaskCenterService(db_engine).register_task('historical',kind='publish',name='已完成',state='completed',account_id=a.id,snapshot_json=json.dumps({'account_id':a.id}))
    api=DesktopApi(db_engine,app_paths)
    try:
        result=call(api,'account.delete',{'id':a.id},'delete-once');assert result['ok'],result
        assert call(api,'account.delete',{'id':a.id},'delete-once')==result
        assert core.list()==[] and core.default('douyin') is None
        with pytest.raises(KeyError):core.get(a.id)
        with session_scope(db_engine) as session:
            from yijianjifa.features.tasks.service import TaskCenterRow
            assert session.get(TaskCenterRow,'historical').account_id==a.id
            assert session.get(AccountRow,a.id).session_ref is None
        flow=LoginFlow(db_engine,SessionStore(app_paths.data_dir/'profiles'))
        job=flow.begin()
        restored=flow.complete(job['id'],platform_user_id='remove-test',nickname='重新登录',session_blob=b'test-session')
        assert restored['state']=='completed' and restored['account_id']==a.id
        assert len(core.list())==1 and not core.get(a.id).is_default
    finally:api.close(.1)


def test_remove_account_blocks_unfinished_frozen_reference(db_engine,app_paths):
    core=AccountService(db_engine);a=core.create('douyin','busy-test','使用中')
    TaskCenterService(db_engine).register_task('pending',kind='publish',name='未完成',state='paused',snapshot_json=json.dumps({'items':[{'account_id':a.id}]}))
    api=DesktopApi(db_engine,app_paths)
    try:
        result=call(api,'account.delete',{'id':a.id},'delete-busy')
        assert not result['ok'] and '未完成任务' in result['error']['message']
        assert core.get(a.id).nickname=='使用中'
    finally:api.close(.1)
