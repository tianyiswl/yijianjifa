"""Local delivery completion is shared; public visibility stays separate.

All adapters, content, account identities and databases are synthetic. No
browser, real account or platform is used by this suite.
"""
from datetime import datetime, timedelta, timezone

import pytest

from yijianjifa.app_services.multi_publish_dto import detail
from yijianjifa.app_services.task_center_access import TaskCenterAccess
from yijianjifa.features.publish.coordinator import AdapterOutcome, PublishCoordinator
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, DeliveryAttemptRow
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.storage.db import session_scope
from test_multi_publish_v2 import setup_v2, prepare_save
from test_image_post_publishing import image_setup


COMPLETE = ('published', 'platform_scheduled', 'platform_accepted', 'processing', 'reviewing')


def outcome(state, payload):
    return AdapterOutcome(state,
        scheduled_at=payload['scheduled_at'] if state == 'platform_scheduled' else None,
        receipt_reference={'work_id': 'synthetic-receipt'} if state in COMPLETE else None)


def set_states(service, batch_id, states, *, paused=False):
    """Seed saved lifecycle states only; this does not execute a submission."""
    children = service.get_task(batch_id)['deliveries']
    assert len(children) == len(states)
    with session_scope(service.engine) as db:
        if paused:
            db.get(TaskCenterRow, batch_id).state = db.get(PublishTaskRow, batch_id).state = 'paused'
        for child, state in zip(children, states):
            row = db.get(PublishDeliveryRow, child['id'])
            db.get(TaskItemRow, row.item_id).state = state
        service.deliveries.aggregate(db, batch_id)


@pytest.mark.parametrize('content_type', ['video', 'image_post'])
@pytest.mark.parametrize('state', COMPLETE)
def test_video_and_image_delivery_completion_preserves_platform_and_public_counts(
        request, db_engine, content_type, state):
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0, microsecond=0).isoformat()
    if content_type == 'video':
        case = request.getfixturevalue('setup_v2')
        service, _, _, adapters = case
        receipt = prepare_save(case, execution_mode='platform_schedule' if state == 'platform_scheduled' else 'publish_now',
                               **({'scheduled_at': when} if state == 'platform_scheduled' else {}))
        batch_id = receipt['id']; coordinator = PublishCoordinator(service)
        for adapter in adapters.values():
            def submit(payload, *, context, before_final, adapter=adapter):
                adapter.calls.append(('submit', payload['video_id'])); before_final()
                return outcome(state, payload)
            adapter.submit = submit
    else:
        publisher, params, _, _, adapter, runtime = request.getfixturevalue('image_setup')
        if state == 'platform_scheduled': params = {**params, 'mode': 'exact', 'start': when}
        def submit(payload, *, context, before_final):
            adapter.calls.append(payload); before_final()
            return outcome(state, payload)
        adapter.submit = submit
        receipt = publisher.submit(params, 'synthetic-completion')
        batch_id = receipt['task_id']; service = runtime.service; coordinator = runtime.coordinator
    quantity = service.get_task(batch_id)['quantity']
    for _ in range(quantity): coordinator.run_once(batch_id)
    result = detail(service, batch_id)
    assert result['state'] == 'completed'
    assert result['completed'] == quantity
    assert [child['state'] for child in result['deliveries']] == [state] * quantity
    assert all(child['reason_message'] == '' for child in result['deliveries'])
    assert result['accepted_count'] == (0 if state == 'published' else quantity)
    assert result['published_count'] == (quantity if state == 'published' else 0)
    assert result['summary']['ready'] == 0 and result['summary']['blocked'] == 0
    assert not any(child['can_submit'] for child in result['deliveries'])
    expected_label = {'published':'已公开', 'platform_scheduled':'平台已接收定时',
                      'platform_accepted':'平台已接收', 'processing':'平台处理中', 'reviewing':'平台审核中'}[state]
    assert result['status_label'] == expected_label
    center = TaskCenterAccess(db_engine)
    record = center.detail(batch_id)['task']
    assert record['completed'] == quantity and record['published_count'] == result['published_count']
    assert record['status_label'] == expected_label
    assert center.list('', 'action', '', 0, 100)['total'] == 0
    assert coordinator.run_once(batch_id) is None
    if content_type == 'video':
        assert all(len(adapter.calls) == 1 for adapter in adapters.values())
    else:
        assert len(adapter.calls) == 1


@pytest.mark.parametrize('failure', ['pre_submit_failed', 'result_unknown', 'paused_auth',
                                    'interrupted', 'waiting_configuration', 'missed_schedule'])
def test_one_unresolved_target_still_requires_handling_and_counts_only_completed_deliveries(setup_v2, failure):
    receipt = prepare_save(setup_v2); service = setup_v2[0]
    states = ['platform_accepted', 'reviewing', 'published', failure]
    set_states(service, receipt['id'], states)
    result = detail(service, receipt['id'])
    assert result['state'] == 'action_required' and result['completed'] == 3
    assert [child['state'] for child in result['deliveries']] == states
    assert result['accepted_count'] == 2 and result['published_count'] == 1
    assert result['summary']['blocked'] == 1 and result['summary']['ready'] == 0


