"""Initial creator-page navigation is bounded and never grants submission."""
import json
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from test_channels_multi_publish import case, module
from yijianjifa.features.publish.coordinator import AdapterOutcome


@pytest.mark.parametrize('source', ['challenge', 'identity'])
@pytest.mark.parametrize('message', [
    'Execution context was destroyed, most likely because of a navigation',
    'Cannot find context with specified id',
    'Frame was detached',
])
def test_transient_navigation_observation_can_settle_without_reopening(case, monkeypatch, source, message):
    adapter, payload, page, events, _, _ = case
    method = '_challenge' if source == 'challenge' else '_identity'
    original = getattr(adapter, method); calls = []
    def changing(session):
        calls.append(session.page)
        if len(calls) == 1: raise PlaywrightError(message + ' SECRET https://private.invalid')
        return original(session)
    monkeypatch.setattr(adapter, method, changing)
    result = adapter.validate_target(payload, payload, payload['execution_mode'])
    assert result.authorized and result.observed_uid == payload['platform_user_id']
    assert len(calls) == 2 and all(p is page for p in calls)
    assert not any(kind in {'upload', 'final', 'close'} for kind, _ in events)
    assert adapter._session(payload).page is page and not adapter._session(payload).final
    assert adapter.failure_diagnostic(RuntimeError('unrelated later failure')) == {}
    adapter.close()


def test_persistent_navigation_observation_parks_original_page_and_requires_explicit_resume(case, monkeypatch):
    adapter, payload, page, events, _, _ = case
    def moving(session): raise PlaywrightError('Execution context was destroyed because of a navigation SECRET')
    monkeypatch.setattr(adapter, '_challenge', moving)
    result = adapter.validate_target(payload, payload, payload['execution_mode'])
    assert isinstance(result, AdapterOutcome) and result.state == 'paused_auth'
    session = adapter._session(payload)
    assert session.page is page and session.context is not None and not session.final
    assert '尚未提交' in adapter.interventions()[0]['message']
    assert adapter.interventions()[0]['reason'] == 'identity_unavailable'
    assert 'SECRET' not in json.dumps(adapter.interventions())
    assert not any(kind in {'upload', 'final', 'close'} for kind, _ in events)
    with pytest.raises(ValueError, match='explicit same-identity resume required'):
        adapter.resume(payload, context=result.context, before_final=lambda: pytest.fail('must not submit'))
    adapter.close()


def test_closed_page_is_not_treated_as_navigation_or_parked(case, monkeypatch):
    adapter, payload, page, events, _, _ = case
    monkeypatch.setattr(page, 'is_closed', lambda: True)
    with pytest.raises(ValueError, match='publish page closed') as caught:
        adapter.validate_target(payload, payload, payload['execution_mode'])
    assert adapter.failure_diagnostic(caught.value) == {'error_stage': 'account_observation', 'error_category': 'page_closed'}
    assert adapter._session(payload).page is None and adapter.interventions() == []
    assert not any(kind in {'upload', 'final'} for kind, _ in events)
    assert sum(kind == 'close' for kind, _ in events) == 1
    adapter.close()


def test_unrelated_exception_with_navigation_words_fails_without_retry(case, monkeypatch):
    adapter, payload, _, events, _, _ = case; calls = []
    def fail(session):
        calls.append(1); raise RuntimeError('Execution context was destroyed SECRET https://private.invalid')
    monkeypatch.setattr(adapter, '_challenge', fail)
    with pytest.raises(RuntimeError) as caught:
        adapter.validate_target(payload, payload, payload['execution_mode'])
    diagnostic = adapter.failure_diagnostic(caught.value)
    assert diagnostic == {'error_stage': 'account_observation', 'error_category': 'execution_failed'}
    assert calls == [1] and 'SECRET' not in json.dumps(diagnostic)
    assert sum(kind == 'close' for kind, _ in events) == 1
    assert not any(kind in {'upload', 'final'} for kind, _ in events)
    assert adapter.failure_diagnostic(caught.value) == {}
    adapter.close()


@pytest.mark.parametrize('operation,message,expected_stage,expected_category', [
    ('launch', 'Failed to create a ProcessSingleton for your profile directory SECRET', 'page_open', 'profile_in_use'),
    ('launch', 'Browser process exited SECRET https://private.invalid', 'page_open', 'browser_launch_failed'),
    ('restore', 'invalid session archive', 'session_restore', 'session_unavailable'),
    ('navigate', 'net::ERR_CONNECTION_CLOSED SECRET https://private.invalid', 'page_navigation', 'execution_failed'),
])
def test_open_failures_keep_only_finite_diagnostics_and_no_final(case, monkeypatch, tmp_path,
        operation, message, expected_stage, expected_category):
    adapter, payload, page, events, _, _ = case
    monkeypatch.setattr(adapter, '_open', module.ChannelsMultiPublishAdapter._open.__get__(adapter))
    def launch(*args, **kwargs):
        if operation == 'launch': raise PlaywrightError(message)
        return context
    context = SimpleNamespace(pages=[page], route=lambda *args: None, set_default_timeout=lambda value: None,
        close=lambda: events.append(('close', None)))
    adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=lambda: None)
    monkeypatch.setattr(module.creator_auth, 'chromium_profile', lambda *args: tmp_path / 'isolated-profile')
    def restore(*args):
        if operation == 'restore': raise ValueError(message)
    monkeypatch.setattr(module.creator_auth, 'restore_session', restore)
    monkeypatch.setattr(page, 'on', lambda *args: None, raising=False)
    def navigate(*args, **kwargs):
        if operation == 'navigate': raise PlaywrightError(message)
    monkeypatch.setattr(page, 'goto', navigate)
    with pytest.raises((PlaywrightError, ValueError)) as caught:
        adapter.validate_target(payload, payload, payload['execution_mode'])
    diagnostic = adapter.failure_diagnostic(caught.value)
    assert diagnostic == {'error_stage': expected_stage, 'error_category': expected_category}
    assert 'SECRET' not in json.dumps(diagnostic)
    assert adapter._session(payload).page is None and adapter.interventions() == []
    assert not adapter._session(payload).locked
    assert not any(kind in {'upload', 'final'} for kind, _ in events)
    adapter.close()
