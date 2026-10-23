"""Controlled video-channel login fixtures; these do not prove a live login."""
import base64
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from yijianjifa.features.accounts.service import AccountIdentityMismatch
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.webui import channels_auth as auth


UID = 'fixture_finder_001'
AVATAR_URL = 'https://images.example.test/channels-account.jpg'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1kAAAAASUVORK5CYII=')
AVATAR = 'data:image/png;base64,' + base64.b64encode(PNG).decode()


def identity(uid=UID):
    return dict(origin='https://channels.weixin.qq.com', logged_in=True,
                expired=False, uid=uid, profile_uid=uid,
                nickname='Fixture video channel', avatar_url=AVATAR_URL)


def state():
    return dict(cookies=[dict(name='fixture', value='private-fixture',
                             domain='channels.weixin.qq.com', path='/',
                             secure=True, httpOnly=True, expires=-1, sameSite='Lax')],
                origins=[dict(origin='https://channels.weixin.qq.com',
                              localStorage=[dict(name='fixture', value='private-state')])])


def run_identity_script(**change):
    fixture = r'''const fs=require('node:fs');const p=JSON.parse(fs.readFileSync(0,'utf8'));
    const finder={finderUsername:p.uid??'fixture_finder_001',nickname:'Fixture video channel',headImgUrl:p.avatar??'https://images.example.test/channels-account.jpg'};
    const headerEl={isConnected:p.connected!==false};
    const header={finder:p.mismatch?{...finder,finderUsername:'other_finder'}:finder,$el:headerEl,$options:{},$children:[]};
    const root={$children:[header],$options:{},$el:{isConnected:true}};
    const footer={isConnected:true,contains:el=>el===headerEl};
    global.window={_store:{userStore:{finder,hasLogin:p.hasLogin??true,isExpired:p.expired??false}}};
    global.location={origin:p.origin??'https://channels.weixin.qq.com',pathname:p.path??'/platform'};
    global.document={querySelector:sel=>sel==='#app'?{__vue__:root}:sel==='.side-bar-footer'&&!p.missingFooter?footer:null};
    const read=eval('('+p.script+')');console.log(JSON.stringify(read()));'''
    result = subprocess.run(['node', '-e', fixture],
                            input=json.dumps(dict(script=auth.IDENTITY_SCRIPT, **change)),
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_script_reads_authenticated_current_finder_identity_and_avatar_only():
    assert run_identity_script() == identity()


@pytest.mark.parametrize('change', [
    dict(origin='https://channels.weixin.qq.com.evil.example'),
    dict(path='/platform/login'), dict(path='/platform/login-for-iframe'),
    dict(path='/login.html'), dict(hasLogin=False), dict(expired=True),
    dict(missingFooter=True), dict(mismatch=True), dict(connected=False),
])
def test_script_rejects_login_page_expiry_and_different_visible_identity(change):
    assert run_identity_script(**change) is None


def test_oversized_avatar_is_ignored_without_losing_verified_login():
    value = run_identity_script(avatar='https://images.example.test/'+'x'*4096)
    assert value['avatar_url'] == ''
    assert value['uid'] == UID and value['logged_in'] is True


def test_identity_requires_official_origin_logged_in_and_consistent_opaque_id():
    assert auth.verified_identity(identity(), UID) == (UID, 'Fixture video channel')
    for change in [dict(uid=123), dict(uid=''), dict(uid='x'*129), dict(uid='id with space'),
                   dict(uid='id\ncontrol'), dict(profile_uid='different'), dict(logged_in=1),
                   dict(expired=0), dict(origin='https://mp.weixin.qq.com'), dict(nickname='')]:
        with pytest.raises(ValueError):
            auth.verified_identity({**identity(), **change}, UID)
    with pytest.raises(AccountIdentityMismatch):
        auth.verified_identity(identity(), 'different')


def test_session_capture_rechecks_identity_and_saves_current_avatar():
    from yijianjifa.webui.creator_auth import observe_session, decode_session
    values = iter([identity(), identity()])
    page = SimpleNamespace(url=auth.HOME,is_closed=lambda:False,
                           evaluate=lambda script,origin=None:next(values) if origin is None else state()['origins'][0])
    result = observe_session(auth.SPEC, page, SimpleNamespace(cookies=lambda:state()['cookies'],pages=[page]),
                             avatar_reader=lambda url: AVATAR if url == AVATAR_URL else '')
    assert result['platform_user_id'] == UID and result['avatar_data'] == AVATAR
    assert decode_session(auth.SPEC, result['session_blob'], UID) == state()
    assert 'ordinary_available' not in result and 'avatar_url' not in result
    values = iter([identity(), identity('different')])
    with pytest.raises(AccountIdentityMismatch):
        observe_session(auth.SPEC, page, SimpleNamespace(cookies=lambda:state()['cookies'],pages=[page]))


def test_session_archive_is_bound_to_platform_and_identity():
    from yijianjifa.webui.creator_auth import encode_session, decode_session
    data = state()
    data['cookies'].append(dict(name='foreign', value='unrelated', domain='.bilibili.com', path='/'))
    data['origins'].append(dict(origin='https://passport.bilibili.com', localStorage=[]))
    blob = encode_session(auth.SPEC, data, UID)
    assert decode_session(auth.SPEC, blob, UID) == state()
    with pytest.raises(ValueError):
        decode_session(auth.SPEC, blob, 'different')
    payload = json.loads(blob)
    payload['platform'] = 'bilibili'
    with pytest.raises(ValueError):
        decode_session(auth.SPEC, json.dumps(payload).encode(), UID)


def test_video_channel_browser_profile_is_separate_from_other_platform(tmp_path):
    from yijianjifa.webui.creator_auth import chromium_profile
    from yijianjifa.webui.chromium_publish import chromium_profile as douyin_profile
    store = SessionStore(tmp_path/'profiles')
    channel = chromium_profile(auth.SPEC, store, UID)
    assert channel == chromium_profile(auth.SPEC, store, UID)
    assert channel != douyin_profile(store, UID) and channel.exists()


@pytest.fixture
def creator_browser(monkeypatch):
    import playwright.sync_api
    class Page:
        def __init__(self):
            self.url='about:blank'; self.closed=False; self.values=[identity(), identity()]
            self.main_frame=object(); self.listeners={}; self.visits=[]
        def on(self, event, callback): self.listeners[event]=callback
        def goto(self, url, **options): self.url=url; self.visits.append(url)
        def is_closed(self): return self.closed
        def evaluate(self, script, origin=None):
            if origin is not None:return state()['origins'][0]
            assert script == auth.IDENTITY_SCRIPT
            return self.values.pop(0)
        def wait_for_timeout(self, delay): self.closed=True
    class Context:
        def __init__(self): self.page=Page(); self.pages=[self.page]; self.closed=0
        def set_default_timeout(self, timeout): pass
        def storage_state(self): raise AssertionError('snapshot must not visit origins')
        def cookies(self): return state()['cookies']
        def close(self): self.closed+=1; self.page.closed=True
    context=Context(); launches=[]; profiles=[]
    def launch(profile, **options):
        profiles.append(Path(profile)); launches.append(options)
        assert profiles[-1].exists()
        return context
    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        def __exit__(self, *args): return False
    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', Playwright)
    return SimpleNamespace(context=context, page=context.page, launches=launches, profiles=profiles)


def test_provider_completes_login_closes_browser_and_removes_temporary_profile(db_engine, app_paths, creator_browser):
    from yijianjifa.features.accounts.service import AccountService
    result=auth.make_channels_provider(db_engine, app_paths)(None, threading.Event())
    assert result['platform_user_id'] == UID and 'ordinary_available' not in result
    assert creator_browser.launches[0]['headless'] is False
    assert creator_browser.page.visits == [auth.HOME]
    assert creator_browser.context.closed == 1 and not creator_browser.profiles[0].exists()
    assert AccountService(db_engine).list() == []


def test_cancelled_login_or_closed_window_cannot_return_saved_identity(db_engine, app_paths, creator_browser):
    cancel=threading.Event(); cancel.set()
    assert auth.make_channels_provider(db_engine, app_paths)(None, cancel) is None
    assert not creator_browser.launches
    creator_browser.page.closed=True
    assert auth.make_channels_provider(db_engine, app_paths)(None, threading.Event()) is None
    assert creator_browser.context.closed == 1


def test_login_flow_records_avatar_and_login_time_without_enabling_publishing(db_engine, tmp_path):
    from yijianjifa.features.accounts.login_flow import LoginFlow
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.webui.creator_auth import encode_session
    flow=LoginFlow(db_engine, SessionStore(tmp_path/'profiles'))
    pending=flow.begin(platform='channels')
    result=flow.complete(pending['id'], platform_user_id=UID, nickname='Fixture video channel',
                         session_blob=encode_session(auth.SPEC, state(), UID), avatar_data=AVATAR)
    account=AccountService(db_engine).get(result['account_id'])
    assert account.platform == 'channels' and account.avatar_data == AVATAR
    assert account.login_state == 'valid' and account.last_login_at is not None
    assert account.capabilities.ordinary == 'pending'