@pytest.mark.parametrize('remaining', ['platform_rejected', 'cancelled'])
def test_completed_deliveries_mixed_with_rejection_or_cancellation_are_not_all_successful(setup_v2, remaining):
    receipt = prepare_save(setup_v2); service = setup_v2[0]
    set_states(service, receipt['id'], ['platform_accepted', 'processing', 'published', remaining])
    result = detail(service, receipt['id'])
    assert result['state'] == 'completed_with_errors' and result['completed'] == 3
    assert result['published_count'] == 1 and result['accepted_count'] == 2
    assert result['status_label'] == '发布结束，有未完成项'


@pytest.mark.parametrize('remaining', ['waiting', 'running'])
def test_active_delivery_is_not_finished_by_completed_siblings(setup_v2, remaining):
    receipt = prepare_save(setup_v2); service = setup_v2[0]
    set_states(service, receipt['id'], ['platform_accepted', 'processing', 'published', remaining])
    result = service.get_task(receipt['id'])
    assert result['state'] == remaining and result['completed'] == 3


def test_an_explicit_batch_pause_is_preserved_when_all_deliveries_are_accepted(setup_v2):
    receipt = prepare_save(setup_v2); service = setup_v2[0]
    set_states(service, receipt['id'], ['platform_accepted'] * 4, paused=True)
    result = service.get_task(receipt['id'])
    assert result['state'] == 'paused' and result['completed'] == 4


def test_a_real_saved_error_is_not_erased_by_a_completed_platform_state(setup_v2):
    import json
    from sqlalchemy import select
    receipt = prepare_save(setup_v2); service = setup_v2[0]
    set_states(service, receipt['id'], ['platform_accepted'] * 4)
    child = service.get_task(receipt['id'])['deliveries'][0]
    with session_scope(service.engine) as db:
        attempt = db.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == child['id']))
        attempt.result_json = json.dumps({'state':'platform_accepted', 'error_code':'verification_required',
                                         'receipt_reference':{'work_id':'synthetic-saved-receipt'}})
    result = detail(service, receipt['id'])
    assert result['deliveries'][0]['error_code'] == 'verification_required'
    assert result['deliveries'][0]['reason_message']


def completed_video_receipt(case, state):
    service, _, _, adapters = case
    receipt = prepare_save(case)
    for adapter in adapters.values():
        def submit(payload, *, context, before_final):
            before_final(); return outcome(state, payload)
        adapter.submit = submit
    coordinator = PublishCoordinator(service)
    for _ in adapters: coordinator.run_once(receipt['id'])
    return service, receipt['id']


def saved_evidence(service, batch_id):
    """Compare all parent, child, snapshot and receipt columns across a read."""
    from sqlalchemy import select
    from yijianjifa.features.publish.history import PublishHistoryRow
    with session_scope(service.engine) as db:
        deliveries = list(db.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == batch_id).order_by(PublishDeliveryRow.id)))
        rows = [db.get(TaskCenterRow, batch_id), db.get(PublishTaskRow, batch_id)]
        for delivery in deliveries:
            rows.extend([delivery, db.get(TaskItemRow, delivery.item_id), db.get(PublishHistoryRow, delivery.history_id)])
            rows.extend(db.scalars(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery.id).order_by(DeliveryAttemptRow.number)))
        return [(row.__tablename__, tuple(getattr(row, column.key) for column in row.__table__.columns)) for row in rows]


@pytest.mark.parametrize('state', ['platform_accepted', 'processing', 'reviewing'])
@pytest.mark.parametrize('old_parent', ['action_required', 'paused'])
def test_old_completed_video_receipts_project_correctly_without_rewriting_saved_evidence(setup_v2, state, old_parent):
    service, batch_id = completed_video_receipt(setup_v2, state)
    with session_scope(service.engine) as db:
        db.get(TaskCenterRow, batch_id).state = db.get(PublishTaskRow, batch_id).state = old_parent
    original = saved_evidence(service, batch_id)
    result = detail(service, batch_id)
    assert result['state'] == ('completed' if old_parent == 'action_required' else 'paused')
    assert result['completed'] == 4 and result['published_count'] == 0 and result['accepted_count'] == 4
    assert all(child['state'] == state for child in result['deliveries'])
    if old_parent == 'action_required':
        assert result['status_label'] == {'platform_accepted':'平台已接收', 'processing':'平台处理中', 'reviewing':'平台审核中'}[state]
        assert TaskCenterAccess(service.engine).list('', 'action', '', 0, 100)['total'] == 0
    assert saved_evidence(service, batch_id) == original


@pytest.mark.parametrize('unresolved', ['result_unknown', 'pre_submit_failed', 'paused_auth', 'platform_rejected'])
def test_old_parent_needing_handling_is_not_upgraded_when_one_child_is_unresolved(setup_v2, unresolved):
    service, batch_id = completed_video_receipt(setup_v2, 'platform_accepted')
    child = service.get_task(batch_id)['deliveries'][0]
    with session_scope(service.engine) as db:
        db.get(TaskCenterRow, batch_id).state = db.get(PublishTaskRow, batch_id).state = 'action_required'
        db.get(TaskItemRow, child['item_id']).state = unresolved
    original = saved_evidence(service, batch_id)
    result = service.get_task(batch_id)
    assert result['state'] == 'action_required' and result['completed'] == 3
    assert saved_evidence(service, batch_id) == original
