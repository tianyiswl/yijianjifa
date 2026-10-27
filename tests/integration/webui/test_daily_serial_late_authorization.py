"""冻结成品之后账号登录失效时，逐条流程应明确等待重新检查登录。"""
import json

import pytest
from sqlalchemy import select

from test_daily_serial_flow import serial, status, call
from yijianjifa.app_services.multi_publish_runtime import authorized_targets
from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.features.publish.deliveries import PublishDeliveryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


@pytest.mark.parametrize('expired_count', [3, 1], ids=['zero-ready', 'partially-ready'])
def test_login_expiring_after_child_save_is_visible_and_authorizes_no_subset(serial, monkeypatch, expired_count):
    api, parent, renderer, events, _, accounts = serial
    original = api._multi_publish.service.save
    saves = []

    def save_then_expire(*args, **kwargs):
        receipt = original(*args, **kwargs)
        saves.append(receipt['id'])
        with session_scope(api._engine) as s:
            for account in accounts[:expired_count]:
                s.get(AccountRow, account.id).login_state = 'expired'
        return receipt

    monkeypatch.setattr(api._multi_publish.service, 'save', save_then_expire)
    api._mix.queue.run_once_until_idle()
    progress = status(serial)
    assert len(saves) == 1 and progress['publish_task_ids'] == saves
    detail = api._multi_publish.detail(saves[0])
    assert sum(d['can_submit'] for d in detail['deliveries']) == 3 - expired_count
    assert sum(d['readiness_reason'] == 'account_verification_required' for d in detail['deliveries']) == expired_count
    assert progress['waiting_reason'] == progress['serial']['waiting_reason'] == 'authorization'
    assert progress['needs_action'] and progress['can_continue'] and progress['failed'] == 0
    assert '登录' in progress['reason'] and progress['serial']['needs_action_count'] == expired_count
    with session_scope(api._engine) as s:
        assert authorized_targets(s, saves[0]) == {}
        proof = json.loads(s.get(Setting, 'mix.daily.status:' + parent).value_json)
        assert not proof.get('handoff_authorized')
        before = [(row.key, row.value_json) for row in s.scalars(select(Setting))]
    for _ in range(3):
        call(api, 'mix.daily.status', {'id': parent})
    with session_scope(api._engine) as s:
        assert before == [(row.key, row.value_json) for row in s.scalars(select(Setting))]
    api._daily.recheck_serial(); api._mix.queue.run_once_until_idle()
    assert renderer.calls == renderer.tts_calls == 1 and saves == progress['publish_task_ids']
    assert not any(event[0] == 'received' for event in events)
    with session_scope(api._engine) as s:
        for account in accounts[:expired_count]:
            s.get(AccountRow, account.id).session_ref = 'explicit-new-login-' + account.platform_user_id
    assert call(api, 'task.resume', {'id': parent}, 'explicit-late-login-check')['ok']
    with session_scope(api._engine) as s:
        assert len(authorized_targets(s, saves[0])) == 3
    assert status(serial)['publish_task_ids'] == saves and len(saves) == 1
    assert renderer.calls == renderer.tts_calls == 1


@pytest.mark.parametrize('priority', ['account_busy', 'real_error', 'result_unknown', 'paused_auth', 'cancelled'])
def test_late_login_display_preserves_busy_error_and_permanent_cancel_priority(serial, priority):
    api, parent, renderer, _, _, accounts = serial
    api._mix.queue.run_once_until_idle()
    child = status(serial)['publish_task_ids'][0]
    with session_scope(api._engine) as s:
        rows = list(s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child)))
        for row in rows:
            s.add(Setting(key=f'publish.execution:{row.id}:{row.attempt}', value_json=json.dumps([
                {'step': 'account.wait', 'checks': {'busy': True}}])))
        if priority != 'account_busy':
            s.get(AccountRow, accounts[0].id).login_state = 'expired'
        if priority == 'real_error':
            s.get(TaskItemRow, rows[0].item_id).state = 'pre_submit_failed'
        elif priority in {'result_unknown', 'paused_auth'}:
            s.get(TaskItemRow, rows[0].item_id).state = priority
    if priority == 'cancelled':
        assert call(api, 'task.cancel', {'id': parent}, 'cancel-before-login-display')['ok']
    progress = status(serial)
    if priority == 'account_busy':
        assert progress['waiting_reason'] == 'account' and not progress['needs_action']
        assert progress['failed'] == 0 and not progress['can_continue']
    elif priority == 'real_error':
        assert progress['failed'] == 1 and progress['waiting_reason'] == 'configuration'
        assert '错误' in progress['reason']
    elif priority in {'result_unknown', 'paused_auth'}:
        assert progress['failed'] == 0 and progress['needs_action'] and not progress['can_continue']
        assert progress['waiting_reason'] == ('result' if priority == 'result_unknown' else 'verification')
    else:
        assert progress['phase'] == 'cancelled' and not progress['can_continue']
        assert call(api, 'task.resume', {'id': parent}, 'cannot-revive-cancelled-login')['ok'] is False
    assert renderer.calls == 1


def test_login_expiry_of_already_received_account_does_not_block_remaining_accounts(serial):
    api, parent, _, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    child = status(serial)['publish_task_ids'][0]
    with session_scope(api._engine) as s:
        ids = list(authorized_targets(s, child))
    api._multi_publish.coordinator.run_once(child, ids)
    with session_scope(api._engine) as s:
        accepted = next(row for row in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child))
                        if s.get(TaskItemRow, row.item_id).state == 'platform_scheduled')
        s.get(AccountRow, accepted.account_id).login_state = 'expired'
    progress = status(serial)
    assert progress['accepted'] == 1 and progress['waiting_reason'] == 'receipt'
    assert progress['failed'] == 0 and not progress['needs_action'] and not progress['can_continue']


def test_explicit_login_repair_with_new_session_only_authorizes_remaining_original_targets(serial):
    api, parent, renderer, events, _, _ = serial
    api._mix.queue.run_once_until_idle()
    child = status(serial)['publish_task_ids'][0]
    original_snapshot = api._mix.queue.get(parent).snapshot
    with session_scope(api._engine) as s:
        ids = list(authorized_targets(s, child))
    api._multi_publish.coordinator.run_once(child, ids)
    with session_scope(api._engine) as s:
        rows = list(s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child)))
        received = {row.id for row in rows if s.get(TaskItemRow, row.item_id).state == 'platform_scheduled'}
        for row in rows:
            if row.id not in received:
                account = s.get(AccountRow, row.account_id)
                account.login_state = 'expired'
                account.session_ref = 'explicit-repaired-session-' + account.platform_user_id
    assert len(received) == 1 and status(serial)['waiting_reason'] == 'authorization'
    assert call(api, 'task.resume', {'id': parent}, 'repair-partial-original')['ok']
    with session_scope(api._engine) as s:
        assert set(authorized_targets(s, child)) == set(ids) - received
        assert all(s.get(PublishDeliveryRow, delivery_id).attempt == 1 for delivery_id in ids)
    assert api._mix.queue.get(parent).snapshot == original_snapshot
    assert status(serial)['publish_task_ids'] == [child] and status(serial)['waiting_reason'] == 'receipt'
    assert renderer.calls == renderer.tts_calls == 1
    assert len([event for event in events if event[0] == 'received']) == 1
