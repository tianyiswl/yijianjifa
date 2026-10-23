"""Real DOM counterexamples, with all browser traffic fulfilled locally.

These checks use synthetic account data. They never open an account profile,
read credentials, upload a video, or claim current-platform acceptance.
"""
from types import SimpleNamespace
import threading

import pytest

from yijianjifa.webui import bilibili_auth as auth, bilibili_multi_publish as m
from yijianjifa.webui.chromium_publish_session import LiveSession


@pytest.fixture(scope='module')
def offline_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, channel='chromium')
        try: yield browser
        finally: browser.close()


@pytest.fixture
def offline_case(offline_browser):
    context = offline_browser.new_context(service_workers='block')
    nav = dict(code=0, data=dict(isLogin=True, mid=111, uname='Fixture Bili'))
    requests = []
    def local(route):
        requests.append(route.request.url)
        if route.request.url.endswith('/x/web-interface/nav'):
            route.fulfill(json=nav, headers={'Access-Control-Allow-Origin': 'https://member.bilibili.com'})
        else:
            route.fulfill(content_type='text/html', body='<main id="app"><h1>稿件管理</h1></main>')
    context.route('**/*', local)
    page = context.new_page()
    page.goto(m.form_contract.MANAGE_URL)
    adapter = m.BilibiliMultiPublishAdapter.__new__(m.BilibiliMultiPublishAdapter)
    adapter.cancel = threading.Event()
    adapter.step_timeout = 180
    adapter._thread = lambda: None
    adapter._trace = lambda *args, **kwargs: None  # This fixture has no database; execution logs are tested separately.
    adapter._open = lambda *args, **kwargs: None
    adapter._version = lambda *args, **kwargs: True
    adapter._account = lambda s: SimpleNamespace(id='fixture-account', nickname='Fixture Bili')
    payload = dict(platform_user_id='111', execution_mode='publish_now',
                   fields=dict(title='Fixture content', body='', tags='#功能测试',
                               category_choice='human:knowledge', creation_statement_choice='no_mark'))
    session = LiveSession(payload, dict(delivery_id='fixture-target', attempt=1, digest='fixture'))
    session.page = page
    adapter._init_observations(session)
    page.on('response', lambda response: adapter._response(session, response))
    try: yield adapter, session, page, nav, requests
    finally: context.close()


def header(page, *, info='missing', connected=True, mid=111):
    page.evaluate('''args => {
      const nav={isLogin:true,mid:args.mid,uname:'Fixture Bili'};
      const el=document.createElement('header');el.textContent='Fixture Bili';
      if(args.connected) document.body.append(el);
      const state={};if(args.info!=='missing') state.upInfo=args.info;
      document.querySelector('#app').__vue__={$store:{state},$children:[{navData:nav,$el:el,$children:[]}]};
    }''', dict(info=info, connected=connected, mid=mid))


def observe_nav(page):
    # Only an intercepted synthetic fixture request; production never requests
    # this endpoint itself, and a response cannot replace the connected header.
    page.evaluate("async()=>{await fetch('https://api.bilibili.com/x/web-interface/nav').then(r=>r.json())}")


def expire_identity_poll(monkeypatch):
    clock = [0]
    def monotonic():
        clock[0] += 5
        return clock[0]
    monkeypatch.setattr(m, 'time', SimpleNamespace(monotonic=monotonic))


@pytest.mark.parametrize('info', ['missing', {}])
def test_normal_manager_nav_and_connected_header_allow_uninitialized_store(offline_case, info):
    adapter, session, page, _, requests = offline_case
    header(page, info=info)
    observe_nav(page)
    assert page.evaluate(m.BILIBILI_CHALLENGE) == dict(sms=False, complex=False, login=False)
    result = adapter._validate_session(session, session.payload, 'publish_now')
    assert result.authorized and result.observed_uid == '111'
    assert session.identity_nav == dict(logged_in=True, uid='111')
    assert session.broker.status()['request'] is None
    assert page.url == m.form_contract.MANAGE_URL
    assert requests == [m.form_contract.MANAGE_URL, 'https://api.bilibili.com/x/web-interface/nav']


@pytest.mark.parametrize('change', ['no-header', 'disconnected', 'conflicting-store'])
def test_normal_nav_cannot_alone_authorize_and_missing_identity_is_not_login(offline_case, monkeypatch, change):
    adapter, session, page, _, _ = offline_case
    if change == 'disconnected': header(page, connected=False)
    if change == 'conflicting-store': header(page, info=dict(isLogin=True, mid=222, uname='Other fixture'))
    observe_nav(page)
    expire_identity_poll(monkeypatch)
    result = adapter._validate_session(session, session.payload, 'publish_now')
    request = session.broker.status()['request']
    assert result.state == 'paused_auth'
    assert request['reason'] == 'identity_unavailable'
    assert '扫码' not in request['message'] and '未登录' not in request['message']
    assert page.url == m.form_contract.MANAGE_URL
    assert not session.uploaded and not session.final


