import pytest
from yijianjifa.webui.douyin_auth import verified_identity, SessionObservation


def observation(**changes):
    value=dict(uid='123456',uid_after='123456',nickname='fixture',handle='handle',status=0,profile_status=0,after_status=0)
    value.update(changes);return value


def test_identity_requires_platform_uid_not_handle():
    assert verified_identity(observation(),expected_uid='123456')==('123456','fixture')
    for changes in [dict(uid=''),dict(uid_after='999'),dict(status=1),dict(profile_status=1),dict(uid=123456),dict(nickname='')]:
        with pytest.raises(ValueError): verified_identity(observation(**changes),expected_uid='123456')
    with pytest.raises(ValueError): verified_identity(observation(),expected_uid='999')


def test_capture_rechecks_identity_before_exposing_session():
    callbacks=[];cookies=[];done=[]
    observer=SessionObservation(lambda cb:callbacks.append(cb),lambda cb:cookies.append(cb),
        lambda rows,identity:b'fixture-session',expected_uid='123456')
    observer.poll(done.append);callbacks.pop()(observation())
    assert done==[]
    cookies.pop()(['fixture-cookie']);assert done==[]
    callbacks.pop()(observation())
    assert done==[dict(platform_user_id='123456',nickname='fixture',session_blob=b'fixture-session')]


def test_switch_or_cancel_during_capture_never_returns_session():
    for cancel in (False,True):
        callbacks=[];cookies=[];done=[]
        observer=SessionObservation(lambda cb:callbacks.append(cb),lambda cb:cookies.append(cb),
            lambda rows,identity:b'secret',expected_uid='123456')
        observer.poll(done.append);callbacks.pop()(observation());cookies.pop()([])
        if cancel:observer.close()
        callbacks.pop()(observation(uid='999',uid_after='999'))
        assert done==[]


def test_overlapping_poll_and_late_capture_do_not_duplicate():
    callbacks=[];cookies=[];done=[]
    observer=SessionObservation(lambda cb:callbacks.append(cb),lambda cb:cookies.append(cb),lambda rows,identity:b'fixture',expected_uid='123456')
    observer.poll(done.append);observer.poll(done.append)
    assert len(callbacks)==1
    callbacks.pop()(observation());observer.close();cookies.pop()([])
    assert callbacks==[] and done==[]


def test_cookie_restore_finishes_before_initial_navigation(monkeypatch):
    import sys
    from types import SimpleNamespace
    from yijianjifa.webui.douyin_auth import DouyinAuthWindow
    from yijianjifa.webui.cocoa_auth import CocoaAuthWindow
    pending=[];loaded=[]
    store=SimpleNamespace(setCookie_completionHandler_=lambda cookie,cb:pending.append(cb))
    config=SimpleNamespace(websiteDataStore=lambda:SimpleNamespace(httpCookieStore=lambda:store))
    fake_timer=SimpleNamespace(scheduledTimerWithTimeInterval_repeats_block_=lambda *args:None)
    monkeypatch.setitem(sys.modules,'Foundation',SimpleNamespace(NSTimer=fake_timer))
    monkeypatch.setattr(CocoaAuthWindow,'load_initial',lambda self:loaded.append(True))
    window=object.__new__(DouyinAuthWindow)
    window.finished=False;window.restored_cookies=('one','two');window.view=SimpleNamespace(configuration=lambda:config)
    window.load_initial();assert loaded==[] and len(pending)==1
    pending.pop()();assert loaded==[] and len(pending)==1
    pending.pop()();assert loaded==[True]


def test_cancel_while_restoring_does_not_navigate(monkeypatch):
    from types import SimpleNamespace
    from yijianjifa.webui.douyin_auth import DouyinAuthWindow
    from yijianjifa.webui.cocoa_auth import CocoaAuthWindow
    pending=[];loaded=[]
    store=SimpleNamespace(setCookie_completionHandler_=lambda cookie,cb:pending.append(cb))
    monkeypatch.setattr(CocoaAuthWindow,'load_initial',lambda self:loaded.append(True))
    window=object.__new__(DouyinAuthWindow)
    window.finished=False;window.restored_cookies=('one',)
    window.view=SimpleNamespace(configuration=lambda:SimpleNamespace(websiteDataStore=lambda:SimpleNamespace(httpCookieStore=lambda:store)))
    window.load_initial();window.finished=True;pending.pop()()
    assert loaded==[]


def test_identity_accepts_native_string_subclasses_without_accepting_numbers():
    from yijianjifa.webui.douyin_auth import verified_identity
    class NativeText(str):
        pass
    value = dict(uid=NativeText('123'), uid_after=NativeText('123'),
                 nickname=NativeText('Fixture'), status=0, profile_status=0, after_status=0)
    result = verified_identity(value, expected_uid='123')
    assert result == ('123', 'Fixture')
    assert all(type(part) is str for part in result)


