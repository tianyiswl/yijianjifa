"""Login recovery owns one synthetic profile; no real account or network IO."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from yijianjifa.features.accounts.service import AccountService, AccountRow, AccountIdentityMismatch
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.webui import channels_multi_publish as module
from yijianjifa.webui.chromium_publish_session import LiveSession
from test_channels_dom_visibility import offline_browser, offline_page, mount_shadow
from test_channels_multi_publish import case


@pytest.fixture
def recovery(db_engine, app_paths, monkeypatch, request):
    account = AccountService(db_engine).create('channels', 'synthetic-finder', '合成视频号')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id)
        row.session_ref = 'synthetic-session'
        row.login_state = 'valid'
        if getattr(request, 'param', None) == 'visible':
            db.add(Setting(key='publish.browser_mode', value_json='"visible"'))
    events, contexts, restores = [], [], []
    state = {'fail_close': False, 'fail_open': False, 'fail_navigation': False}

    class Page:
        def __init__(self):
            self.closed = False
            self.url = 'about:blank'
        def on(self, *args): pass
        def goto(self, url, **kwargs):
            if state['fail_navigation']: raise RuntimeError('private-cookie-value')
            self.url = url
            events.append(('navigate', url))
        def is_closed(self): return self.closed
        def bring_to_front(self): events.append(('front', self))
        def screenshot(self, **kwargs): return b'synthetic-frame'

    class Context:
        def __init__(self): self.pages = [Page()]; self.closed = False
        def set_default_timeout(self, *args): pass
        def route(self, *args): pass
        def close(self):
            events.append(('close', self))
            if state['fail_close']: raise RuntimeError('private-cookie-value')
            self.closed = True
            self.pages[0].closed = True

    def launch(profile, **options):
        events.append(('launch', (profile, options)))
        if state['fail_open']: raise RuntimeError('private-cookie-value')
        context = Context(); contexts.append(context)
        return context

    adapter = module.ChannelsMultiPublishAdapter(db_engine, app_paths)
    adapter._thread()
    adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(module.creator_auth, 'restore_session', lambda *args: restores.append(args))
    monkeypatch.setattr(adapter, '_persist_sessions', lambda: None)
    payload = {'platform': 'channels', 'account_id': account.id, 'platform_user_id': 'synthetic-finder'}
    binding = {'delivery_id': 'synthetic-delivery', 'attempt': 1, 'digest': 'synthetic-digest'}
    session = LiveSession(payload, binding)
    adapter.sessions['synthetic-session'] = session
    adapter._open(session)
    try:
        yield SimpleNamespace(adapter=adapter, session=session, account=account,
                              events=events, contexts=contexts, restores=restores, state=state)
    finally:
        state.update(fail_close=False, fail_open=False, fail_navigation=False)
        adapter._release_page(session)


def launches(case):
    return [value for event, value in case.events if event == 'launch']


@pytest.fixture
def fresh_recovery(recovery):
    c = recovery
    payload, binding = deepcopy(c.session.payload), deepcopy(c.session.binding)
    c.adapter._release_page(c.session)
    c.session = LiveSession(payload, binding)
    c.adapter.sessions['synthetic-session'] = c.session
    try:
        yield c
    finally:
        c.state.update(fail_close=False, fail_open=False, fail_navigation=False)
        c.adapter._release_page(c.session)


def test_first_login_window_uses_current_archive_when_no_original_page_exists(fresh_recovery):
    c = fresh_recovery; s = c.session
    # A changed session can park before _open; a remembered reference alone
    # does not mean its credentials were restored into a browser profile.
    s.original_ref = 'synthetic-session'
    c.adapter._park(s, 'login', reason='session_changed')
    assert launches(c)[-1][1]['headless'] is False
    assert len(c.restores) == 2
    assert not s.resume_authorized and not s.uploaded and not s.final


@pytest.mark.parametrize('failure', ['launch', 'restore'])
def test_retry_of_first_window_still_restores_current_login_archive(fresh_recovery, monkeypatch, failure):
    c = fresh_recovery; s = c.session
    failed = [True]; calls = []
    def restore(*args):
        calls.append(args)
        if failed[0] and failure == 'restore':
            raise RuntimeError('synthetic archive temporarily unavailable')
    monkeypatch.setattr(module.creator_auth, 'restore_session', restore)
    c.state['fail_open'] = failure == 'launch'
    c.adapter._park(s, 'login', reason='session_changed')
    assert s.request_id and not s.external_browser and not s.resume_authorized
    c.state['fail_open'] = False; failed[0] = False
    assert c.adapter._show_intervention_browser(s) is True
    assert len(calls) == (1 if failure == 'launch' else 2)
    assert not s.resume_authorized and not s.uploaded and not s.final


@pytest.mark.parametrize('reason', ['login_required', 'identity_unavailable', 'identity_mismatch'])
def test_initial_login_handoff_opens_a_real_window_without_publication_authority(recovery, reason):
    c = recovery; s = c.session
    old_context, old_page = s.context, s.page
    payload, binding = deepcopy(s.payload), deepcopy(s.binding)
    result = c.adapter._park(s, 'login', reason=reason)
    assert len(launches(c)) == 2
    assert launches(c)[0][1]['headless'] is True
    assert launches(c)[1][1]['headless'] is False
    assert launches(c)[0][0] == launches(c)[1][0]
    assert old_context.closed and old_page.closed
    assert s.context is not old_context and s.page is not old_page
    assert s.payload == payload and s.binding == binding and s.locked
    assert not s.resume_authorized and not s.uploaded and not s.final
    assert result.state == 'paused_auth'
    assert s.browser_login and s.external_browser
    assert len(c.restores) == 1  # Reopening must not overwrite current profile cookies.
    request = s.broker.status()['request']
    assert request['view_mode'] == 'browser' and not request.get('image')
    assert not any(event in {'upload', 'final'} for event, _ in c.events)


def test_repeated_parking_and_explicit_show_reuse_the_same_visible_page(recovery):
    c = recovery; s = c.session
    c.adapter._park(s, 'login')
    page, context, request_id = s.page, s.context, s.request_id
    c.adapter._park(s, 'login')
    assert len(launches(c)) == 2
    assert c.adapter._show_intervention_browser(s) is True
    assert s.page is page and s.context is context and s.request_id == request_id
    assert len(launches(c)) == 2 and not s.resume_authorized


@pytest.mark.parametrize('uploaded,final', [(True, False), (True, True), (False, True)])
def test_uploaded_or_submitted_pages_are_never_recreated_for_login(recovery, uploaded, final):
    c = recovery; s = c.session
    s.uploaded, s.final = uploaded, final
    page, context = s.page, s.context
    c.adapter._park(s, 'login')
    assert not getattr(s, 'browser_login', False)
    assert c.adapter._show_intervention_browser(s) is False
    assert s.page is page and s.context is context and not context.closed
    assert len(launches(c)) == 1


def test_context_close_failure_keeps_profile_lock_and_retries_without_parallel_launch(recovery):
    c = recovery; s = c.session
    old_context = s.context
    c.state['fail_close'] = True
    c.adapter._park(s, 'login')
    assert s.context is old_context and s.locked and not old_context.closed
    assert len(launches(c)) == 1
    assert 'private-cookie-value' not in repr(s.broker.status())
    c.state['fail_close'] = False
    assert c.adapter._show_intervention_browser(s) is True
    assert old_context.closed and len(launches(c)) == 2 and s.locked


def test_failed_browser_launch_leaves_retryable_task_without_reusing_stale_archive(recovery):
    c = recovery; s = c.session
    c.state['fail_open'] = True
    c.adapter._park(s, 'login')
    assert s.context is None and s.page is None and s.locked
    assert s.browser_login and not s.external_browser
    assert 'private-cookie-value' not in repr(s.broker.status())
    assert not s.resume_authorized and s.request_id
    c.state['fail_open'] = False
    assert c.adapter._show_intervention_browser(s) is True
    assert len(c.restores) == 1 and s.external_browser


def test_partial_open_failure_is_closed_before_another_visible_launch(recovery):
    c = recovery; s = c.session
    c.state['fail_navigation'] = True
    c.adapter._park(s, 'login')
    partial = s.context
    assert partial is not None and not partial.closed and not s.external_browser
    assert not s.resume_authorized
    c.state['fail_navigation'] = False
    assert c.adapter._show_intervention_browser(s) is True
    assert partial.closed and len(launches(c)) == 3
    assert len(c.restores) == 1


def test_cancellation_never_launches_a_login_browser(recovery):
    c = recovery; s = c.session
    c.adapter.cancel.set()
    assert c.adapter._show_intervention_browser(s) is False
    assert len(launches(c)) == 1
    assert not c.contexts[0].closed and s.locked


def test_inspection_keeps_its_original_page_and_screenshot_verification(recovery):
    c = recovery; s = c.session
    c.adapter.inspection_only = True
    page, context = s.page, s.context
    c.adapter._park(s, 'login')
    assert not getattr(s, 'browser_login', False)
    assert c.adapter._show_intervention_browser(s) is False
    assert s.page is page and s.context is context and not context.closed
    assert len(launches(c)) == 1
    assert s.broker.status()['request'].get('image')


def test_reverified_identity_clears_login_mode_before_upload(case, monkeypatch):
    adapter, payload, _, events, _, _ = case
    assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
    session = adapter._session(payload)
    session.browser_login = True
    original = adapter._upload
    def upload(s, surface):
        assert not s.browser_login
        return original(s, surface)
    monkeypatch.setattr(adapter, '_upload', upload)
    result = adapter.submit(payload, context=None, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'platform_scheduled'
    adapter.close()


def test_wrong_frozen_uid_cannot_close_or_replace_the_original_context(recovery):
    c = recovery; s = c.session
    s.payload['platform_user_id'] = 'different-finder'
    with pytest.raises(AccountIdentityMismatch): c.adapter._show_intervention_browser(s)
    assert len(launches(c)) == 1 and not c.contexts[0].closed and s.locked


@pytest.mark.parametrize('recovery', ['visible'], indirect=True)
def test_existing_visible_window_is_focused_without_another_context(recovery):
    c = recovery; s = c.session
    # The real _open selected a visible browser at launch, rather than a new
    # visibility policy being applied to an existing background context.
    assert launches(c)[0][1]['headless'] is False and s.external_browser
    page, context = s.page, s.context
    c.adapter._park(s, 'login')
    assert s.page is page and s.context is context and not context.closed
    assert len(launches(c)) == 1 and not s.resume_authorized
    assert any(event == 'front' and value is page for event, value in c.events)


def test_closed_login_window_does_not_authorize_resume_or_claim_logged_in(recovery):
    c = recovery; s = c.session
    c.adapter._park(s, 'login')
    s.page.closed = True
    assert not s.resume_authorized
    assert s.broker.status()['request'] and not s.uploaded and not s.final
    assert c.adapter._show_intervention_browser(s) is True
    assert len(launches(c)) == 3 and not s.resume_authorized


def test_network_blocked_real_browser_click_requires_same_uid_and_explicit_resume(case, monkeypatch):
    """Real Chromium page/window; uploads and final ACK remain owned fixtures."""
    from playwright.sync_api import sync_playwright
    from types import MethodType
    adapter, payload, _, events, _, _ = case
    adapter.step_timeout = 3
    html = '''<!doctype html><meta charset="utf-8"><div id="app"><button class="login-panel">微信快捷登录</button>
      <div class="side-bar-footer"><div id="profile"></div></div></div><script>
      const finder={finderUsername:'finder-test',nickname:'合成视频号'};
      const profile=document.querySelector('#profile');
      document.querySelector('#app').__vue__={$children:[{$el:profile,finder,$children:[]}],$el:document.querySelector('#app')};
      const button=document.querySelector('button');
      function update(){const logged=sessionStorage.syntheticLogged==='yes';
        window._store={userStore:{hasLogin:logged,isExpired:!logged,finder}};button.hidden=logged;}
      button.onclick=()=>{sessionStorage.syntheticLogged='yes';update();};update();
      </script>'''
    options_seen, contexts, requested = [], [], []
    monkeypatch.setattr(adapter, '_open', MethodType(module.ChannelsMultiPublishAdapter._open, adapter))
    monkeypatch.setattr(adapter, '_challenge', MethodType(module.ChannelsMultiPublishAdapter._challenge, adapter))
    monkeypatch.setattr(module.creator_auth, 'restore_session', lambda *args: None)
    with sync_playwright() as driver:
        def launch(profile, **options):
            options_seen.append(dict(options))
            context = driver.chromium.launch_persistent_context(str(profile), **options)
            def route(request):
                requested.append(request.request.url)
                if request.request.is_navigation_request(): request.fulfill(status=200, content_type='text/html', body=html)
                else: request.abort()
            context.route('**/*', route)
            contexts.append(context)
            return context
        adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=lambda: None)
        try:
            first = adapter.validate_target(payload, payload, payload['execution_mode'])
            assert first.state == 'paused_auth'
            session = adapter._session(payload)
            assert [value['headless'] for value in options_seen] == [True, False]
            assert not session.resume_authorized and not session.uploaded and not session.final
            session.page.get_by_role('button', name='微信快捷登录', exact=True).click()
            assert adapter._identity(session) == ('finder-test', '合成视频号')
            with pytest.raises(ValueError, match='explicit same-identity resume required'):
                adapter.resume(payload, context=first.context, before_final=lambda: events.append(('journal', None)))
            assert not any(event in {'upload', 'journal', 'final'} for event, _ in events)
            session.list_seen = True
            adapter.respond(session.request_id, 'done')
            result = adapter.resume(payload, context=first.context, before_final=lambda: events.append(('journal', None)))
            assert result.state == 'platform_scheduled'
            assert [event for event, _ in events if event in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
            assert session.final and not session.browser_login
            assert len(options_seen) == 2 and requested
        finally:
            adapter.close()
            for context in contexts:
                try: context.close()
                except Exception: pass


@pytest.mark.parametrize('surface', ['document', 'shadow'])
def test_visible_quick_login_control_is_detected_before_identity_check(offline_page, surface):
    html = '<div class="login-panel"><button><span>微信快捷登录</span></button></div>'
    if surface == 'shadow': mount_shadow(offline_page, html)
    else: offline_page.set_content(html)
    assert offline_page.evaluate(module.CHALLENGE)['login'] is True


@pytest.mark.parametrize('html', [
    '<div class="login-panel" style="display:none"><button>微信快捷登录</button></div>',
    '<div class="post-create"><div contenteditable="true"><span>微信快捷登录</span></div></div>',
    '<div class="video-title"><span>微信快捷登录</span></div>',
    '<p>微信快捷登录</p>',
])
def test_quick_login_metadata_or_hidden_control_does_not_pause_publication(offline_page, html):
    offline_page.set_content(html)
    assert offline_page.evaluate(module.CHALLENGE)['login'] is False
