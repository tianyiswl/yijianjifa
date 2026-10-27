"""A native login handoff only shows the bound browser; it never resumes work."""
from types import SimpleNamespace

import pytest

from yijianjifa.webui.chromium_publish_session import ChromiumPublishSessionAdapter, LiveSession
from yijianjifa.webui.publish_intervention import InterventionBroker


@pytest.fixture
def native_handoff(db_engine, app_paths, monkeypatch):
    adapter = ChromiumPublishSessionAdapter(db_engine, app_paths)
    adapter.platform_id = 'channels'
    adapter._thread()
    session = LiveSession({'account_id': 'account'}, {'delivery_id': 'delivery'})
    session.browser_login = True
    session.external_browser = False
    session.request_id = session.broker.request('account', '合成账号', 'login', '请重新登录')
    adapter.sessions['fixture'] = session
    opened = []

    def show(s):
        opened.append(s)
        s.page = SimpleNamespace(is_closed=lambda: False)
        s.external_browser = True
        return True

    monkeypatch.setattr(adapter, '_show_intervention_browser', show, raising=False)
    monkeypatch.setattr(adapter, '_grant_resume', lambda s: pytest.fail('display must not authorize resume'))
    monkeypatch.setattr(adapter, '_step', lambda *a, **kw: pytest.fail('display must not run publishing'))
    monkeypatch.setattr(adapter, '_refresh_intervention_frame', lambda *a, **kw: pytest.fail('native login must not take screenshots'))
    return adapter, session, opened


def test_opening_native_login_without_a_page_only_displays_bound_session(native_handoff):
    adapter, session, opened = native_handoff
    token = session.request_id
    accepted = adapter.respond(token, 'visible')
    assert adapter.process_verification_view('delivery', token, accepted['view_revision']) == {'accepted': True}
    request = session.broker.status()['request']
    assert opened == [session]
    assert request['view_state'] == 'ready' and request['view_mode'] == 'browser'
    assert request['external_browser'] is True and 'image' not in request
    assert session.request_id == token and not session.resume_authorized and not session.final
    exposed = adapter.interventions()[0]
    assert exposed['browser_login'] is True and exposed['external_browser'] is True


def test_failed_native_open_is_retryable_without_claiming_success(native_handoff, monkeypatch):
    adapter, session, opened = native_handoff
    monkeypatch.setattr(adapter, '_show_intervention_browser', lambda s: False)
    first = adapter.respond(session.request_id, 'visible')
    assert adapter.process_verification_view('delivery', session.request_id, first['view_revision']) == {'accepted': False}
    request = session.broker.status()['request']
    assert request['view_state'] == 'failed' and '浏览器' in request['view_error']
    assert not request.get('external_browser') and not session.resume_authorized
    assert session.request_id and not opened


def test_browser_failure_never_exposes_platform_session_details(native_handoff, monkeypatch):
    adapter, session, opened = native_handoff
    def failed(s):
        raise RuntimeError('private-session-data')
    monkeypatch.setattr(adapter, '_show_intervention_browser', failed)
    accepted = adapter.respond(session.request_id, 'visible')
    assert adapter.process_verification_view('delivery', session.request_id, accepted['view_revision']) == {'accepted': False}
    error = session.broker.status()['request']['view_error']
    assert '浏览器' in error and 'private-session-data' not in error
    assert session.request_id and not opened


@pytest.mark.parametrize('action,code', [('pointer', [1, 2]), ('scroll', [0, 450]), ('refresh', '')])
def test_native_login_cannot_receive_stale_screenshot_actions(native_handoff, action, code):
    adapter, session, opened = native_handoff
    with pytest.raises(ValueError, match='浏览器'):
        adapter.respond(session.request_id, action, code)
    assert session.broker.consume(session.request_id) is None
    assert not opened and not session.resume_authorized


def test_native_browser_state_is_nonce_bound_and_discards_old_frame():
    broker = InterventionBroker()
    token = broker.request('account', '合成账号', 'login', '重新登录')
    broker.frame(token, 'stale-frame')
    broker.open_browser('other-request')
    assert broker.status()['request']['image'] == 'stale-frame'
    broker.open_browser(token)
    request = broker.status()['request']
    assert request['view_mode'] == 'browser' and request['external_browser'] is True
    assert 'image' not in request
    assert request['view_revision'] == 1 and request['view_state'] == 'ready'


def test_automatic_browser_failure_is_reported_without_creating_an_action():
    broker = InterventionBroker()
    token = broker.request('account', '合成账号', 'login', '重新登录')
    broker.browser_failure(token, '未能打开登录浏览器，请重试；原任务仍已暂停。')
    request = broker.status()['request']
    assert request['browser_login'] is True and request['external_browser'] is False
    assert request['view_revision'] == 1 and request['view_state'] == 'failed'
    assert '浏览器' in request['view_error'] and broker.consume(token) is None
