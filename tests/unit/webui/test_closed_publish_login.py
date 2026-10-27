"""Closing an unused login page releases its owner lease, never resumes work."""
from types import SimpleNamespace

import pytest

from yijianjifa.webui.chromium_publish import account_lock
from yijianjifa.webui.chromium_publish_session import ChromiumPublishSessionAdapter, LiveSession


@pytest.fixture
def closed_login(db_engine, app_paths, monkeypatch):
    adapter = ChromiumPublishSessionAdapter(db_engine, app_paths)
    adapter.platform_id = 'channels'; adapter._thread()
    events = []; closed = [True]; fail_close = [False]
    def close():
        events.append('close')
        if fail_close[0]: raise RuntimeError('synthetic close failure')
    session = LiveSession({'account_id': 'synthetic-closed-account'}, {'delivery_id': 'synthetic-delivery'})
    session.context = SimpleNamespace(close=close)
    session.page = SimpleNamespace(is_closed=lambda: closed[0])
    session.browser_login = True; session.external_browser = True
    session.request_id = session.broker.request('synthetic-closed-account', '合成账号', 'login', '登录')
    session.locked = True
    lock = account_lock(session.payload['account_id']); lock.acquire()
    adapter.sessions['synthetic'] = session
    monkeypatch.setattr(adapter, '_step', lambda *args, **kwargs: pytest.fail('cleanup cannot resume publishing'))
    monkeypatch.setattr(adapter, '_grant_resume', lambda *args: pytest.fail('cleanup cannot grant authority'))
    try:
        yield adapter, session, lock, events, closed, fail_close
    finally:
        if lock.locked(): lock.release()


def test_closed_unused_login_page_releases_account_but_keeps_original_target(closed_login):
    adapter, session, lock, events, _, _ = closed_login
    payload, binding = dict(session.payload), dict(session.binding)
    assert adapter.refresh_interventions()
    assert events == ['close'] and not lock.locked()
    assert session.context is None and session.page is None and session.request_id is None
    assert session.payload == payload and session.binding == binding
    assert adapter.sessions['synthetic'] is session and not session.resume_authorized


def test_live_login_page_keeps_lease_for_explicit_window_reuse(closed_login):
    adapter, session, lock, events, closed, _ = closed_login; closed[0] = False
    adapter.refresh_interventions()
    assert lock.locked() and not events and session.request_id
    assert not session.resume_authorized


@pytest.mark.parametrize('uploaded,final', [(True, False), (False, True), (True, True)])
def test_closed_uploaded_or_submitted_page_releases_resources_but_preserves_original_attempt(closed_login, uploaded, final):
    adapter, session, lock, events, _, _ = closed_login
    session.uploaded, session.final = uploaded, final
    payload,binding=dict(session.payload),dict(session.binding)
    adapter.refresh_interventions()
    assert not lock.locked() and events==['close'] and session.request_id is None
    assert session.context is None and session.page is None
    assert (session.uploaded,session.final)==(uploaded,final)
    assert session.payload==payload and session.binding==binding
    assert session.broker.status()['request'] is None and not session.resume_authorized


def test_close_failure_keeps_lease_until_owner_cleanup_succeeds(closed_login):
    adapter, session, lock, events, _, fail_close = closed_login; fail_close[0] = True
    adapter.refresh_interventions()
    assert lock.locked() and session.context is not None and session.cleanup_pending
    fail_close[0] = False; session.cleanup_retry_at = 0
    adapter.refresh_interventions()
    assert not lock.locked() and session.context is None and not session.cleanup_pending
    assert not session.resume_authorized
