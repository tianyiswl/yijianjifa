"""Resume observes navigation without replaying any publishing action."""
import json

import pytest
from playwright.sync_api import Error as PlaywrightError

from test_channels_multi_publish import case


def parked(case, monkeypatch):
    adapter, payload, page, events, _, _ = case
    monkeypatch.setattr(adapter, '_challenge', lambda s: 'login')
    first = adapter.validate_target(payload, payload, payload['execution_mode'])
    assert first.state == 'paused_auth'
    adapter.respond(adapter.interventions()[0]['id'], 'done')
    return adapter, payload, page, events, first


@pytest.mark.parametrize('message', [
    'Execution context was destroyed because of a navigation', 'Frame was detached',
])
def test_done_navigation_settles_on_original_page_then_submits_once(case, monkeypatch, message):
    adapter, payload, page, events, first = parked(case, monkeypatch);calls = []
    def moving(session):
        calls.append(session.page)
        if len(calls) == 1: raise PlaywrightError(message)
        return None
    monkeypatch.setattr(adapter, '_challenge', moving)
    result = adapter.resume(payload, context=first.context, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'platform_scheduled' and all(p is page for p in calls)
    assert [kind for kind, _ in events if kind in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
    adapter.close()


def test_done_persistent_navigation_keeps_request_and_page_without_upload_or_final(case, monkeypatch):
    adapter, payload, page, events, first = parked(case, monkeypatch)
    def moving(session): raise PlaywrightError('Execution context was destroyed because of a navigation SECRET')
    monkeypatch.setattr(adapter, '_challenge', moving)
    result = adapter.resume(payload, context=first.context, before_final=lambda: pytest.fail('must not grant final'))
    assert result.state == 'paused_auth' and result.context == first.context
    session = adapter._session(payload)
    assert session.page is page and not session.final and not session.resume_authorized
    assert not any(kind in {'upload', 'final', 'close'} for kind, _ in events)
    assert '尚未提交' in adapter.interventions()[0]['message']
    assert 'SECRET' not in json.dumps(adapter.interventions())
    monkeypatch.setattr(adapter, '_challenge', lambda s: None)
    with pytest.raises(ValueError, match='explicit same-identity resume required'):
        adapter.resume(payload, context=first.context, before_final=lambda: pytest.fail('must not grant final'))
    adapter.respond(adapter.interventions()[0]['id'], 'done')
    assert adapter.resume(payload, context=first.context, before_final=lambda: None).state == 'platform_scheduled'
    assert sum(kind == 'upload' for kind, _ in events) == sum(kind == 'final' for kind, _ in events) == 1
    adapter.close()


def test_done_ordinary_error_is_not_retried_and_has_safe_verification_diagnostic(case, monkeypatch):
    adapter, payload, _, events, first = parked(case, monkeypatch);calls = []
    def fail(session):
        calls.append(1);raise RuntimeError('Execution context was destroyed SECRET https://private.invalid')
    monkeypatch.setattr(adapter, '_challenge', fail)
    with pytest.raises(RuntimeError) as caught:
        adapter.resume(payload, context=first.context, before_final=lambda: pytest.fail('must not grant final'))
    assert calls == [1] and not any(kind in {'upload', 'final'} for kind, _ in events)
    assert adapter.failure_diagnostic(caught.value) == {'error_stage': 'verification_observation', 'error_category': 'execution_failed'}
    assert adapter.failure_diagnostic(caught.value) == {}
    adapter.close()


@pytest.mark.parametrize('source', ['challenge', 'identity'])
def test_advance_navigation_observation_does_not_replay_upload_or_form(case, monkeypatch, source):
    adapter, payload, _, events, first = parked(case, monkeypatch)
    calls = [];original_identity = adapter._identity
    def challenge(session):
        calls.append('challenge')
        # The first observation belongs to consuming done, the next to advance.
        if source == 'challenge' and len(calls) == 2: raise PlaywrightError('Frame has been detached')
        return None
    identity_calls = []
    def identity(session):
        identity_calls.append(1)
        if source == 'identity' and len(identity_calls) == 1: raise PlaywrightError('Cannot find context with specified id')
        return original_identity(session)
    monkeypatch.setattr(adapter, '_challenge', challenge);monkeypatch.setattr(adapter, '_identity', identity)
    forms = [];original_prepare = adapter._prepare_form
    def prepare(*args): forms.append(1);return original_prepare(*args)
    monkeypatch.setattr(adapter, '_prepare_form', prepare)
    assert adapter.resume(payload, context=first.context, before_final=lambda: None).state == 'platform_scheduled'
    assert forms == [1]
    assert sum(kind == 'upload' for kind, _ in events) == sum(kind == 'final' for kind, _ in events) == 1
    adapter.close()


def test_post_final_done_navigation_keeps_original_result_page_and_never_reuploads(case, monkeypatch):
    adapter, payload, page, events, _, _ = case
    monkeypatch.setattr(adapter, '_click_final', lambda s, surface: events.append(('final', None)))
    monkeypatch.setattr(adapter, '_challenge', lambda s: 'complex' if s.final else None)
    assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
    first = adapter.submit(payload, context=None, before_final=lambda: None)
    assert first.state == 'result_unknown'
    adapter.respond(adapter.interventions()[0]['id'], 'done')
    def moving(session): raise PlaywrightError('Execution context was destroyed because of a navigation SECRET')
    monkeypatch.setattr(adapter, '_challenge', moving)
    result = adapter.reconcile(payload, context=first.context)
    assert result.state == 'result_unknown' and adapter._session(payload).page is page
    assert adapter._session(payload).final and adapter.interventions()
    assert sum(kind == 'upload' for kind, _ in events) == sum(kind == 'final' for kind, _ in events) == 1
    adapter.close()


def test_bound_ack_received_during_unready_verification_still_wins(case, monkeypatch):
    from test_channels_multi_publish import normal_request, response
    from yijianjifa.webui import channels_publish_form as form
    adapter, payload, _, events, _, media = case
    monkeypatch.setattr(adapter, '_click_final', lambda s, surface: events.append(('final', None)))
    monkeypatch.setattr(adapter, '_challenge', lambda s: 'complex' if s.final else None)
    assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
    first = adapter.submit(payload, context=None, before_final=lambda: None)
    assert first.state == 'result_unknown'
    adapter.respond(adapter.interventions()[0]['id'], 'done')
    def moving(session):
        adapter._response(session, response(form.CREATE_PATH, normal_request(form.validate_form(payload), media),
            {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '9007199254740993'}}))
        raise PlaywrightError('Execution context was destroyed because of a navigation')
    monkeypatch.setattr(adapter, '_challenge', moving)
    monkeypatch.setattr(adapter, '_refresh_after_receipt', lambda s: None)
    result = adapter.reconcile(payload, context=first.context)
    assert result.state == 'platform_scheduled' and result.receipt_reference == {'work_id': '9007199254740993'}
    assert sum(kind == 'upload' for kind, _ in events) == sum(kind == 'final' for kind, _ in events) == 1
    assert not adapter.interventions()
    adapter.close()


def test_before_final_failure_observes_navigation_without_retrying_final_authority(case, monkeypatch):
    adapter, payload, page, events, _, _ = case
    assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
    authority = []
    def denied():
        authority.append(1)
        raise ValueError('final authority rejected')
    original_prepare = adapter._prepare_form
    def prepare(*args):
        ready = original_prepare(*args)
        def moving(session): raise PlaywrightError('Frame was detached')
        monkeypatch.setattr(adapter, '_challenge', moving)
        return ready
    monkeypatch.setattr(adapter, '_prepare_form', prepare)
    result = adapter.submit(payload, context=None, before_final=denied)
    assert result.state == 'paused_auth' and adapter._session(payload).page is page
    assert authority == [1] and sum(kind == 'upload' for kind, _ in events) == 1
    assert not any(kind in {'final', 'close'} for kind, _ in events)
    adapter.close()


def test_done_closed_original_page_fails_without_upload_or_final(case, monkeypatch):
    adapter, payload, page, events, first = parked(case, monkeypatch)
    monkeypatch.setattr(page, 'is_closed', lambda: True)
    with pytest.raises(ValueError, match='publish page closed') as caught:
        adapter.resume(payload, context=first.context, before_final=lambda: pytest.fail('must not grant final'))
    assert adapter.failure_diagnostic(caught.value) == {'error_stage': 'verification_observation', 'error_category': 'page_closed'}
    assert not any(kind in {'upload', 'final'} for kind, _ in events)
    assert adapter._session(payload).page is None and not adapter.interventions()
    adapter.close()


@pytest.mark.parametrize('post_final', [False, True])
def test_ordinary_done_failure_releases_only_pre_final_page_and_account_lock(case, monkeypatch, post_final):
    from yijianjifa.webui.chromium_publish import account_lock
    adapter, payload, page, events, _, _ = case
    if post_final:
        monkeypatch.setattr(adapter, '_click_final', lambda s, surface: events.append(('final', None)))
        monkeypatch.setattr(adapter, '_challenge', lambda s: 'complex' if s.final else None)
        assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
        first = adapter.submit(payload, context=None, before_final=lambda: None)
        adapter.respond(adapter.interventions()[0]['id'], 'done')
    else:
        adapter, payload, page, events, first = parked(case, monkeypatch)
    session = adapter._session(payload)
    lock = account_lock(payload['account_id'])
    assert lock.acquire(blocking=False)
    session.locked = True
    def fail(s): raise RuntimeError('synthetic ordinary failure')
    monkeypatch.setattr(adapter, '_challenge', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic ordinary failure'):
            if post_final: adapter.reconcile(payload, context=first.context)
            else: adapter.resume(payload, context=first.context, before_final=lambda: pytest.fail('must not grant final'))
        if post_final:
            assert session.page is page and session.context is not None and session.locked
            assert adapter.interventions() and not any(kind == 'close' for kind, _ in events)
        else:
            assert session.page is None and session.context is None and not session.locked
            assert not adapter.interventions() and any(kind == 'close' for kind, _ in events)
            assert lock.acquire(blocking=False)
            lock.release()
    finally:
        adapter.close()
