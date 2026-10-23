"""Fixture-only creator login and session boundaries; no live account evidence."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from yijianjifa.webui import kuaishou_auth as auth
from yijianjifa.features.accounts.session_store import SessionStore


def identity(uid='123'):
    return dict(origin='https://cp.kuaishou.com', logged_in=True, uid=uid,
                profile_uid=uid, nickname='Fixture account')


def state():
    return dict(cookies=[dict(name='fixture', value='private-fixture', domain='.kuaishou.com',
                             path='/', secure=True, httpOnly=True, expires=-1, sameSite='Lax')],
                origins=[dict(origin='https://cp.kuaishou.com', localStorage=[dict(name='fixture', value='private-state')])])


def test_identity_needs_official_origin_stable_uid_and_logged_in_state():
    assert auth.verified_identity(identity(), '123') == ('123', 'Fixture account')
    for change in [dict(uid=123), dict(uid='0'), dict(uid='display-handle'), dict(logged_in=1),
                   dict(profile_uid='456'), dict(origin='https://passport.kuaishou.com'), dict(nickname='')]:
        with pytest.raises(ValueError): auth.verified_identity({**identity(), **change}, '123')
    with pytest.raises(auth.AccountIdentityMismatch): auth.verified_identity(identity(), '456')


def test_session_capture_rechecks_identity_after_storage():
    class Page:
        def __init__(self, values): self.values=iter(values)
        def evaluate(self, script):
            assert script == auth.IDENTITY_SCRIPT
            return next(self.values)
    context=SimpleNamespace(storage_state=state)
    result=auth.observe_session(Page([identity(), identity()]), context)
    assert result['platform_user_id']=='123' and 'ordinary_available' not in result
    assert set(result)=={'platform_user_id','nickname','session_blob'}
    with pytest.raises(auth.AccountIdentityMismatch): auth.observe_session(Page([identity(), identity('456')]), context)
    with pytest.raises(auth.AccountIdentityMismatch): auth.observe_session(Page([identity('456')]), context, '123')


def test_scoped_session_roundtrip_and_other_platform_rejection():
    data=state()
    data['cookies'].append(dict(name='foreign',value='unrelated',domain='.douyin.com',path='/'))
    data['origins'].append(dict(origin='https://other.example',localStorage=[]))
    blob=auth.encode_session(data,'123')
    assert auth.decode_session(blob,'123')==state()
    with pytest.raises(ValueError): auth.decode_session(blob,'456')
    for change in [dict(platform='douyin'),dict(identity='456'),dict(version=True)]:
        payload=json.loads(blob);payload.update(change)
        with pytest.raises(ValueError): auth.decode_session(json.dumps(payload).encode(),'123')
    payload=json.loads(blob);payload['state']['cookies'][0]['domain']='kuaishou.com.evil.example'
    with pytest.raises(ValueError): auth.decode_session(json.dumps(payload).encode(),'123')
    payload=json.loads(blob);payload['state']['origins'][0]['origin']='http://cp.kuaishou.com'
    with pytest.raises(ValueError): auth.decode_session(json.dumps(payload).encode(),'123')
    with pytest.raises(ValueError): auth.decode_session(b'{"version":1,"version":1}', '123')


def test_kuaishou_profile_does_not_overlap_douyin_profile(tmp_path):
    from yijianjifa.webui.chromium_publish import chromium_profile as douyin_profile
    store=SessionStore(tmp_path/'profiles')
    ks=auth.chromium_profile(store,'123');dy=douyin_profile(store,'123')
    assert ks != dy and ks.exists() and dy.exists()
    assert auth.chromium_profile(store,'123') == ks


def test_restore_does_not_overwrite_rotated_live_cookie(tmp_path):
    store=SessionStore(tmp_path/'profiles');profile=auth.chromium_profile(store,'123')
    ref=store.put('account',auth.encode_session(state(),'123'),versioned=True)
    account=SimpleNamespace(id='account',platform_user_id='123',session_ref=ref)
    class Context:
        def __init__(self): self.rows=[];self.scripts=[]
        def cookies(self): return [{**state()['cookies'][0],'value':'rotated'}]
        def add_cookies(self,rows):self.rows.extend(rows)
        def add_init_script(self,script):self.scripts.append(script)
    first=Context();auth.restore_session(first,store,account,profile)
    assert len(first.rows)==1 and 'overwrite = true' in first.scripts[0]
    second=Context();auth.restore_session(second,store,account,profile)
    assert second.rows==[] and 'overwrite = false' in second.scripts[0]
    assert 'location.origin' in second.scripts[0]


def test_restore_ref_for_another_account_rejected(tmp_path):
    store=SessionStore(tmp_path/'profiles');profile=auth.chromium_profile(store,'123')
    ref=store.put('other',auth.encode_session(state(),'123'),versioned=True)
    with pytest.raises(ValueError):
        auth.restore_session(object(),store,SimpleNamespace(id='account',platform_user_id='123',session_ref=ref),profile)


@pytest.fixture
def creator_browser(monkeypatch):
    """Only a lifecycle substitute; never launch or contact a real browser."""
    import playwright.sync_api
    class Page:
        def __init__(self):
            self.url='about:blank';self.closed=False;self.observation=identity()
            self.visits=[];self.evaluations=0;self.waits=[]
            self.main_frame=object();self.listeners={}
        def on(self,event,callback):self.listeners[event]=callback
        def goto(self,url,**options):
            self.url=url;self.visits.append((url,options))
        def is_closed(self):return self.closed
        def evaluate(self,script):
            assert script==auth.IDENTITY_SCRIPT
            self.evaluations+=1
            return self.observation
        def get_by_role(self,role,**kwargs):
            assert role=='link' and kwargs=={'name':'立即登录','exact':True}
            return SimpleNamespace(count=lambda:0)
        def wait_for_timeout(self,delay):
            # Simulate the user closing an invalid/incomplete login, without
            # sleeping or keeping the fixture's provider loop alive.
            self.waits.append(delay);self.closed=True
    class Context:
        def __init__(self):
            self.page=Page();self.pages=[self.page];self.closed=0
            self.rows=[];self.scripts=[];self.timeouts=[];self.on_close=lambda:None
        def set_default_timeout(self,timeout):self.timeouts.append(timeout)
        def cookies(self):return []
        def add_cookies(self,rows):self.rows.extend(rows)
        def add_init_script(self,script):self.scripts.append(script)
        def storage_state(self):return state()
        def close(self):
            self.closed+=1;self.on_close();self.page.closed=True
    context=Context();launches=[];profiles=[]
    def launch(profile,**options):
        path=Path(profile)
        assert path.exists()
        profiles.append(path);launches.append(options)
        return context
    class Playwright:
        def __enter__(self):return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        def __exit__(self,*args):return False
    monkeypatch.setattr(playwright.sync_api,'sync_playwright',Playwright)
    return SimpleNamespace(context=context,page=context.page,launches=launches,profiles=profiles)


def test_provider_finishes_and_removes_temporary_profile_without_saving_account(db_engine,app_paths,creator_browser):
    from yijianjifa.features.accounts.service import AccountService
    provider=auth.make_kuaishou_provider(db_engine,app_paths)
    result=provider(None,threading.Event())
    assert result['platform_user_id']=='123'
    assert auth.decode_session(result['session_blob'],'123')==state()
    assert 'ordinary_available' not in result
    assert creator_browser.launches[0]['headless'] is False
    assert creator_browser.context.closed==1 and creator_browser.page.evaluations==2
    assert creator_browser.page.visits[0][0]==auth.HOME
    assert not creator_browser.profiles[0].exists()
    assert list((app_paths.data_dir/'profiles').iterdir())==[]
    assert AccountService(db_engine).list()==[]


def test_bundled_browser_path_is_configured_before_driver_captures_environment(db_engine,app_paths,creator_browser,monkeypatch):
    import os
    import playwright.sync_api
    import yijianjifa.webui.chromium_publish as chromium
    entered=[];original=playwright.sync_api.sync_playwright
    def options(mode):
        monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH','fixture-bundled-browser')
        return dict(headless=False)
    def driver():
        entered.append(os.environ.get('PLAYWRIGHT_BROWSERS_PATH'))
        assert entered[-1]=='fixture-bundled-browser'
        return original()
    monkeypatch.setattr(chromium,'browser_options',options)
    monkeypatch.setattr(playwright.sync_api,'sync_playwright',driver)
    result=auth.make_kuaishou_provider(db_engine,app_paths)(None,threading.Event())
    assert result['platform_user_id']=='123' and len(entered)==1


def test_douyin_host_prepares_runtime_before_any_shared_publish_driver(db_engine,app_paths,monkeypatch):
    import yijianjifa.webui.chromium_publish as chromium
    calls=[]
    monkeypatch.setattr(chromium,'browser_options',lambda mode:calls.append(mode) or dict(headless=False))
    assert callable(chromium.make_chromium_login(db_engine,app_paths))
    assert calls==['visible']


def test_frozen_host_sets_actual_bundle_browser_path_before_login(db_engine,app_paths,tmp_path,monkeypatch):
    import os
    import sys
    from yijianjifa.webui.chromium_publish import make_chromium_login
    bundle=tmp_path/'frozen-frameworks'
    monkeypatch.setattr(sys,'_MEIPASS',str(bundle),raising=False)
    previous=os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    monkeypatch.delenv('PLAYWRIGHT_BROWSERS_PATH',raising=False)
    try:
        assert 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ
        assert callable(make_chromium_login(db_engine,app_paths))
        assert os.environ['PLAYWRIGHT_BROWSERS_PATH']==str((bundle/'playwright-browsers').resolve())
    finally:
        # Production sets this directly. An initially absent key is not tracked
        # by monkeypatch.delenv, so restore it explicitly for later browsers.
        if previous is None:os.environ.pop('PLAYWRIGHT_BROWSERS_PATH',None)
        else:os.environ['PLAYWRIGHT_BROWSERS_PATH']=previous


def test_cancel_during_context_close_drops_observation_and_cleans_temp_profile(db_engine,app_paths,creator_browser):
    cancel=threading.Event();creator_browser.context.on_close=cancel.set
    provider=auth.make_kuaishou_provider(db_engine,app_paths)
    assert provider(None,cancel) is None
    assert creator_browser.page.evaluations==2 and creator_browser.context.closed==1
    assert not creator_browser.profiles[0].exists()
    assert list((app_paths.data_dir/'profiles').iterdir())==[]


def test_closed_login_window_never_becomes_completed_identity(db_engine,app_paths,creator_browser):
    creator_browser.page.closed=True
    assert auth.make_kuaishou_provider(db_engine,app_paths)(None,threading.Event()) is None
    assert creator_browser.page.evaluations==0 and creator_browser.context.closed==1
    assert not creator_browser.profiles[0].exists()


def test_provider_rejects_wrong_platform_before_launch(db_engine,app_paths,creator_browser):
    from yijianjifa.features.accounts.service import AccountService
    account=AccountService(db_engine).create('douyin','123','Other platform')
    with pytest.raises(ValueError,match='platform mismatch'):
        auth.make_kuaishou_provider(db_engine,app_paths)(account.id,threading.Event())
    assert creator_browser.launches==[] and creator_browser.profiles==[]


def test_existing_provider_restores_only_accepted_account_session_before_observation(db_engine,app_paths,creator_browser):
    from yijianjifa.features.accounts.service import AccountService
    service=AccountService(db_engine);account=service.create('kuaishou','123','Fixture account')
    store=SessionStore(app_paths.data_dir/'profiles')
    ref=service.attach_session(account.id,store,auth.encode_session(state(),'123'))
    result=auth.make_kuaishou_provider(db_engine,app_paths)(account.id,threading.Event())
    assert result['platform_user_id']=='123' and creator_browser.context.rows==state()['cookies']
    assert len(creator_browser.context.scripts)==1
    assert 'https://cp.kuaishou.com' in creator_browser.context.scripts[0]
    assert 'overwrite = true' in creator_browser.context.scripts[0]
    assert creator_browser.profiles[0]==auth.chromium_profile(store,'123')
    assert creator_browser.profiles[0].exists()
    assert (creator_browser.profiles[0]/'.source-ref').read_text()==ref
    assert service.get(account.id).session_ref==ref
    assert creator_browser.context.closed==1


def test_wrong_account_observation_cannot_replace_existing_session(db_engine,app_paths,creator_browser):
    from yijianjifa.features.accounts.service import AccountService
    service=AccountService(db_engine);account=service.create('kuaishou','123','Fixture account')
    store=SessionStore(app_paths.data_dir/'profiles');original=auth.encode_session(state(),'123')
    ref=service.attach_session(account.id,store,original)
    creator_browser.page.observation=identity('456')
    with pytest.raises(auth.AccountIdentityMismatch):
        auth.make_kuaishou_provider(db_engine,app_paths)(account.id,threading.Event())
    saved=service.get(account.id)
    assert saved.platform_user_id=='123' and saved.session_ref==ref
    assert store.get(saved.id,ref=ref)==original
    assert creator_browser.context.closed==1


def backend_account(db_engine, app_paths):
    from yijianjifa.features.accounts.service import AccountService
    service=AccountService(db_engine)
    account=service.create('kuaishou','123','Fixture account')
    store=SessionStore(app_paths.data_dir/'profiles')
    ref=service.attach_session(account.id,store,auth.encode_session(state(),'123'))
    return service, account, store, ref


def test_backend_reuses_saved_profile_and_stays_open_until_user_close(db_engine,app_paths,creator_browser,monkeypatch):
    service,account,store,ref=backend_account(db_engine,app_paths)
    clock=[0];monkeypatch.setattr(auth.time,'monotonic',lambda:clock[0])
    updated=state();updated['cookies'][0]['value']='latest-session'
    def wait(delay):
        creator_browser.page.waits.append(delay)
        assert creator_browser.context.closed==0
        if len(creator_browser.page.waits)==1:
            # The initial authentication deadline must not close a backend
            # window that has already confirmed the saved account.
            clock[0]=1000
            creator_browser.page.observation={**identity(),'nickname':'Updated name'}
            creator_browser.context.storage_state=lambda:updated
        else:creator_browser.page.closed=True
    creator_browser.page.wait_for_timeout=wait
    provider=auth.make_kuaishou_provider(db_engine,app_paths)
    result=provider.open_backend(account.id,threading.Event())
    assert result['platform_user_id']=='123' and result['nickname']=='Updated name'
    assert auth.decode_session(result['session_blob'],'123')==updated
    assert 'ordinary_available' not in result
    assert creator_browser.page.waits==[750,750] and creator_browser.page.evaluations==4
    assert creator_browser.context.closed==1
    assert creator_browser.launches[0]['headless'] is False
    assert creator_browser.context.rows==state()['cookies']
    assert creator_browser.profiles==[auth.chromium_profile(store,'123')]
    assert creator_browser.profiles[0].exists()
    assert service.get(account.id).session_ref==ref
    assert len(service.list())==1


def test_backend_requires_existing_kuaishou_account_before_launch(db_engine,app_paths,creator_browser):
    from yijianjifa.features.accounts.service import AccountService
    provider=auth.make_kuaishou_provider(db_engine,app_paths)
    with pytest.raises(ValueError,match='existing account'):
        provider.open_backend(None,threading.Event())
    other=AccountService(db_engine).create('douyin','123','Other platform')
    with pytest.raises(ValueError,match='platform mismatch'):
        provider.open_backend(other.id,threading.Event())
    assert creator_browser.launches==[] and creator_browser.profiles==[]


def test_backend_cancel_after_identity_does_not_save_new_session(db_engine,app_paths,creator_browser):
    service,account,store,ref=backend_account(db_engine,app_paths)
    original=store.get(account.id,ref=ref);cancel=threading.Event()
    def wait(delay):
        creator_browser.page.waits.append(delay);cancel.set()
    creator_browser.page.wait_for_timeout=wait
    provider=auth.make_kuaishou_provider(db_engine,app_paths)
    assert provider.open_backend(account.id,cancel) is None
    assert creator_browser.page.evaluations==2 and creator_browser.page.waits==[750]
    assert creator_browser.context.closed==1
    assert service.get(account.id).session_ref==ref and store.get(account.id,ref=ref)==original


def test_backend_identity_change_is_rejected_even_after_first_confirmation(db_engine,app_paths,creator_browser):
    from yijianjifa.webui.chromium_publish import account_lock
    service,account,store,ref=backend_account(db_engine,app_paths)
    original=store.get(account.id,ref=ref)
    def wait(delay):
        creator_browser.page.waits.append(delay)
        creator_browser.page.observation=identity('456')
    creator_browser.page.wait_for_timeout=wait
    with pytest.raises(auth.AccountIdentityMismatch):
        auth.make_kuaishou_provider(db_engine,app_paths).open_backend(account.id,threading.Event())
    assert creator_browser.context.closed==1 and creator_browser.page.evaluations==3
    assert service.get(account.id).session_ref==ref and store.get(account.id,ref=ref)==original
    lock=account_lock(account.id)
    assert lock.acquire(blocking=False)
    lock.release()


def test_backend_respects_account_lock_and_never_opens_busy_profile(db_engine,app_paths,creator_browser):
    from yijianjifa.webui.chromium_publish import account_lock
    _,account,_,_=backend_account(db_engine,app_paths)
    lock=account_lock(account.id);assert lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError,match='account busy'):
            auth.make_kuaishou_provider(db_engine,app_paths).open_backend(account.id,threading.Event())
        assert creator_browser.launches==[] and creator_browser.profiles==[]
    finally:lock.release()


def test_backend_closed_without_identity_returns_no_account_update(db_engine,app_paths,creator_browser):
    service,account,store,ref=backend_account(db_engine,app_paths)
    original=store.get(account.id,ref=ref)
    creator_browser.page.observation={**identity(),'logged_in':False}
    assert auth.make_kuaishou_provider(db_engine,app_paths).open_backend(account.id,threading.Event()) is None
    assert creator_browser.page.waits==[750] and creator_browser.context.closed==1
    assert service.get(account.id).session_ref==ref and store.get(account.id,ref=ref)==original


def test_backend_can_return_last_confirmed_identity_when_browser_was_already_closed(db_engine,app_paths,creator_browser):
    _,account,_,_=backend_account(db_engine,app_paths)
    def close():
        creator_browser.context.closed+=1
        assert creator_browser.page.closed
        raise RuntimeError('fixture browser already closed')
    creator_browser.context.close=close
    result=auth.make_kuaishou_provider(db_engine,app_paths).open_backend(account.id,threading.Event())
    assert result['platform_user_id']=='123' and creator_browser.context.closed==1


def test_backend_runtime_saves_latest_verified_session_only_after_window_close(db_engine,app_paths,creator_browser):
    from yijianjifa.app_services.login_runtime import LoginRuntime
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    service,account,store,ref=backend_account(db_engine,app_paths)
    capabilities=CapabilitySnapshot(ordinary='not_found',commerce='not_found',location_scope='national')
    service.update_capabilities(account.id,capabilities)
    confirmed=threading.Event();release=threading.Event()
    def wait(delay):
        creator_browser.page.waits.append(delay);confirmed.set()
        assert release.wait(2)
        creator_browser.page.closed=True
    creator_browser.page.wait_for_timeout=wait
    runtime=LoginRuntime(db_engine,app_paths,providers={'kuaishou':auth.make_kuaishou_provider(db_engine,app_paths)})
    try:
        job=runtime.start('backend-fixture',account.id,backend=True)
        assert confirmed.wait(1)
        assert runtime.status(job['id'])['state']=='waiting'
        assert service.get(account.id).session_ref==ref
        release.set();runtime.thread.join(2)
        assert not runtime.thread.is_alive()
        assert runtime.status(job['id'])['state']=='completed'
        saved=service.get(account.id)
        assert saved.id==account.id and saved.platform=='kuaishou'
        assert saved.session_ref!=ref and saved.capabilities==capabilities
        assert auth.decode_session(store.get(saved.id,ref=saved.session_ref),'123')==state()
        assert len(service.list())==1
    finally:release.set();runtime.close(2)
