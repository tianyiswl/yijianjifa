"""A settled background result releases resources, never submission evidence."""
from types import SimpleNamespace

import pytest

from yijianjifa.features.publish.coordinator import AdapterOutcome
from yijianjifa.webui.chromium_publish import account_lock
from yijianjifa.webui.chromium_publish_session import ChromiumPublishSessionAdapter, LiveSession


@pytest.fixture
def settled(db_engine, app_paths, monkeypatch):
    adapter = ChromiumPublishSessionAdapter(db_engine, app_paths)
    adapter.platform_id = 'channels'; adapter._thread()
    events = []; fail_close = [False]
    def close():
        events.append('close')
        if fail_close[0]: raise RuntimeError('synthetic close failure')
    live = LiveSession({'account_id': 'synthetic-settled-account'},
                       {'delivery_id': 'synthetic-settled-delivery', 'attempt': 1})
    live.context = SimpleNamespace(close=close)
    live.page = SimpleNamespace(is_closed=lambda: False)
    live.final = live.uploaded = live.locked = True
    live.phase = 'result'
    adapter.sessions[live.handle] = live
    outcome = AdapterOutcome('result_unknown', context={'handle': live.handle}, error_code='result_unknown')
    monkeypatch.setattr(adapter, '_advance', lambda *args, **kwargs: outcome, raising=False)
    monkeypatch.setattr(adapter, '_trace', lambda *args, **kwargs: None)
    lock = account_lock(live.payload['account_id']); assert lock.acquire(False)
    try:
        yield SimpleNamespace(adapter=adapter, live=live, events=events, fail_close=fail_close,
                              lock=lock, outcome=outcome)
    finally:
        if lock.locked(): lock.release()


def test_unknown_result_requires_platform_proof_before_releasing_original_page(settled):
    case = settled
    assert case.adapter._step(case.live) is case.outcome
    assert not case.events and case.lock.locked() and case.live.context is not None


def test_proven_settled_result_releases_account_without_granting_another_submission(settled, monkeypatch):
    case = settled
    monkeypatch.setattr(case.adapter, '_result_page_releasable', lambda s: True, raising=False)
    payload, binding = dict(case.live.payload), dict(case.live.binding)
    assert case.adapter._step(case.live) is case.outcome
    assert case.events == ['close'] and not case.lock.locked()
    assert case.live.context is None and case.live.page is None
    assert case.live.final and case.live.uploaded and not case.live.resume_authorized
    assert case.live.payload == payload and case.live.binding == binding
    monkeypatch.setattr(case.adapter, '_session', lambda *args, **kwargs: case.live)
    with pytest.raises(ValueError, match='only query is allowed'):
        case.adapter.submit(payload, context={'handle': case.live.handle},
                            before_final=lambda: pytest.fail('must not authorize another post'))


def test_verification_pause_keeps_original_page_even_if_result_hook_is_true(settled, monkeypatch):
    case = settled
    monkeypatch.setattr(case.adapter, '_result_page_releasable', lambda s: True, raising=False)
    paused = AdapterOutcome('paused_auth', context={'handle': case.live.handle}, error_code='verification_required')
    monkeypatch.setattr(case.adapter, '_advance', lambda *args, **kwargs: paused)
    assert case.adapter._step(case.live) is paused
    assert not case.events and case.lock.locked() and case.live.context is not None


def test_settled_close_failure_keeps_lock_until_owner_retry_succeeds(settled, monkeypatch):
    case = settled; case.fail_close[0] = True
    monkeypatch.setattr(case.adapter, '_result_page_releasable', lambda s: True, raising=False)
    assert case.adapter._step(case.live) is case.outcome
    assert case.live.cleanup_pending and case.lock.locked() and case.live.context is not None
    case.fail_close[0] = False; case.live.cleanup_retry_at = 0
    assert case.adapter.account_available(case.live.payload)
    assert case.events == ['close', 'close'] and not case.lock.locked()
    assert case.live.final and case.live.uploaded and case.live.context is None


@pytest.mark.parametrize('settled_proof', [True, False])
def test_post_final_observation_error_uses_same_resource_proof(settled, monkeypatch, settled_proof):
    case = settled
    monkeypatch.setattr(case.adapter, '_result_page_releasable', lambda s: settled_proof, raising=False)
    def fail_observation(*args, **kwargs):
        raise ValueError('synthetic observation error')
    monkeypatch.setattr(case.adapter, '_advance', fail_observation)
    with pytest.raises(ValueError, match='synthetic observation error'):
        case.adapter._step(case.live, query=True)
    assert case.lock.locked() is not settled_proof
    assert case.events == (['close'] if settled_proof else [])
    assert case.live.final and case.live.uploaded and not case.live.resume_authorized


def test_failed_close_never_masks_original_post_final_error(settled, monkeypatch):
    case = settled; case.fail_close[0] = True
    monkeypatch.setattr(case.adapter, '_result_page_releasable', lambda s: True, raising=False)
    def fail_observation(*args, **kwargs):
        raise ValueError('synthetic observation error')
    monkeypatch.setattr(case.adapter, '_advance', fail_observation)
    with pytest.raises(ValueError, match='synthetic observation error'):
        case.adapter._step(case.live, query=True)
    assert case.live.cleanup_pending and case.lock.locked() and case.events == ['close']
    assert case.live.final and case.live.uploaded and case.live.context is not None