@pytest.mark.parametrize('data', [dict(isLogin=False), dict(isLogin=True, mid=222, uname='Other fixture')])
def test_normal_nav_logout_or_other_uid_cannot_authorize_stale_header(offline_case, data):
    adapter, session, page, nav, _ = offline_case
    header(page)
    nav['data'] = data
    observe_nav(page)
    with pytest.raises((ValueError, auth.AccountIdentityMismatch)):
        adapter._identity(session)
    assert not session.uploaded and not session.final


def test_changed_session_has_specific_reason_without_claiming_page_login(offline_case):
    adapter, session, page, _, requests = offline_case
    adapter._version = lambda *args, **kwargs: False
    result = adapter._validate_session(session, session.payload, 'publish_now')
    assert result.state == 'paused_auth'
    assert session.broker.status()['request']['reason'] == 'session_changed'
    assert requests == [m.form_contract.MANAGE_URL]


def test_real_login_page_remains_login_challenge(offline_case):
    adapter, session, page, _, _ = offline_case
    page.set_content('<button>扫码登录</button>')
    result = adapter._validate_session(session, session.payload, 'publish_now')
    assert result.state == 'paused_auth'
    assert session.broker.status()['request']['reason'] == 'login_required'


def test_identity_disappearing_after_validation_parks_without_reupload_or_navigation(offline_case, monkeypatch):
    adapter, session, page, _, requests = offline_case
    header(page)
    observe_nav(page)
    assert adapter._validate_session(session, session.payload, 'publish_now').authorized
    page.evaluate("()=>{delete document.querySelector('#app').__vue__}")
    expire_identity_poll(monkeypatch)
    result = adapter._advance(session, before_final=lambda: pytest.fail('no final authority while identity unavailable'))
    assert result.state == 'paused_auth'
    assert session.broker.status()['request']['reason'] == 'identity_unavailable'
    assert page.url == m.form_contract.MANAGE_URL and len(requests) == 2
    assert not session.uploaded and not session.final


@pytest.mark.parametrize('change', ['other-frame', 'post', 'foreign-host', 'foreign-path'])
def test_unrelated_nav_response_cannot_change_current_identity(offline_case, change):
    adapter, session, page, _, _ = offline_case
    header(page)
    request = SimpleNamespace(method='GET', frame=page.main_frame)
    url = 'https://api.bilibili.com/x/web-interface/nav'
    if change == 'other-frame': request.frame = object()
    if change == 'post': request.method = 'POST'
    if change == 'foreign-host': url = 'https://api.bilibili.com.evil.example/x/web-interface/nav'
    if change == 'foreign-path': url = 'https://api.bilibili.com/foreign/nav'
    adapter._response(session, SimpleNamespace(url=url, request=request,
        json=lambda: dict(code=0, data=dict(isLogin=True, mid=222, uname='Other fixture'))))
    assert session.identity_nav is None
    assert adapter._identity(session)[0] == '111'


@pytest.mark.parametrize('script', [auth.IDENTITY_SCRIPT, m.BILIBILI_CHALLENGE])
def test_transient_navigation_observation_retries_original_page_without_claiming_verification(offline_case, monkeypatch, script):
    from playwright.sync_api import Error
    adapter, session, page, _, requests = offline_case
    header(page)
    observe_nav(page)
    actual = page.evaluate
    failed = []
    def evaluate(value, *args):
        if value == script and not failed:
            failed.append(True)
            raise Error('Execution context was destroyed, most likely because of a navigation')
        return actual(value, *args)
    monkeypatch.setattr(page, 'evaluate', evaluate)
    result = adapter._validate_session(session, session.payload, 'publish_now')
    assert result.authorized and failed == [True]
    assert session.broker.status()['request'] is None
    assert page.url == m.form_contract.MANAGE_URL and len(requests) == 2


def test_unrelated_page_observation_error_is_not_hidden_as_navigation(offline_case, monkeypatch):
    adapter, session, page, _, _ = offline_case
    monkeypatch.setattr(page, 'evaluate', lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic implementation error')))
    with pytest.raises(RuntimeError, match='synthetic implementation error'):
        adapter._validate_session(session, session.payload, 'publish_now')
    assert session.broker.status()['request'] is None
