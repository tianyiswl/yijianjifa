"""Account avatar regression checks use local images and synthetic page state."""
import base64
import json
import subprocess
from types import SimpleNamespace
import pytest
from yijianjifa.webui import kuaishou_auth as auth

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1kAAAAASUVORK5CYII=')
AVATAR = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
URL = 'https://images.example.test/account.png?version=1'


def test_verified_page_script_returns_the_current_account_avatar():
    script = '''const fs=require('node:fs');const input=JSON.parse(fs.readFileSync(0,'utf8'));
    const user={userId:'123',userName:'Fixture account',logined:true,userAvatar:input.url};
    const root={$store:{state:{user}},$options:{},$children:[],userLogined:true,userName:user.userName,$el:{isConnected:true}};
    global.location={origin:'https://cp.kuaishou.com'};global.document={querySelector:()=>({__vue__:root})};
    const read=eval('('+input.script+')');console.log(JSON.stringify(read()));'''
    result=subprocess.run(['node','-e',script],input=json.dumps({'url':URL,'script':auth.IDENTITY_SCRIPT}),text=True,capture_output=True,check=True)
    observed=json.loads(result.stdout)
    assert observed.get('avatar_url')==URL


def request(frame, url=URL, body=PNG, *, resource='image', status=200, length=None, response_url=None):
    response=SimpleNamespace(url=response_url or url,status=status,headers={'content-length':str(len(body) if length is None else length)},body=lambda:body)
    return SimpleNamespace(frame=frame,resource_type=resource,response=lambda:response)


def test_avatar_cache_reads_only_the_verified_account_image():
    frame=object();cache=auth.LoadedAvatarCache(frame)
    cache.capture(request(frame,url='https://images.example.test/recommendation.png',body=b'not the account'))
    cache.capture(request(frame))
    assert cache.data_for(URL)==AVATAR
    assert cache.data_for('https://images.example.test/account.png?version=2') is None
    assert auth.LoadedAvatarCache(frame).data_for(URL) is None


@pytest.mark.parametrize('change',[
    {'resource':'fetch'},{'status':404},{'frame':object()},
    {'response_url':'https://images.example.test/redirect.png'},
    {'length':512*1024+1},{'body':b'<html>not an image</html>'},
])
def test_unrelated_or_invalid_response_never_becomes_an_avatar(change):
    frame=object();cache=auth.LoadedAvatarCache(frame)
    changed_frame=change.get('frame',frame)
    cache.capture(request(changed_frame,**{k:v for k,v in change.items() if k!='frame'}))
    assert not cache.data_for(URL)


def test_avatar_capture_is_passed_through_with_identity_rechecked():
    original=dict(origin='https://cp.kuaishou.com',logged_in=True,uid='123',profile_uid='123',nickname='Fixture account',avatar_url=URL)
    class Page:
        def __init__(self,second):self.values=iter([original,second])
        def evaluate(self,script):return next(self.values)
    state=dict(cookies=[dict(name='fixture',value='fixture',domain='.kuaishou.com',path='/')],origins=[])
    result=auth.observe_session(Page(original),SimpleNamespace(storage_state=lambda:state),avatar_reader=lambda url:AVATAR if url==URL else '')
    assert result['avatar_data']==AVATAR
    assert 'avatar_url' not in result
    assert 'avatar_data' not in auth.observe_session(Page(original),SimpleNamespace(storage_state=lambda:state),avatar_reader=lambda url:'')
    with pytest.raises(auth.AccountIdentityMismatch):
        auth.observe_session(Page({**original,'uid':'456','profile_uid':'456'}),SimpleNamespace(storage_state=lambda:state),avatar_reader=lambda url:AVATAR)


def test_kuaishou_relogin_updates_avatar_without_creating_another_account(db_engine,tmp_path):
    from yijianjifa.features.accounts.login_flow import LoginFlow
    from yijianjifa.features.accounts.session_store import SessionStore
    from yijianjifa.features.accounts.service import AccountService
    flow=LoginFlow(db_engine,SessionStore(tmp_path/'profiles'));first=flow.begin(platform='kuaishou')
    saved=flow.complete(first['id'],platform_user_id='123',nickname='Fixture',session_blob=b'fixture')
    again=flow.begin(saved['account_id'])
    flow.complete(again['id'],platform_user_id='123',nickname='Fixture',session_blob=b'fixture',avatar_data=AVATAR)
    account=AccountService(db_engine).get(saved['account_id'])
    assert account.avatar_data==AVATAR and account.capabilities.ordinary=='pending'
    third=flow.begin(saved['account_id'])
    flow.complete(third['id'],platform_user_id='123',nickname='Fixture',session_blob=b'fixture',avatar_data='')
    assert AccountService(db_engine).get(saved['account_id']).avatar_data==AVATAR
    assert len(AccountService(db_engine).list())==1


def test_avatar_changed_during_capture_is_not_saved_as_the_new_profile():
    before=dict(origin='https://cp.kuaishou.com',logged_in=True,uid='123',profile_uid='123',nickname='Fixture account',avatar_url=URL)
    after={**before,'avatar_url':'https://images.example.test/new-account-image.png'}
    values=iter([before,after])
    page=SimpleNamespace(evaluate=lambda script:next(values))
    state=dict(cookies=[dict(name='fixture',value='fixture',domain='.kuaishou.com',path='/')],origins=[])
    result=auth.observe_session(page,SimpleNamespace(storage_state=lambda:state),avatar_reader=lambda url:AVATAR)
    assert 'avatar_data' not in result
    assert result['platform_user_id']=='123'
