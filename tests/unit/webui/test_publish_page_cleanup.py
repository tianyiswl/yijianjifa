"""Dead browser cleanup preserves frozen publishing evidence and authority."""
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_publish_intervention_defer import parked
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui.chromium_publish import account_lock
from yijianjifa.webui.chromium_publish_session import ChromiumPublishSessionAdapter, LiveSession


@pytest.fixture
def page_owner(parked, db_engine, app_paths, tmp_path, monkeypatch):
    account = AccountService(db_engine).create('douyin', '111', 'Synthetic account')
    video_path = tmp_path / 'owned-video.mp4'; video_path.write_bytes(b'synthetic-video')
    video = FinishedVideoService(db_engine).import_external(video_path, duration_ms=1000, title='Synthetic video')
    plan = parked.call('prepare', {'video_ids': [video.id],
        'targets': [{'account_id': account.id, 'platform': 'douyin'}],
        'common_fields': {}, 'execution_mode': 'publish_now'})
    batch = parked.call('confirm', {'prepared_id': plan['prepared_id'],
        'intent_id': plan['request_id'], 'consent_partial': False})
    delivery_id = parked.call('detail', {'id': batch['id']})['deliveries'][0]['id']
    with session_scope(db_engine) as db:
        delivery = db.get(PublishDeliveryRow, delivery_id)
        attempt = db.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery_id))
        attempt.final_json = json.dumps({'request_digest': delivery.request_digest, 'state': 'result_unknown'})
        attempt.result_json = json.dumps({'state': 'result_unknown', 'error_code': 'verification_required'})
        attempt.context_json = json.dumps({'handle': 'original-live-session'})
        db.get(TaskItemRow, delivery.item_id).state = 'result_unknown'
        binding = {'delivery_id': delivery.id, 'task_id': delivery.task_id,
                   'attempt': delivery.attempt, 'digest': delivery.request_digest}
    def evidence():
        with session_scope(db_engine) as db:
            delivery = db.get(PublishDeliveryRow, delivery_id)
            attempt = db.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery_id))
            return (delivery.attempt, delivery.request_digest, delivery.cancel_requested,
                    attempt.owner_hash, attempt.final_json, attempt.result_json, attempt.context_json,
                    db.get(TaskItemRow, delivery.item_id).state)
    adapter = ChromiumPublishSessionAdapter(db_engine, app_paths)
    adapter.platform_id = 'douyin'; adapter._thread()
    events = []; closed = [True]; fail_close = [False]
    def close():
        events.append('close')
        if fail_close[0]: raise RuntimeError('synthetic close failure')
    live = LiveSession({'account_id': account.id, 'platform_user_id': '111'}, binding)
    live.context = SimpleNamespace(close=close)
    live.page = SimpleNamespace(is_closed=lambda: closed[0])
    live.final = True; live.uploaded = True; live.phase = 'result'
    live.request_id = live.broker.request(account.id, 'Synthetic account', 'complex', 'Verification')
    live.locked = True
    lock = account_lock(account.id); assert lock.acquire(False)
    adapter.sessions['owned-page'] = live
    monkeypatch.setattr(adapter, '_step', lambda *args, **kwargs: pytest.fail('cleanup must not execute publishing'))
    monkeypatch.setattr(adapter, '_grant_resume', lambda *args: pytest.fail('cleanup must not grant resume'))
    monkeypatch.setattr(adapter, '_refresh_intervention_frame', lambda *args, **kwargs: False)
    try:
        yield SimpleNamespace(adapter=adapter, live=live, lock=lock, events=events,
                              closed=closed, fail_close=fail_close, evidence=evidence)
    finally:
        if lock.locked(): lock.release()


def test_failed_page_close_retains_original_verification_and_schedules_safe_retry(page_owner):
    case = page_owner; case.fail_close[0] = True
    request = case.live.broker.status()['request']; before = case.evidence()
    with pytest.raises(RuntimeError, match='synthetic close failure'):
        case.adapter._release_page(case.live)
    assert case.live.cleanup_pending is True and case.live.cleanup_retry_at > time.monotonic()
    assert case.live.request_id == request['id']
    assert case.live.broker.status()['request'] == request
    assert case.lock.locked() and case.live.context is not None and case.live.page is not None
    assert case.evidence() == before


@pytest.mark.parametrize('uploaded,final', [(False, False), (True, False), (False, True), (True, True)])
def test_real_closed_page_releases_browser_resources_without_changing_submission_evidence(page_owner, uploaded, final):
    case = page_owner; case.live.uploaded, case.live.final = uploaded, final
    payload, binding, phase = dict(case.live.payload), dict(case.live.binding), case.live.phase
    before = case.evidence()
    assert case.adapter.refresh_interventions() is True
    assert case.events == ['close'] and not case.lock.locked()
    assert case.live.context is None and case.live.page is None and case.live.request_id is None
    assert case.live.broker.status()['request'] is None
    assert (case.live.uploaded, case.live.final) == (uploaded, final)
    assert (case.live.payload, case.live.binding, case.live.phase) == (payload, binding, phase)
    assert case.evidence() == before and not case.live.resume_authorized


@pytest.mark.parametrize('uploaded,final', [(False, False), (True, False), (False, True), (True, True)])
def test_live_page_is_never_disposed_just_because_a_task_is_parked(page_owner, uploaded, final):
    case = page_owner; case.closed[0] = False
    case.live.uploaded, case.live.final = uploaded, final
    request = case.live.broker.status()['request']; before = case.evidence()
    case.adapter.refresh_interventions()
    assert case.events == [] and case.lock.locked()
    assert case.live.context is not None and case.live.page is not None
    assert case.live.broker.status()['request'] == request and case.evidence() == before


def test_close_failure_keeps_recovery_entry_until_a_due_retry_proves_cleanup(page_owner):
    case = page_owner; case.live.uploaded = case.live.final = False
    case.fail_close[0] = True
    request = case.live.broker.status()['request']; before = case.evidence()
    case.adapter.refresh_interventions()
    assert case.live.cleanup_pending and case.live.broker.status()['request'] == request
    assert case.events == ['close'] and case.lock.locked()
    case.adapter.refresh_interventions()
    assert case.events == ['close'], 'The closed-page branch must respect the pending retry delay'
    case.fail_close[0] = False; case.live.cleanup_retry_at = 0
    case.adapter.refresh_interventions()
    assert case.events == ['close', 'close'] and not case.lock.locked()
    assert case.live.broker.status()['request'] is None and case.live.request_id is None
    assert case.evidence() == before and not case.live.resume_authorized
