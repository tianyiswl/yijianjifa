"""Exact failed-target time edits stay local and preserve conflict evidence."""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from test_multi_publish_api import call, fixture, saved
from yijianjifa.app_services.multi_publish_runtime import AUTH
from yijianjifa.features.publish.coordinator import AdapterOutcome
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def evidence(engine, task_id, delivery_id):
    with session_scope(engine) as session:
        row = session.get(PublishDeliveryRow, delivery_id)
        attempts = list(session.scalars(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id == delivery_id).order_by(DeliveryAttemptRow.number)))
        grant = session.get(Setting, AUTH + task_id)
        return {
            'attempt': row.attempt, 'digest': row.request_digest, 'snapshot': row.snapshot_json,
            'mode': row.execution_mode, 'time': row.scheduled_at,
            'attempts': [(a.number, a.snapshot_json, a.result_json, a.final_json) for a in attempts],
            'grant': grant.value_json if grant else None,
        }


def test_reschedule_failed_target_accepts_ui_iso_time_and_reports_conflict_without_mutation(
        fixture, db_engine, monkeypatch):
    api, _, _, adapter = fixture
    runtime = api._multi_publish
    monkeypatch.setattr(runtime, 'wake', lambda: None)
    batch = saved(fixture)
    targets = call(api, 'detail', {'id': batch['id']})['data']['deliveries']
    old, failed = [target['id'] for target in targets]
    tomorrow_noon = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=4, minute=0, second=0, microsecond=0)
    ui_time = tomorrow_noon.isoformat().replace('+00:00', 'Z')
    scheduled = call(api, 'target.reschedule', {
        'delivery_id': old, 'execution_mode': 'platform_schedule', 'scheduled_at': ui_time,
    }, 'old-schedule')
    assert scheduled['ok'] and scheduled['data']['scheduled_at'] == tomorrow_noon.isoformat()

    check = call(api, 'execution.check', {'id': batch['id'], 'delivery_ids': [failed]})['data']
    assert call(api, 'execution.start', {'id': batch['id'], 'delivery_ids': [failed],
        'digest': check['digest'], 'consent': True,
        'consent_partial': check['requires_partial_consent']}, 'start-failed')['ok']
    submissions = []
    adapter.submit = lambda payload, **kwargs: (
        submissions.append(payload['video_id']) or AdapterOutcome('pre_submit_failed', error_code='fixture-before-final'))
    assert runtime.coordinator.run_once(batch['id'], [failed]) == 'pre_submit_failed'
    assert 'reschedule' in call(api, 'detail', {'id': batch['id']})['data']['deliveries'][1]['allowed_actions']
    original_failed = evidence(db_engine, batch['id'], failed)
    original_old = evidence(db_engine, batch['id'], old)
    params = {'delivery_id': failed, 'execution_mode': 'platform_schedule', 'scheduled_at': ui_time}
    rejected = call(api, 'target.reschedule', params, 'retry-exact-time')
    assert not rejected['ok']
    assert rejected['error']['code'] == 'publish_schedule_conflict'
    assert '已有其他发布安排' in rejected['error']['message']
    assert '本次目标未修改' in rejected['error']['message']
    assert evidence(db_engine, batch['id'], failed) == original_failed
    assert evidence(db_engine, batch['id'], old) == original_old

    # Explicitly cancel only the old local reservation, then retry the same UI
    # intent. Refreezing clears authorization; it never starts another upload.
    assert call(api, 'target.cancel', {'delivery_id': old}, 'cancel-old')['ok']
    corrected = call(api, 'target.reschedule', params, 'retry-exact-time')
    assert corrected['ok'], corrected
    assert corrected['data']['attempt'] == 2
    assert corrected['data']['execution_mode'] == 'platform_schedule'
    assert corrected['data']['scheduled_at'] == tomorrow_noon.isoformat()
    assert corrected['data']['state'] == 'waiting'
    assert len(submissions) == 1
    with session_scope(db_engine) as session:
        grant = json.loads(session.get(Setting, AUTH + batch['id']).value_json)
        assert failed not in grant['delivery_ids'] and failed not in grant['targets']
        current = session.scalar(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id == failed, DeliveryAttemptRow.number == 2))
        assert current.owner_hash is None and current.final_json is None
