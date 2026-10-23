"""Exercise the real login provider with a deterministic browser boundary."""
import threading
from types import SimpleNamespace

import pytest

from yijianjifa.webui import chromium_publish as transport


def surface(**changes):
    value = dict(loading=False, blank=False, landing=False, scan=False,
                 form=False, qr=False, challenge=False, interacting=False, dashboard=False)
    value.update(changes)
    return value


@pytest.fixture
def login_browser(monkeypatch, db_engine, app_paths):
    clock = [0.0]
    cancel = threading.Event()
    events = []

    class Page:
        main_frame = object()
        url = 'https://creator.douyin.com/creator-micro/home'
        state = surface(landing=True)
        closed = False
        after_reload = None
        scan_ready = True

        def on(self, *_args): pass
        def is_closed(self): return self.closed
        def goto(self, url, **options):
            self.url = url
            events.append(('goto', options))
        def evaluate(self, _script): return dict(self.state)
        def wait_for_timeout(self, ms):
            clock[0] += ms / 1000
            if clock[0] >= 40: cancel.set()
        def reload(self, **options):
            events.append(('reload', options))
            if self.after_reload is not None: self.state = self.after_reload
        def get_by_text(self, text, **_options):
            assert text == '扫码登录'
            page = self
            class Scan:
                first = property(lambda self: self)
                def filter(self, **_kwargs): return self
                def click(self, **_kwargs):
                    events.append(('scan', clock[0]))
                    page.state = surface(landing=True, scan=True, qr=page.scan_ready, form=not page.scan_ready)
            return Scan()

    page = Page()
    context = SimpleNamespace(pages=[page], set_default_timeout=lambda _: None,
                              close=lambda: events.append(('close', clock[0])))
    driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *_args, **_kwargs: context))
    class Driver:
        def __enter__(self): return driver
        def __exit__(self, *_args): pass
    monkeypatch.setattr('playwright.sync_api.sync_playwright', Driver)
    monkeypatch.setattr(transport.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(transport, 'browser_options', lambda _: {})
    def observe(*_args, **_kwargs):
        events.append(('identity', clock[0]))
        raise ValueError('not authenticated')
    monkeypatch.setattr(transport, 'observe_login_session', observe)
    return SimpleNamespace(page=page, events=events, cancel=cancel,
                           login=transport.make_chromium_login(db_engine, app_paths),
                           profiles=app_paths.data_dir/'profiles')


def test_blank_login_recovers_then_waits_for_scan_without_identity_requests(login_browser):
    b = login_browser
    b.page.after_reload = surface(landing=True, qr=True)
    assert b.login(None, b.cancel) is None
    assert len([e for e in b.events if e[0] == 'reload']) == 1
    assert not [e for e in b.events if e[0] == 'identity']
    assert not list(b.profiles.glob('.chromium-login-*'))
    assert b.events[-1][0] == 'close'


def test_default_sms_login_selects_scan_once(login_browser):
    b = login_browser
    b.page.state = surface(landing=True, scan=True, form=True)
    b.login(None, b.cancel)
    assert len([e for e in b.events if e[0] == 'scan']) == 1
    assert not [e for e in b.events if e[0] in {'reload', 'identity'}]


@pytest.mark.parametrize('state', [
    surface(landing=True, qr=True, scan=True),
    surface(landing=True, challenge=True, scan=True),
    surface(landing=True, form=True, scan=True, interacting=True),
])
def test_ready_qr_or_user_verification_is_never_replaced(login_browser, state):
    b = login_browser
    b.page.state = state
    b.login(None, b.cancel)
    assert not [e for e in b.events if e[0] in {'reload', 'scan', 'identity'}]


def test_blank_retries_are_bounded(login_browser):
    b = login_browser
    b.login(None, b.cancel)
    assert len([e for e in b.events if e[0] == 'reload']) == 2
    assert not [e for e in b.events if e[0] == 'identity']


def test_dashboard_still_requires_verified_identity_observer(login_browser, monkeypatch):
    b = login_browser
    b.page.state = surface(dashboard=True)
    result = dict(platform_user_id='123456', nickname='fixture', session_blob=b'fixture')
    def observe(*_args, **_kwargs):
        b.events.append(('verified', None))
        return result
    monkeypatch.setattr(transport, 'observe_login_session', observe)
    assert b.login(None, b.cancel) == result
    assert [e[0] for e in b.events] == ['goto', 'verified', 'close']


def test_external_verification_page_is_left_untouched(login_browser):
    b = login_browser
    original = b.page.goto
    def goto(*args, **kwargs):
        original(*args, **kwargs)
        b.page.url = 'https://verification.example.test/'
    b.page.goto = goto
    b.login(None, b.cancel)
    assert not [e for e in b.events if e[0] in {'reload', 'scan', 'identity'}]


@pytest.mark.parametrize('protected_state', [
    surface(landing=True, qr=True),
    surface(landing=True, challenge=True),
    surface(landing=True, form=True, interacting=True),
])
def test_recovery_does_not_resume_after_human_flow_has_started(protected_state):
    from yijianjifa.webui.douyin_login_bootstrap import DouyinLoginBootstrap
    state = [protected_state]
    page = SimpleNamespace(url='https://creator.douyin.com/creator-micro/home',
                           evaluate=lambda _script: state[0],
                           reload=lambda **kwargs: pytest.fail('human flow was reloaded'))
    bootstrap = DouyinLoginBootstrap(0)
    assert bootstrap.poll(page, 1) is False
    state[0] = surface(landing=True)
    assert bootstrap.poll(page, 30) is False
    state[0] = surface(dashboard=True)
    assert bootstrap.poll(page, 31) is True


def test_closed_window_exits_without_creating_account(login_browser):
    b = login_browser
    b.page.closed = True
    assert b.login(None, b.cancel) is None
    assert [e[0] for e in b.events] == ['goto', 'close']
    assert not list(b.profiles.glob('.chromium-login-*'))


def test_initial_navigation_timeout_can_recover(login_browser):
    from playwright.sync_api import TimeoutError
    b = login_browser
    def timeout(*args, **kwargs): raise TimeoutError('fixture navigation timeout')
    b.page.goto = timeout
    b.page.after_reload = surface(landing=True, qr=True)
    assert b.login(None, b.cancel) is None
    assert len([e for e in b.events if e[0] == 'reload']) == 1


def test_tls_navigation_error_is_not_ignored(login_browser):
    from playwright.sync_api import Error
    b = login_browser
    def tls_error(*args, **kwargs): raise Error('net::ERR_CERT_AUTHORITY_INVALID')
    b.page.goto = tls_error
    with pytest.raises(Error, match='ERR_CERT_AUTHORITY_INVALID'):
        b.login(None, b.cancel)
    assert [e[0] for e in b.events] == ['close']
    assert not list(b.profiles.glob('.chromium-login-*'))


def test_partial_marketing_shell_does_not_start_identity_requests(login_browser):
    b = login_browser
    b.page.state = surface()
    b.login(None, b.cancel)
    assert not [e for e in b.events if e[0] == 'identity']


def test_empty_qr_placeholder_with_empty_sms_fields_gets_bounded_recovery(login_browser):
    b = login_browser
    b.page.scan_ready = False
    b.page.state = surface(landing=True, scan=True, form=True)
    b.login(None, b.cancel)
    assert len([e for e in b.events if e[0] == 'reload']) == 2
    assert len([e for e in b.events if e[0] == 'scan']) == 3
    assert not [e for e in b.events if e[0] == 'identity']


def test_unactionable_scan_tab_is_not_clicked_on_every_poll(login_browser):
    b = login_browser
    b.page.state = surface(landing=True, scan=True, form=True)
    original = b.page.get_by_text
    def get_by_text(*args, **kwargs):
        locator = original(*args, **kwargs)
        def click(**_kwargs):
            b.events.append(('failed-scan', None))
            raise TimeoutError('fixture covered tab')
        locator.click = click
        return locator
    b.page.get_by_text = get_by_text
    b.login(None, b.cancel)
    assert len([e for e in b.events if e[0] == 'failed-scan']) == 3
    assert len([e for e in b.events if e[0] == 'reload']) == 2


def test_existing_account_reuses_profile_and_keeps_expected_identity(login_browser, db_engine, monkeypatch):
    from yijianjifa.features.accounts.service import AccountService
    b = login_browser
    account = AccountService(db_engine).create('douyin', '777', 'fixture')
    restored = []
    monkeypatch.setattr(transport, 'restore_account_session', lambda *args: restored.append(args))
    def observe(page, context, expected_uid, **kwargs):
        assert expected_uid == '777'
        return dict(platform_user_id='777', nickname='fixture', session_blob=b'fixture')
    monkeypatch.setattr(transport, 'observe_login_session', observe)
    b.page.state = surface(dashboard=True)
    assert b.login(account.id, b.cancel)['platform_user_id'] == '777'
    assert len(restored) == 1 and restored[0][-1] == '777'
    assert not [e for e in b.events if e[0] == 'reload']
