"""Controlled creator login fixtures; these do not claim a live B站 login."""
import importlib
import json
import subprocess
from types import SimpleNamespace

import pytest


def auth_module():
    return importlib.import_module('yijianjifa.webui.bilibili_auth')


def nav(mid=123, **changes):
    return dict(dict(isLogin=True, mid=mid, uname='Fixture B站',
                     face='https://i0.hdslb.com/bfs/face/fixture.jpg'), **changes)


def observation(uid='123', **changes):
    return dict(dict(origin='https://member.bilibili.com', logged_in=True,
                     uid=uid, profile_uid=uid, nickname='Fixture B站'), **changes)


def evaluate_identity(*, info=None, header=None, origin='https://member.bilibili.com',
                      avatar='https://i0.hdslb.com/bfs/face/fixture.jpg@112w_112h.webp',
                      connected=True, mounted=True, info_missing=False, extra_header=None):
    # Real script execution catches missing identity extraction and accidental
    # cookie-only success. No request/fetch API exists in this fixture.
    program = r'''
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
global.location = {origin:data.origin, href:data.origin+'/platform/home'};
const element = {isConnected:data.connected, querySelector:selector => {
 if (selector !== 'a.avatar img') throw Error('unexpected avatar selector');
 return data.avatar ? {currentSrc:data.avatar,src:data.avatar} : null;
}};
const header = data.header ? {navData:data.header,$el:element,$children:[]} : null;
const root = {$store:{state:{upInfo:data.info}},$children:header?[header]:[]};
if(data.info_missing) delete root.$store.state.upInfo;
if(data.extra_header) root.$children.push({navData:data.extra_header,$el:element,$children:[]});
global.document = {querySelector:selector => {
 if(selector === '#creativecenter-v3') return null;
 if(selector === '#app') return data.mounted ? {__vue__:root} : null;
 throw Error('unexpected root selector');
}};
const value = eval('('+data.script+')')();
process.stdout.write(JSON.stringify(value));
'''
    data = dict(script=auth_module().IDENTITY_SCRIPT, origin=origin,
                info=nav() if info is None else info,
                header=nav() if header is None else header,
                avatar=avatar, connected=connected, mounted=mounted,
                info_missing=info_missing,extra_header=extra_header)
    result = subprocess.run(['node', '-e', program], input=json.dumps(data),
                            text=True, encoding="utf-8", capture_output=True, check=True)
    return json.loads(result.stdout)


def test_official_page_identity_includes_uid_and_loaded_account_avatar():
    value = evaluate_identity()
    assert value == dict(origin='https://member.bilibili.com', logged_in=True,
                         uid='123', profile_uid='123', nickname='Fixture B站',
                         avatar_url='https://i0.hdslb.com/bfs/face/fixture.jpg@112w_112h.webp')
    assert auth_module().verified_identity(value, '123') == ('123', 'Fixture B站')


def test_page_without_mounted_application_is_not_login_success():
    assert evaluate_identity(mounted=False) is None


def test_connected_official_nav_header_does_not_require_store_initialization():
    value = evaluate_identity(info_missing=True)
    assert auth_module().verified_identity(value, '123') == ('123', 'Fixture B站')
    assert auth_module().verified_identity(evaluate_identity(info={}), '123') == ('123', 'Fixture B站')
    assert evaluate_identity(info_missing=True, connected=False) is None
    assert evaluate_identity(info={}, connected=False) is None


@pytest.mark.parametrize('header', [nav(mid=456), nav(uname='Other connected account'),
                                  nav(isLogin=False)])
def test_conflicting_connected_nav_headers_are_not_identity_evidence(header):
    assert evaluate_identity(info_missing=True, extra_header=header) is None


@pytest.mark.parametrize('change', [
    dict(origin='https://passport.bilibili.com'),
    dict(origin='https://member.bilibili.com.evil.example'),
    dict(info={'isLogin':False,'mid':123,'uname':'Fixture B站'}),
    dict(info={'mid':123,'uname':'Fixture B站'}),
    dict(info={'isLogin':True,'mid':0,'uname':'Fixture B站'}),
    dict(info={'isLogin':True,'mid':9007199254740992,'uname':'Fixture B站'}),
    dict(header={'isLogin':True,'mid':456,'uname':'Fixture B站'}),
    dict(header={'isLogin':True,'mid':123,'uname':'Different name'}),
    dict(connected=False),
])
def test_page_requires_logged_in_matching_connected_header(change):
    assert evaluate_identity(**change) is None


