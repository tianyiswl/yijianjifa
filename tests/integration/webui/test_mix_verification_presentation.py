"""Verification guidance is a pure view, never a new publication outcome."""
import json

import pytest
from sqlalchemy import event, select

from yijianjifa.app_services.daily_mixing import read_status
from yijianjifa.app_services.mix_execution import read_execution
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_mix_execution_publishing import publishing


@pytest.mark.parametrize(('state', 'code', 'final', 'cancelled', 'expected'), [
    ('paused_auth', 'verification_required', False, False, True),
    ('result_unknown', 'verification_required', True, False, True),
    ('interrupted', 'verification_required', True, False, True),
    ('result_unknown', 'timeout', True, False, False),
    ('result_unknown', 'PRIVATE_RAW_ERROR', True, False, False),
    ('paused_auth', None, False, False, False),
    ('pre_submit_failed', 'verification_required', False, False, False),
    ('platform_scheduled', 'verification_required', True, False, False),
    ('platform_rejected', 'verification_required', True, False, False),
    ('cancelled', 'verification_required', False, False, False),
    ('result_unknown', 'verification_required', True, True, False),
])
def test_only_unresolved_uncancelled_verification_gets_platform_guidance(
        publishing, db_engine, state, code, final, cancelled, expected):
    parent, _, child, link = publishing
    batch, delivery_id = child(state=state, code=code, final=final)
    link([batch])
    with session_scope(db_engine) as session:
        session.get(PublishDeliveryRow, delivery_id).cancel_requested = cancelled
    target = read_execution(db_engine, parent)['publishing']['targets'][0]
    assert target['needs_verification'] is expected
    assert 'error_code' not in target  # No new raw-error surface.
    if expected:
        assert '需完成平台验证' in target['reason']
    if state in {'platform_scheduled', 'platform_rejected', 'cancelled'}:
        assert '需完成平台验证' not in target['reason']
    if code == 'PRIVATE_RAW_ERROR':
        assert code not in json.dumps(target)


@pytest.mark.parametrize(('other_state', 'other_code', 'failed_making', 'auth_phase', 'expected'), [
    ('platform_scheduled', None, 0, '', True),
    ('paused_auth', 'verification_required', 0, '', True),
    ('result_unknown', 'timeout', 0, '', False),
    ('pre_submit_failed', 'timeout', 0, '', False),
    ('platform_rejected', None, 0, '', False),
    ('waiting', None, 0, '', False),
    ('running', None, 0, '', False),
    ('platform_scheduled', None, 1, '', False),
    ('platform_scheduled', None, 0, 'needs_login', False),
])
def test_batch_current_never_hides_nonverification_problems(
        publishing, db_engine, other_state, other_code, failed_making, auth_phase, expected):
    parent, _, child, link = publishing
    verification, _ = child('需完成验证', state='result_unknown', code='verification_required', final=True)
    other, _ = child('另一目标', state=other_state, code=other_code,
                     final=other_state not in {'paused_auth', 'pre_submit_failed', 'waiting'})
    link([verification, other], auth_phase=auth_phase)
    with session_scope(db_engine) as session:
        session.get(MixTask, parent).failure_count = failed_making
    assert (read_execution(db_engine, parent)['current']['label'] == '等待完成平台验证') is expected


@pytest.mark.parametrize('stop', ['hidden_other_target', 'automatic_stopped'])
def test_partial_or_stopped_projection_never_claims_all_remaining_work_only_needs_verification(
        publishing, db_engine, stop):
    parent, _, child, link = publishing
    verification, _ = child('需完成验证', state='result_unknown', code='verification_required', final=True)
    other, other_id = child('未确认目标', state='result_unknown', code='timeout', final=True)
    link([verification, other], automatic_stopped=stop == 'automatic_stopped')
    if stop == 'hidden_other_target':
        with session_scope(db_engine) as session:
            session.get(PublishDeliveryRow, other_id).hidden = True
    assert read_execution(db_engine, parent)['current']['label'] != '等待完成平台验证'


def test_verification_read_preserves_states_counts_authorization_logs_and_final_markers(publishing, db_engine):
    parent, _, child, link = publishing
    verification, delivery_id = child('需完成验证', state='result_unknown', code='verification_required', final=True)
    accepted, _ = child('已接收作品', state='platform_scheduled', final=True)
    link([verification, accepted], handoff_authorized=True,
         reason_code='verification_required', reason='verification_required')
    with session_scope(db_engine) as session:
        task = session.get(MixTask, parent)
        snapshot = json.loads(task.snapshot_json)
        snapshot['daily_publish']['resolved_start'] = '2026-10-17T12:00:00+08:00'
        task.snapshot_json = json.dumps(snapshot)

    def evidence():
        with session_scope(db_engine) as session:
            task = session.get(MixTask, parent)
            return (task.state, task.completed_count, task.success_count, task.failure_count,
                [(row.id, row.state) for row in session.scalars(select(TaskItemRow).order_by(TaskItemRow.id))],
                [(row.id, row.final_json, row.result_json) for row in
                 session.scalars(select(DeliveryAttemptRow).order_by(DeliveryAttemptRow.id))],
                [(row.key, row.value_json) for row in session.scalars(select(Setting).order_by(Setting.key))])

    before = evidence()
    statements = []

    def observed(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().upper())

    event.listen(db_engine, 'before_cursor_execute', observed)
    try:
        status = read_status(db_engine, parent)
        execution = read_execution(db_engine, parent)
    finally:
        event.remove(db_engine, 'before_cursor_execute', observed)
    assert status['reason_code'] == 'verification_required'
    assert status['reason'] != 'verification_required' and '验证' in status['reason']
    assert status['accepted'] == 1 and status['published'] == 0 and status['failed'] == 0
    assert status['pending'] == 1 and status['needs_action_count'] == 1 and status['needs_action'] is True
    assert status['waiting_reason'] == 'verification'
    assert execution['current']['label'] == '等待完成平台验证'
    assert [target['needs_verification'] for target in execution['publishing']['targets']] == [True, False]
    assert evidence() == before
    assert not any(statement.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for statement in statements)