def test_native_login_callback_invalid_result_keeps_waiting(monkeypatch):
    import sys
    import time
    from types import SimpleNamespace
    from yijianjifa.webui.douyin_auth import DouyinAuthWindow
    monkeypatch.setitem(sys.modules,'WebKit',SimpleNamespace(WKContentWorld=SimpleNamespace(defaultClientWorld=lambda:None)))
    callbacks=[]
    class View:
        def URL(self):return 'https://creator.douyin.com/creator-micro/home'
        def callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(self,*args):callbacks.append(args[-1])
    window=object.__new__(DouyinAuthWindow)
    window.finished=False;window.window=None;window.view=View();window.started=time.monotonic();window.session_observer=None;window.expected_uid='123456'
    for value,error in [(None,None),(object(),None),(42,None),('invalid',None),({},object())]:
        window.observe();assert window.session_observer.busy
        callbacks.pop()(value,error)
        assert not window.session_observer.busy and not window.finished
    window.observe();window.finished=True;callbacks.pop()(object(),None)


def test_new_account_discovers_identity_then_requires_same_identity_after_cookies():
    for switched in (False,True):
        callbacks=[];cookies=[];done=[]
        observer=SessionObservation(callbacks.append,cookies.append,lambda rows,identity:b'fixture',expected_uid=None)
        observer.poll(done.append);callbacks.pop()(observation(uid='777',uid_after='777'))
        cookies.pop()([])
        callbacks.pop()(observation(uid='888' if switched else '777',uid_after='888' if switched else '777'))
        assert len(done)==(0 if switched else 1)
        if done:assert done[0]['platform_user_id']=='777'


def test_discovery_still_requires_verified_platform_response():
    for changes in [dict(uid=''),dict(uid_after='999'),dict(status=1),dict(profile_status=1),dict(nickname='')]:
        with pytest.raises(ValueError):verified_identity(observation(**changes),expected_uid=None)


def test_provider_addition_uses_discovery_existing_login_uses_selected_identity(db_engine,app_paths,monkeypatch):
    from yijianjifa.webui import douyin_auth
    from yijianjifa.features.accounts.service import AccountService
    import threading
    from yijianjifa.webui import douyin_metadata, douyin_scheduled, douyin_capability, chromium_publish
    seen=[]
    def chromium_factory(engine,paths,*,backend=False):
        def login(account_id,cancel):
            uid=AccountService(engine).get(account_id).platform_user_id if account_id else None
            seen.append({'expected_uid':uid});return None
        return login
    monkeypatch.setattr(chromium_publish,'make_chromium_login',chromium_factory)
    class Provider:
        def __init__(self,*args,window_factory):self.factory=window_factory
        def __call__(self,*args):self.factory();return None
    monkeypatch.setattr(douyin_auth,'CocoaAuthProvider',Provider)
    monkeypatch.setattr(douyin_auth,'DouyinAuthWindow',lambda *args,**kwargs:seen.append(kwargs))
    provider=douyin_auth.make_douyin_provider(db_engine,app_paths,expected_uid='123456')
    provider(None,threading.Event());assert seen[-1]['expected_uid'] is None
    a=AccountService(db_engine).create('douyin','777','另一个测试账号')
    provider(a.id,threading.Event());assert seen[-1]['expected_uid']=='777'
    with pytest.raises(ValueError):provider.open_backend(a.id,threading.Event())
    assert provider.scheduled_publish.expected_uid=='123456'
    normal=douyin_auth.make_douyin_provider(db_engine,app_paths)
    assert normal.scheduled_publish.expected_uid is None
    normal.open_backend(a.id,threading.Event())
    assert seen[-1]['expected_uid']=='777'


def test_navigation_or_lost_callback_restarts_observation_and_rejects_late_reply(monkeypatch):
    import sys,time
    from types import SimpleNamespace
    from yijianjifa.webui.douyin_auth import DouyinAuthWindow
    monkeypatch.setitem(sys.modules,'WebKit',SimpleNamespace(WKContentWorld=SimpleNamespace(defaultClientWorld=lambda:None)))
    callbacks=[]
    class View:
        def URL(self):return 'https://creator.douyin.com/creator-micro/home'
        def callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(self,*args):callbacks.append(args[-1])
    w=object.__new__(DouyinAuthWindow)
    w.finished=False;w.window=None;w.view=View();w.started=time.monotonic();w.session_observer=None;w.expected_uid=None;w.generation=1
    w.observe();old=w.session_observer
    w.generation=2;w.observe()
    assert old.closed and w.session_observer is not old and len(callbacks)==2
    callbacks[0](observation(),None) # Cannot capture cookies or complete after navigation.
    second=w.session_observer;second.attempt_started-=36;w.observe()
    assert second.closed and len(callbacks)==3
    callbacks[-1](None,None);assert not w.session_observer.busy