@pytest.mark.parametrize('avatar', [
    'https://i0.hdslb.com/bfs/face/recommendation.jpg',
    'https://evil.example/bfs/face/fixture.jpg',
    'https://i0.hdslb.com/bfs/face/fixture.jpg.evil',
    'data:image/jpeg;base64,untrusted',
])
def test_unrelated_loaded_image_cannot_become_account_avatar(avatar):
    assert evaluate_identity(avatar=avatar)['avatar_url'] == ''


def test_identity_verifier_rejects_incomplete_or_wrong_account():
    auth=auth_module()
    assert auth.verified_identity(observation(nickname=' Fixture B站 ')) == ('123','Fixture B站')
    for field,value in [('uid',123),('uid','0'),('uid','display-handle'),
                        ('logged_in',1),('profile_uid','456'),('nickname',''),
                        ('origin','http://member.bilibili.com')]:
        invalid=observation();invalid[field]=value
        with pytest.raises(ValueError):auth.verified_identity(invalid)
    with pytest.raises(auth.AccountIdentityMismatch):
        auth.verified_identity(observation(), '456')


def session_state():
    return dict(cookies=[dict(name='fixture',value='private-fixture',domain='.bilibili.com',
                             path='/',secure=True,httpOnly=True,expires=-1,sameSite='Lax')],
                origins=[dict(origin='https://member.bilibili.com',
                              localStorage=[dict(name='fixture',value='private-state')])])


def test_bilibili_archive_excludes_other_platforms_and_binds_account_identity():
    from yijianjifa.webui.creator_auth import encode_session,decode_session
    auth=auth_module();state=session_state()
    state['cookies'].append(dict(name='foreign',value='unused',domain='.kuaishou.com',path='/'))
    state['origins'].append(dict(origin='https://channels.weixin.qq.com',localStorage=[]))
    blob=encode_session(auth.SPEC,state,'123')
    assert decode_session(auth.SPEC,blob,'123') == session_state()
    with pytest.raises(ValueError):decode_session(auth.SPEC,blob,'456')
    changed=json.loads(blob);changed['platform']='kuaishou'
    with pytest.raises(ValueError):decode_session(auth.SPEC,json.dumps(changed).encode(),'123')
    changed=json.loads(blob);changed['state']['cookies'][0]['domain']='bilibili.com.evil.example'
    with pytest.raises(ValueError):decode_session(auth.SPEC,json.dumps(changed).encode(),'123')


def test_account_switch_during_capture_does_not_return_session():
    from yijianjifa.webui.creator_auth import observe_session
    auth=auth_module();values=iter([observation(),observation('456')])
    page=SimpleNamespace(url=auth.HOME,is_closed=lambda:False,
                         evaluate=lambda script,origin=None:next(values) if origin is None else session_state()['origins'][0])
    with pytest.raises(auth.AccountIdentityMismatch):
        observe_session(auth.SPEC,page,SimpleNamespace(cookies=lambda:session_state()['cookies'],pages=[page]))


def test_bilibili_login_result_saves_avatar_session_and_time_without_enabling_publish(db_engine,tmp_path):
    from yijianjifa.webui.creator_auth import observe_session,decode_session
    from yijianjifa.features.accounts.login_flow import LoginFlow
    from yijianjifa.features.accounts.session_store import SessionStore
    from yijianjifa.features.accounts.service import AccountService
    auth=auth_module();identity=evaluate_identity()
    page=SimpleNamespace(url=auth.HOME,is_closed=lambda:False,
                         evaluate=lambda script,origin=None:identity if origin is None else session_state()['origins'][0])
    avatar='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB'
    result=observe_session(auth.SPEC,page,SimpleNamespace(cookies=lambda:session_state()['cookies'],pages=[page]),
                           avatar_reader=lambda url:avatar)
    store=SessionStore(tmp_path/'profiles');flow=LoginFlow(db_engine,store)
    first=flow.begin(platform='bilibili');completed=flow.complete(first['id'],**result)
    service=AccountService(db_engine);account=service.get(completed['account_id'])
    assert account.platform == 'bilibili' and account.platform_user_id == '123'
    assert account.nickname == 'Fixture B站' and account.avatar_data == avatar
    assert account.last_login_at is not None and account.login_state == 'valid'
    assert account.capabilities.ordinary == 'pending'
    assert decode_session(auth.SPEC,store.get(account.id,ref=account.session_ref),'123') == session_state()
    again=flow.begin(account.id);flow.complete(again['id'],**result)
    assert len(service.list('bilibili')) == 1
