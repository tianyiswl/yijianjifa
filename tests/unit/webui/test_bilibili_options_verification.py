"""B站 configuration checks keep a human verification page, never submit."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from yijianjifa.features.accounts.service import AccountService, AccountRow, AccountIdentityMismatch
from yijianjifa.storage.db import session_scope
from yijianjifa.webui import bilibili_multi_publish as m


@pytest.fixture(scope='module')
def offline_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, channel='chromium')
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def offline_page(offline_browser):
    context = offline_browser.new_context(service_workers='block')
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.mark.parametrize('html', [
    '<h1>内容管理</h1><iframe id="captcha-sdk" style="width:1px;height:1px;border:0"></iframe>',
    '<h1>内容管理</h1><div id="captcha-sdk" style="width:300px;height:180px"></div>',
    '<div id="captcha-sdk"><img style="width:1px;height:1px"></div>',
    '<div role="dialog">稿件中不能包含验证码教程<input placeholder="标题"></div>',
    '<h1>稿件管理</h1><section class="verify-status">账号认证已完成<button>查看认证信息</button></section>',
    '<h1>稿件管理</h1><div id="captcha-sdk">页面配置<button>重新检测</button></div>',
    '<div id="captcha-sdk"><img width="280" height="160"><button>SDK 配置</button></div>',
    '<section class="verify-status">已完成安全验证<img width="280" height="160"><button>查看认证信息</button></section>',
    '<section class="verify-status">安全验证已完成<canvas width="280" height="160"></canvas><button>重新检测</button></section>',
    '<div id="captcha-sdk"><canvas width="280" height="160"></canvas><button>SDK 配置</button></div>',
])
def test_sdk_placeholders_and_unrelated_dialog_are_not_captcha(offline_page, html):
    offline_page.set_content(html)
    script = getattr(m, 'BILIBILI_CHALLENGE', m.browser.CHALLENGE)
    assert offline_page.evaluate(script) == dict(sms=False, complex=False, login=False)


@pytest.mark.parametrize('html,expected', [
    ('<div role="dialog">请完成安全验证<iframe id="captcha-frame" style="width:300px;height:180px;border:0"></iframe></div>', 'complex'),
    ('<div class="captcha-panel"><canvas width="280" height="160"></canvas><button>拖动滑块完成验证</button></div>', 'complex'),
    ('<iframe title="安全验证" style="width:300px;height:180px;border:0"></iframe>', 'complex'),
    ('<div class="geetest-panel"><button style="width:220px;height:40px">点击按钮进行验证</button></div>', 'complex'),
    ('<div class="geetest-panel"><canvas width="280" height="160"></canvas><div role="slider" style="width:220px;height:40px"></div></div>', 'complex'),
    ('<div class="captcha-panel"><canvas width="280" height="160"></canvas><input type="range"></div>', 'complex'),
    ('<input placeholder="验证码">', 'sms'),
    ('<button>扫码登录</button>', 'login'),
    ('<div style="opacity:0"><div role="dialog">请完成安全验证<canvas width="280" height="160"></canvas></div></div>', None),
    ('<div style="display:none"><input placeholder="验证码"></div>', None),
])
def test_actual_verification_controls_keep_human_gate(offline_page, html, expected):
    offline_page.set_content(html)
    script = getattr(m, 'BILIBILI_CHALLENGE', m.browser.CHALLENGE)
    assert m.browser.challenge_kind(offline_page.evaluate(script)) == expected


@pytest.fixture
def options_case(db_engine, app_paths, monkeypatch):
    account = AccountService(db_engine).create('bilibili', '111', '合成账号')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id)
        row.session_ref = 'version-1'
        row.login_state = 'valid'
    adapter = m.BilibiliMultiPublishAdapter(db_engine, app_paths)
    state = dict(challenge='complex', uid='111', show_fails=False, close_fails=False)
    events = []
    class Page:
        def is_closed(self): return False
        def goto(self, url, **kwargs): events.append(('navigate', url))
        def wait_for_timeout(self, duration): pass
        def bring_to_front(self):
            if state['show_fails']: raise RuntimeError('synthetic show failure')
            events.append(('show', self))
    page = Page()
    def close_context():
        if state['close_fails']: raise RuntimeError('synthetic close failure')
        events.append(('close', None))
    def open_session(s, **kwargs):
        assert getattr(s, 'options_only', False), 'configuration must opt into strict visible session'
        assert s not in adapter.sessions.values(), 'configuration cannot register as a delivery'
        assert not s.uploaded and not s.final
        if s.page: return
        assert m.browser.account_lock(account.id).acquire(blocking=False)
        s.locked = True
        s.page = page
        s.context = SimpleNamespace(close=close_context)
        s.original_ref = 'version-1'
        adapter._init_observations(s)
        s.options = dict(categories=[dict(id=101, name='合成分区', mode='human')],
                         statements=[dict(id=-1, name='不标记')], category_mode='human')
        s.options_at = datetime.now(timezone.utc).isoformat()
        events.append(('open', s))
    def identity(s):
        if state['uid'] != s.payload['platform_user_id']: raise AccountIdentityMismatch('synthetic mismatch')
        return state['uid'], '合成账号'
    monkeypatch.setattr(adapter, '_open', open_session)
    monkeypatch.setattr(adapter, '_challenge', lambda s: state['challenge'])
    monkeypatch.setattr(adapter, '_identity', identity)
    for method in ('_advance', 'submit', 'resume', 'reconcile', '_upload', '_click_final'):
        monkeypatch.setattr(adapter, method, lambda *args, **kwargs: pytest.fail('options must never enter publishing'))
    yield adapter, account.id, page, state, events
    state['close_fails'] = False
    adapter.close()


def test_challenge_retains_original_page_show_does_not_navigate_and_continue_is_explicit(options_case):
    adapter, aid, page, state, events = options_case
    result = adapter.begin_platform_options(aid, 'ticket-one')
    assert result['status'] == 'verification_required' and result['verification_available'] is True
    session = adapter.options_sessions['ticket-one']
    assert session.page is page and session.locked and not adapter.sessions
    shown = adapter.continue_platform_options(aid, 'ticket-one', 'show')
    assert shown['verification_view_state'] == 'opened'
    assert events[-1] == ('show', page) and not any(e[0] == 'navigate' for e in events)
    assert adapter.continue_platform_options(aid, 'ticket-one', 'continue')['status'] == 'verification_required'
    assert session.page is page and not any(e[0] == 'close' for e in events)
    state['challenge'] = None
    ready = adapter.continue_platform_options(aid, 'ticket-one', 'continue')
    assert ready['status'] == 'ready' and ready['cleanup_complete'] is True
    assert adapter.get_cached_platform_options(aid)['categories'] == ready['categories']
    assert 'ticket-one' not in adapter.options_sessions and not session.locked
    assert [e[0] for e in events] == ['open', 'show', 'navigate', 'close']


@pytest.mark.parametrize('change', ['uid', 'account-version'])
def test_resume_rechecks_frozen_uid_and_session_before_writing_cache(options_case, change):
    adapter, aid, _, state, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    state['challenge'] = None
    if change == 'uid': state['uid'] = '222'
    else:
        with session_scope(adapter.engine) as db: db.get(AccountRow, aid).session_ref = 'version-2'
    result = adapter.continue_platform_options(aid, 'ticket-one', 'continue')
    assert result['status'] == 'error' and result['cleanup_complete'] is True
    assert adapter.get_cached_platform_options(aid)['status'] == 'empty'
    assert [e[0] for e in events] == ['open', 'close']


def test_wrong_ticket_or_account_cannot_control_retained_window(options_case):
    adapter, aid, _, _, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    assert adapter.continue_platform_options(aid, 'other-ticket', 'show')['status'] == 'error'
    assert adapter.continue_platform_options('other-account', 'ticket-one', 'cancel')['status'] == 'error'
    assert [e[0] for e in events] == ['open']
    assert 'ticket-one' in adapter.options_sessions


def test_cancel_before_page_open_finishes_as_cancelled_without_opening(options_case):
    adapter,aid,_,_,events=options_case
    result=adapter.continue_platform_options(aid,'never-started-ticket','cancel')
    assert result['status']=='cancelled' and result['cleanup_complete'] is True
    assert events==[] and not adapter.options_sessions


def test_show_failure_keeps_original_page_and_does_not_claim_opened(options_case):
    adapter, aid, page, state, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    state['show_fails'] = True
    result = adapter.continue_platform_options(aid, 'ticket-one', 'show')
    assert result['verification_view_state'] == 'failed' and result['verification_available'] is True
    assert adapter.options_sessions['ticket-one'].page is page and not any(e[0] == 'close' for e in events)


@pytest.mark.parametrize('action', ['cancel', 'ready'])
def test_close_failure_preserves_page_and_account_lock_until_explicit_retry(options_case, action):
    adapter, aid, page, state, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    state['close_fails'] = True
    if action == 'ready': state['challenge'] = None
    result = adapter.continue_platform_options(aid, 'ticket-one', 'cancel' if action == 'cancel' else 'continue')
    assert result['status'] == 'error' and result['cleanup_complete'] is False
    session = adapter.options_sessions['ticket-one']
    assert session.page is page and session.locked
    assert not m.browser.account_lock(aid).acquire(blocking=False)
    state['close_fails'] = False
    cancelled = adapter.continue_platform_options(aid, 'ticket-one', 'cancel')
    assert cancelled['status'] == 'cancelled' and cancelled['cleanup_complete'] is True
    assert not adapter.options_sessions and not session.locked


def test_expiry_and_shutdown_close_retained_configuration_sessions(options_case, monkeypatch):
    adapter, aid, _, _, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    adapter.options_sessions['ticket-one'].options_expires_at = 0
    assert adapter.expire_platform_options() == 1
    assert not adapter.options_sessions
    adapter.begin_platform_options(aid, 'ticket-two')
    adapter.close()
    assert not adapter.options_sessions and sum(e[0] == 'close' for e in events) == 2


def test_expired_continue_cannot_reopen_or_navigate(options_case):
    adapter, aid, _, _, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    adapter.options_sessions['ticket-one'].options_expires_at = 0
    result = adapter.continue_platform_options(aid, 'ticket-one', 'continue')
    assert result['status'] == 'error' and result['cleanup_complete'] is True
    assert [e[0] for e in events] == ['open', 'close']


def test_same_ticket_reuses_original_window_and_second_ticket_cannot_steal_account(options_case):
    adapter, aid, _, _, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    assert adapter.begin_platform_options(aid, 'ticket-one')['verification_available'] is True
    assert adapter.begin_platform_options(aid, 'ticket-two')['status'] == 'account_busy'
    assert [e[0] for e in events] == ['open']


def test_configuration_open_is_visible_and_final_guard_never_relaxes(db_engine, app_paths, monkeypatch):
    account = AccountService(db_engine).create('bilibili', '111', '合成账号')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id); row.session_ref = 'version-1'; row.login_state = 'valid'
    adapter = m.BilibiliMultiPublishAdapter(db_engine, app_paths)
    recorded = dict(routes=[], launch=[])
    page = SimpleNamespace(on=lambda *args: None, goto=lambda *args, **kwargs: None, is_closed=lambda: False)
    context = SimpleNamespace(pages=[page], route=lambda pattern, fn: recorded['routes'].append(fn),
                              set_default_timeout=lambda value: None, close=lambda: None)
    def launch(path, **kwargs): recorded['launch'].append(kwargs); return context
    adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=lambda: None)
    monkeypatch.setattr(m.creator_auth, 'chromium_profile', lambda *args: app_paths.cache_dir/'synthetic-profile')
    monkeypatch.setattr(m.creator_auth, 'restore_session', lambda *args: None)
    monkeypatch.setattr(adapter, '_challenge', lambda s: 'complex')
    adapter.begin_platform_options(account.id, 'ticket-one')
    assert recorded['launch'][0]['headless'] is False and recorded['launch'][0]['service_workers'] == 'block'
    session = adapter.options_sessions['ticket-one']; session.final = True
    outcomes = []
    for path in ('/x/vu/web/add/v3', '/x/vu/web/edit', '/x/vu/web/draft/save', '/x/unknown/write'):
        route = SimpleNamespace(request=SimpleNamespace(url='https://member.bilibili.com'+path, method='POST'),
                                abort=lambda reason: outcomes.append('blocked'), fallback=lambda: outcomes.append('allowed'))
        recorded['routes'][0](route)
    assert outcomes == ['blocked']*4
    adapter.close()


@pytest.mark.parametrize('stop_fails', [False, True])
def test_shutdown_cleans_regular_sessions_even_when_options_close_fails(options_case, stop_fails):
    adapter, aid, _, state, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    other = AccountService(adapter.engine).create('bilibili', '222', '其他合成账号')
    normal = m.LiveSession(dict(account_id=other.id), {})
    normal.context = SimpleNamespace(close=lambda: events.append(('regular-close', None)))
    assert m.browser.account_lock(other.id).acquire(blocking=False)
    normal.locked = True
    adapter.sessions['synthetic-normal'] = normal
    def stop():
        events.append(('driver-stop', None))
        if stop_fails: raise RuntimeError('synthetic driver stop failure')
    driver = SimpleNamespace(stop=stop)
    adapter.driver = driver
    state['close_fails'] = True
    with pytest.raises(ValueError, match='configuration browser close failed'):
        adapter.close()
    assert ('regular-close', None) in events and not adapter.sessions and not normal.locked
    assert sum(e[0] == 'driver-stop' for e in events) == 1
    if stop_fails:
        assert adapter.options_sessions['ticket-one'].locked and adapter.driver is driver
    else:
        assert not adapter.options_sessions and adapter.driver is None
        assert m.browser.account_lock(aid).acquire(blocking=False)
        m.browser.account_lock(aid).release()
    state['close_fails'] = False
    driver.stop = lambda: None
    adapter.close()
    assert not adapter.options_sessions and adapter.driver is None


def test_cache_boundary_rechecks_uid_after_options_were_observed(options_case, monkeypatch):
    adapter, aid, _, state, events = options_case
    adapter.begin_platform_options(aid, 'ticket-one')
    state['challenge'] = None
    calls = []
    def identity(s):
        calls.append(None)
        if len(calls) == 3: raise AccountIdentityMismatch('same nickname different uid')
        return '111', '合成账号'
    monkeypatch.setattr(adapter, '_identity', identity)
    result = adapter.continue_platform_options(aid, 'ticket-one', 'continue')
    assert result['status'] == 'error' and result['cleanup_complete'] is True
    assert len(calls) == 3 and adapter.get_cached_platform_options(aid)['status'] == 'empty'
    assert [e[0] for e in events] == ['open', 'navigate', 'close']


def test_invalid_local_session_does_not_open_configuration_browser(options_case, monkeypatch):
    adapter, aid, _, _, events = options_case
    with session_scope(adapter.engine) as db: db.get(AccountRow, aid).login_state = 'expired'
    monkeypatch.setattr(adapter, '_open', lambda *args, **kwargs: pytest.fail('local invalid login must not open'))
    result = adapter.begin_platform_options(aid, 'ticket-one')
    assert result['status'] == 'verification_required' and result['verification_stage'] == 'local'
    assert result['verification_available'] is False and result['cleanup_complete'] is True
    assert not adapter.options_sessions and not events
