"""Host registration must make every integrated login reachable from the UI."""
import json
import pytest
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService


def call(api,operation,params,request_id):
    return api.dispatch(json.dumps(dict(v=1,request_id=request_id,operation=operation,params=params)),trusted_origin=True)


def test_host_registry_advertises_all_four_login_platforms(db_engine,app_paths):
    from yijianjifa.webui.creator_providers import make_creator_login_providers
    api=DesktopApi(db_engine,app_paths,login_provider=lambda *args:None,login_providers=make_creator_login_providers(db_engine,app_paths))
    try:
        result=call(api,'account.login.availability',{},'availability')
        assert result['data']=={'available':True,'platforms':['douyin','kuaishou','channels','bilibili']}
        assert AccountService(db_engine).list()==[]
    finally:api.close(1)


@pytest.mark.parametrize('platform', ['channels','bilibili'])
def test_selected_platform_saves_avatar_login_time_and_keeps_posting_pending(db_engine,app_paths,platform):
    def provider(account_id,cancel):
        return dict(platform_user_id='123',nickname='Fixture '+platform,session_blob=b'synthetic-session',avatar_data='data:image/jpeg;base64,/9j/')
    other='bilibili' if platform=='channels' else 'channels'
    def wrong_provider(*args):raise AssertionError('selected platform lost')
    api=DesktopApi(db_engine,app_paths,login_providers={platform:provider,other:wrong_provider})
    try:
        started=call(api,'account.login.start',{'account_id':None,'platform':platform},'start')
        assert started['ok'];api._login_runtime.thread.join(2)
        status=call(api,'account.login.status',{'id':started['data']['id']},'status')
        assert status['data']['state']=='completed'
        saved=AccountService(db_engine).get(status['data']['account_id'])
        assert saved.platform==platform and saved.nickname=='Fixture '+platform
        assert saved.last_login_at and saved.avatar_data=='data:image/jpeg;base64,/9j/'
        assert saved.capabilities.ordinary=='pending' and saved.session_ref
        assert len(AccountService(db_engine).list())==1
    finally:api.close(1)
