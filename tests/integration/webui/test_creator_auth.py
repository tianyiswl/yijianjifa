"""Shared session boundaries, independent of live platform acceptance."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from urllib.parse import urlsplit
import pytest
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.features.accounts.service import AccountIdentityMismatch, AccountService


def spec(platform='bilibili'):
    from yijianjifa.webui.creator_auth import CreatorAuthSpec
    host='member.bilibili.com' if platform=='bilibili' else 'channels.weixin.qq.com'
    def verify(value, expected_uid=None):
        if not isinstance(value,dict) or value.get('origin')!='https://'+host or value.get('logged_in') is not True or value.get('uid') not in ('123','456'):
            raise ValueError('identity unavailable')
        if expected_uid and value['uid']!=expected_uid:raise AccountIdentityMismatch('changed')
        return value['uid'],value['nickname']
    return CreatorAuthSpec(platform,'https://'+host,frozenset({host}),frozenset({'https://'+host}), 'fixture-script',verify)


def identity(uid='123',url=''):
    return dict(origin='https://member.bilibili.com',logged_in=True,uid=uid,nickname='Fixture',avatar_url=url)


def state():
    return {'cookies':[{'name':'fixture','value':'synthetic','domain':'member.bilibili.com','path':'/','secure':True}],
            'origins':[{'origin':'https://member.bilibili.com','localStorage':[{'name':'fixture','value':'synthetic'}]}]}


def test_session_is_scoped_and_cannot_be_restored_for_another_platform_or_identity():
    from yijianjifa.webui.creator_auth import encode_session,decode_session
    raw=state();raw['cookies'].append(dict(name='foreign',value='synthetic',domain='.douyin.com',path='/'))
    raw['origins'].append(dict(origin='https://douyin.com',localStorage=[]))
    blob=encode_session(spec(),raw,'123')
    assert json.loads(blob)['state']==state()
    assert decode_session(spec(),blob,'123')==state()
    with pytest.raises(ValueError):decode_session(spec('channels'),blob,'123')
    with pytest.raises(ValueError):decode_session(spec(),blob,'456')
    for domain in ('bilibili.com.evil.example','other.example'):
        archive=json.loads(blob);archive['state']['cookies'][0]['domain']=domain
        with pytest.raises(ValueError):decode_session(spec(),json.dumps(archive).encode(),'123')
    with pytest.raises(ValueError):decode_session(spec(),b'{"version":1,"version":1}', '123')


def test_identity_recheck_stops_cross_account_capture_and_old_avatar():
    from yijianjifa.webui.creator_auth import observe_session
    class Page:
        url='https://member.bilibili.com'
        def __init__(self,values):self.values=iter(values)
        def is_closed(self):return False
        def evaluate(self,script,origin=None):
            if origin is not None:return state()['origins'][0]
            assert script=='fixture-script';return next(self.values)
    context=SimpleNamespace(cookies=lambda:state()['cookies'],pages=[])
    avatar='data:image/jpeg;base64,/9j/'
    result=observe_session(spec(),Page([identity(url='https://images.example/a'),identity(url='https://images.example/a')]),context,avatar_reader=lambda url:avatar)
    assert result['avatar_data']==avatar and result['platform_user_id']=='123'
    assert 'ordinary_available' not in result and 'avatar_url' not in result
    with pytest.raises(AccountIdentityMismatch):observe_session(spec(),Page([identity(),identity('456')]),context)
    changed=observe_session(spec(),Page([identity(url='https://images.example/a'),identity(url='https://images.example/b')]),context,avatar_reader=lambda url:avatar)
    assert 'avatar_data' not in changed


def test_snapshot_reads_only_existing_allowed_documents_and_prioritizes_current_page():
    from yijianjifa.webui.creator_auth import capture_session_state
    configured=replace(spec(),origins=frozenset({'https://member.bilibili.com','https://passport.bilibili.com'}))
    class Page:
        def __init__(self,url,value,closed=False):self.url=url;self.value=value;self.closed=closed;self.reads=[]
        def is_closed(self):return self.closed
        def evaluate(self,script,origin):
            self.reads.append(origin)
            assert 'localStorage.getItem' in script and 'location.origin !== expectedOrigin' in script
            if isinstance(self.value,Exception):raise self.value
            return self.value
    current=Page(configured.home,state()['origins'][0])
    duplicate=Page(configured.home,AssertionError('duplicate origin must not be read'))
    passport=Page('https://passport.bilibili.com/login',dict(origin='https://passport.bilibili.com',localStorage=[dict(name='passport',value='synthetic')]))
    foreign=Page('https://member.bilibili.com.evil.test/',AssertionError('foreign storage must not be read'))
    closed=Page('https://passport.bilibili.com/',None,closed=True)
    context=SimpleNamespace(cookies=lambda:state()['cookies']+[dict(name='foreign',value='synthetic',domain='.other.test',path='/')],pages=[duplicate,foreign,closed,passport,current])
    captured=capture_session_state(configured,context,current)
    assert captured==dict(cookies=state()['cookies'],origins=[state()['origins'][0],passport.value])
    assert current.reads==['https://member.bilibili.com'] and passport.reads==['https://passport.bilibili.com']
    assert duplicate.reads==foreign.reads==closed.reads==[]


@pytest.mark.parametrize('storage_value',[None,RuntimeError('document navigated')])
def test_snapshot_navigation_or_closing_page_does_not_trigger_a_recovery_visit(storage_value):
    from yijianjifa.webui.creator_auth import capture_session_state
    class Page:
        url=spec().home
        def is_closed(self):return False
        def evaluate(self,script,origin):
            if isinstance(storage_value,Exception):raise storage_value
            return storage_value
        def goto(self,*args,**kwargs):raise AssertionError('must not navigate')
        def reload(self,*args,**kwargs):raise AssertionError('must not reload')
    context=SimpleNamespace(cookies=lambda:state()['cookies'],pages=[Page()])
    assert capture_session_state(spec(),context)==dict(cookies=state()['cookies'],origins=[])


def test_snapshot_does_not_accept_changed_origin_or_invalid_storage_values():
    from yijianjifa.webui.creator_auth import capture_session_state
    page=SimpleNamespace(url=spec().home,is_closed=lambda:False)
    context=SimpleNamespace(cookies=lambda:state()['cookies'],pages=[page])
    for value in [dict(origin='https://other.test',localStorage=[]),
                  dict(origin=spec().home,localStorage=[dict(name='token',value=1)])]:
        page.evaluate=lambda script,origin:value
        with pytest.raises(ValueError):capture_session_state(spec(),context)


def test_storage_script_does_not_read_after_origin_changes():
    import subprocess
    from yijianjifa.webui.creator_auth import _LOCAL_STORAGE_SNAPSHOT
    # Execute the actual browser expression against a document that navigated
    # after Python read page.url. Accessing its storage must never occur.
    code='const location={origin:"https://foreign.test"};Object.defineProperty(globalThis,"localStorage",{get(){throw Error("foreign storage read");}});const read='+_LOCAL_STORAGE_SNAPSHOT+';if(read("https://member.bilibili.com")!==null)process.exit(1);'
    subprocess.run(['node','-e',code],check=True,capture_output=True,text=True)


def test_profiles_are_platform_scoped_and_live_cookie_rotation_is_preserved(tmp_path):
    from yijianjifa.webui.creator_auth import chromium_profile,encode_session,restore_session
    store=SessionStore(tmp_path/'profiles');s=spec()
    profile=chromium_profile(s,store,'123')
    assert profile!=chromium_profile(spec('channels'),store,'123')
    ref=store.put('fixture',encode_session(s,state(),'123'),versioned=True)
    account=SimpleNamespace(id='fixture',session_ref=ref,platform_user_id='123')
    class Context:
        def __init__(self):self.rows=[];self.scripts=[]
        def cookies(self):return [{**state()['cookies'][0],'value':'rotated'}]
        def add_cookies(self,rows):self.rows.extend(rows)
        def add_init_script(self,script):self.scripts.append(script)
    first=Context();restore_session(s,first,store,account,profile)
    assert first.rows==state()['cookies']
    second=Context();restore_session(s,second,store,account,profile)
    assert second.rows==[]


@pytest.fixture
def browser(monkeypatch):
    import playwright.sync_api
    class Page:
        def __init__(self):self.main_frame=object();self.url='about:blank';self.closed=False;self.observation=identity();self.visits=[];self.waits=[];self.evaluations=0;self.storage_provider=lambda:state()['origins']
        def on(self,event,callback):assert event=='requestfinished'
        def goto(self,url,**options):self.url=url;self.visits.append(url)
        def reload(self,**options):raise AssertionError('snapshot must not reload')
        def is_closed(self):return self.closed
        def evaluate(self,script,origin=None):
            if origin is not None:return next(row for row in self.storage_provider() if row['origin']==origin)
            assert script=='fixture-script';self.evaluations+=1;return self.observation
        def wait_for_timeout(self,delay):self.waits.append(delay);self.closed=True
    class Context:
        def __init__(self):self.snapshot=state();self.pages=[Page()];self.closed=0;self.on_close=lambda:None;self.rows=[];self.scripts=[];self.state_visits=[];self.pages[0].storage_provider=lambda:self.snapshot['origins']
        def set_default_timeout(self,timeout):pass
        def storage_state(self):
            self.state_visits.append('https://passport.bilibili.com')
            raise AssertionError('storage_state would create and navigate a temporary page')
        def new_page(self):raise AssertionError('snapshot must not create a page')
        def cookies(self):return self.snapshot['cookies']
        def add_cookies(self,rows):self.rows.extend(rows)
        def add_init_script(self,script):self.scripts.append(script)
        def close(self):self.closed+=1;self.pages[0].closed=True;self.on_close()
    context=Context();profiles=[]
    def launch(path,**options):
        assert options['headless'] is False
        assert Path(path).exists();profiles.append(Path(path));return context
    class Playwright:
        def __enter__(self):return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        def __exit__(self,*args):return False
    monkeypatch.setattr(playwright.sync_api,'sync_playwright',Playwright)
    return SimpleNamespace(context=context,page=context.pages[0],profiles=profiles)


def test_new_login_closes_browser_and_discards_temp_profile_before_returning(db_engine,app_paths,browser):
    from yijianjifa.webui.creator_auth import make_creator_provider
    result=make_creator_provider(db_engine,app_paths,spec())(None,threading.Event())
    assert result['nickname']=='Fixture' and browser.context.closed==1
    assert browser.page.visits==['https://member.bilibili.com']
    assert not browser.profiles[0].exists() and AccountService(db_engine).list()==[]


def backend_account(db_engine,app_paths,browser,platform='bilibili'):
    from yijianjifa.webui.creator_auth import encode_session
    configured=spec(platform);host=urlsplit(configured.home).netloc
    current=state();current['cookies'][0]['domain']=host;current['origins'][0]['origin']=configured.home
    browser.page.observation={**identity(), 'origin':configured.home}
    browser.context.snapshot=current
    service=AccountService(db_engine);account=service.create(platform,'123','Saved fixture')
    store=SessionStore(app_paths.data_dir/'profiles')
    ref=service.attach_session(account.id,store,encode_session(configured,current,'123'))
    return configured,service,account,store,ref,current


@pytest.mark.parametrize('platform',['bilibili','channels'])
def test_backend_reuses_uid_profile_stays_open_and_observes_latest_session(db_engine,app_paths,browser,monkeypatch,platform):
    from yijianjifa.webui import creator_auth as auth
    configured,service,account,store,ref,current=backend_account(db_engine,app_paths,browser,platform)
    clock=[0];monkeypatch.setattr(auth.time,'monotonic',lambda:clock[0])
    latest=json.loads(json.dumps(current));latest['cookies'][0]['value']='latest-synthetic'
    def wait(delay):
        browser.page.waits.append(delay)
        assert browser.context.closed==0
        if len(browser.page.waits)==1:
            clock[0]=1000;browser.page.observation['nickname']='Current fixture'
            browser.context.snapshot=latest
        else:browser.page.closed=True
    browser.page.wait_for_timeout=wait
    result=auth.make_creator_provider(db_engine,app_paths,configured).open_backend(account.id,threading.Event())
    assert result['platform_user_id']=='123' and result['nickname']=='Current fixture'
    assert auth.decode_session(configured,result['session_blob'],'123')==latest
    assert browser.page.waits==[750,750] and browser.page.evaluations==4
    assert browser.context.closed==1 and browser.page.visits==[configured.home]
    assert browser.context.state_visits==[]
    assert browser.profiles==[auth.chromium_profile(configured,store,'123')] and browser.profiles[0].exists()
    assert browser.context.rows==current['cookies']
    assert service.get(account.id).session_ref==ref and len(service.list())==1
    assert 'ordinary_available' not in result


def test_backend_polling_does_not_create_temporary_pages_for_previously_visited_origins(db_engine,app_paths,browser):
    from yijianjifa.webui import creator_auth as auth
    configured,service,account,_,ref,current=backend_account(db_engine,app_paths,browser)
    configured=replace(configured,origins=frozenset({configured.home,'https://passport.bilibili.com'}))
    created=[]
    class TemporaryPage:
        def __init__(self):self.visits=[];self.closed=False
        def goto(self,url):self.visits.append(url)
        def close(self):self.closed=True
    def new_page():
        page=TemporaryPage();created.append(page);return page
    def storage_state():
        # Model Playwright's visited-origin snapshot helper: a passport origin
        # no longer open in context.pages causes newPage/goto/close each poll.
        temporary=browser.context.new_page()
        temporary.goto('https://passport.bilibili.com');temporary.close()
        return current
    browser.context.new_page=new_page;browser.context.storage_state=storage_state
    def wait(delay):
        browser.page.waits.append(delay)
        if len(browser.page.waits)==5:browser.page.closed=True
    browser.page.wait_for_timeout=wait
    result=auth.make_creator_provider(db_engine,app_paths,configured).open_backend(account.id,threading.Event())
    assert result['platform_user_id']=='123' and auth.decode_session(configured,result['session_blob'],'123')==current
    assert browser.page.waits==[750]*5 and browser.page.evaluations==10
    assert browser.page.visits==[configured.home] and created==[]
    assert browser.context.closed==1 and service.get(account.id).session_ref==ref


def test_backend_requires_existing_correct_platform_and_free_account_lock(db_engine,app_paths,browser):
    from yijianjifa.webui.creator_auth import make_creator_provider
    from yijianjifa.webui.chromium_publish import account_lock
    configured,_,account,_,_,_=backend_account(db_engine,app_paths,browser)
    provider=make_creator_provider(db_engine,app_paths,configured)
    with pytest.raises(ValueError,match='existing account'):provider.open_backend(None,threading.Event())
    other=AccountService(db_engine).create('channels','123','Other fixture')
    with pytest.raises(ValueError,match='platform mismatch'):provider.open_backend(other.id,threading.Event())
    lock=account_lock(account.id);assert lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError,match='account busy'):provider.open_backend(account.id,threading.Event())
        assert browser.profiles==[]
    finally:lock.release()


@pytest.mark.parametrize('late_cancel',[False,True])
def test_backend_cancel_never_returns_session_to_save(db_engine,app_paths,browser,late_cancel):
    from yijianjifa.webui.creator_auth import make_creator_provider
    configured,service,account,store,ref,_=backend_account(db_engine,app_paths,browser)
    previous=store.get(account.id,ref=ref);cancel=threading.Event()
    if late_cancel:browser.context.on_close=cancel.set
    else:browser.page.wait_for_timeout=lambda delay:cancel.set()
    assert make_creator_provider(db_engine,app_paths,configured).open_backend(account.id,cancel) is None
    assert browser.context.closed==1 and service.get(account.id).session_ref==ref
    assert store.get(account.id,ref=ref)==previous


def test_backend_changed_identity_discards_earlier_observation_and_unlocks(db_engine,app_paths,browser):
    from yijianjifa.webui.creator_auth import make_creator_provider
    from yijianjifa.webui.chromium_publish import account_lock
    configured,service,account,store,ref,_=backend_account(db_engine,app_paths,browser)
    previous=store.get(account.id,ref=ref)
    def change(delay):browser.page.observation['uid']='456'
    browser.page.wait_for_timeout=change
    with pytest.raises(AccountIdentityMismatch):make_creator_provider(db_engine,app_paths,configured).open_backend(account.id,threading.Event())
    assert browser.context.closed==1 and browser.page.evaluations==3
    assert service.get(account.id).session_ref==ref and store.get(account.id,ref=ref)==previous
    lock=account_lock(account.id);assert lock.acquire(blocking=False);lock.release()


def test_backend_closed_without_verified_identity_does_not_complete(db_engine,app_paths,browser):
    from yijianjifa.webui.creator_auth import make_creator_provider
    configured,service,account,_,ref,_=backend_account(db_engine,app_paths,browser)
    browser.page.observation=None
    assert make_creator_provider(db_engine,app_paths,configured).open_backend(account.id,threading.Event()) is None
    assert browser.page.waits==[750] and browser.context.closed==1
    assert service.get(account.id).session_ref==ref


def test_backend_already_closed_browser_retains_verified_observation(db_engine,app_paths,browser):
    from yijianjifa.webui.creator_auth import make_creator_provider
    configured,_,account,_,_,_=backend_account(db_engine,app_paths,browser)
    def close():
        browser.context.closed+=1
        assert browser.page.closed
        raise RuntimeError('synthetic browser already closed')
    browser.context.close=close
    assert make_creator_provider(db_engine,app_paths,configured).open_backend(account.id,threading.Event())['platform_user_id']=='123'
    assert browser.context.closed==1


@pytest.mark.parametrize('platform',['bilibili','channels'])
def test_backend_runtime_saves_after_close_without_changing_capabilities(db_engine,app_paths,browser,platform):
    from yijianjifa.webui.creator_auth import make_creator_provider,decode_session
    from yijianjifa.app_services.login_runtime import LoginRuntime
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    configured,service,account,store,ref,current=backend_account(db_engine,app_paths,browser,platform)
    capabilities=CapabilitySnapshot(ordinary='not_found',commerce='not_found',location_scope='national')
    service.update_capabilities(account.id,capabilities)
    observed=threading.Event();close_window=threading.Event()
    def wait(delay):
        observed.set();assert close_window.wait(2);browser.page.closed=True
    browser.page.wait_for_timeout=wait
    runtime=LoginRuntime(db_engine,app_paths,providers={platform:make_creator_provider(db_engine,app_paths,configured)})
    try:
        job=runtime.start('creator-backend',account.id,backend=True)
        assert observed.wait(1) and runtime.status(job['id'])['state']=='waiting'
        assert service.get(account.id).session_ref==ref
        close_window.set();runtime.thread.join(2)
        assert not runtime.thread.is_alive() and runtime.status(job['id'])['state']=='completed'
        saved=service.get(account.id)
        assert saved.id==account.id and saved.platform==platform and saved.session_ref!=ref
        assert saved.capabilities==capabilities and len(service.list())==1
        assert decode_session(configured,store.get(saved.id,ref=saved.session_ref),'123')==current
    finally:close_window.set();runtime.close(2)


def test_window_close_or_late_cancel_is_not_login_success(db_engine,app_paths,browser):
    from yijianjifa.webui.creator_auth import make_creator_provider
    browser.page.observation=None
    assert make_creator_provider(db_engine,app_paths,spec())(None,threading.Event()) is None
    assert browser.context.closed==1 and not browser.profiles[0].exists()
    browser.page.closed=False;browser.page.observation=identity()
    cancel=threading.Event();browser.context.on_close=cancel.set
    assert make_creator_provider(db_engine,app_paths,spec())(None,cancel) is None
    assert AccountService(db_engine).list()==[]


def test_official_login_landing_returns_to_creator_page_before_identity_capture(db_engine,app_paths,browser):
    from yijianjifa.webui.creator_auth import make_creator_provider
    configured=replace(spec(),return_home_origins=frozenset({'https://www.bilibili.com'}))
    browser.page.observation=None
    def goto(url,**options):
        browser.page.visits.append(url)
        browser.page.url='https://www.bilibili.com/' if len(browser.page.visits)==1 else url
        if len(browser.page.visits)==2:browser.page.observation=identity()
    browser.page.goto=goto
    result=make_creator_provider(db_engine,app_paths,configured)(None,threading.Event())
    assert result['platform_user_id']=='123'
    assert browser.page.visits==['https://member.bilibili.com','https://member.bilibili.com']


def test_foreign_landing_is_not_automatically_revisited_or_accepted(db_engine,app_paths,browser):
    from yijianjifa.webui.creator_auth import make_creator_provider
    configured=replace(spec(),return_home_origins=frozenset({'https://www.bilibili.com'}))
    browser.page.observation=None;browser.page.url='https://www.bilibili.com.evil.example/'
    assert make_creator_provider(db_engine,app_paths,configured)(None,threading.Event()) is None
    assert browser.page.visits==['https://member.bilibili.com']
