"""任务中心只投影逐条等待，不领取制作或授权发布。"""
import copy
import json

import pytest

from yijianjifa.app_services.task_center_access import TaskCenterAccess
from yijianjifa.features.mixing.queue import MixQueue, MixTask
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class NoRender:
    def render_item(self, *args):
        pytest.fail('只读任务中心不能执行制作')


@pytest.fixture
def serial_task(db_engine, monkeypatch):
    from yijianjifa.app_services import daily_mixing, mix_execution
    task = MixQueue(db_engine, NoRender()).enqueue({'quantity': 3, 'copies': [{'title': '第一条冻结文案'}] * 3,
        'daily_publish': {'execution_flow': 'per_item_v1', 'targets': []}})
    serial = {'mode': 'per_item_v1', 'quantity': 3, 'item_index': 0, 'item_number': 1,
        'item_title': '第一条冻结文案', 'stage': 'publishing', 'target_count': 3,
        'item_received': 1, 'items_received': 0, 'targets_received': 1, 'targets_total': 9,
        'waiting_reason': 'receipt', 'needs_action': False, 'waiting_count': 2,
        'needs_action_count': 0, 'error_count': 0, 'reason': '等待平台确认接收。', 'private_probe': 'DO_NOT_EXPORT'}
    execution = {'serial': serial, 'making': {'total': 3, 'completed': 1, 'succeeded': 1, 'failed': 0},
        'current': {'label': '等待平台确认接收', 'index': 1, 'total': 3, 'title': '第一条冻结文案'},
        'events': [], 'publishing': {'targets': [], 'complete': True, 'omitted': 0}, 'timing': {'active': False}}
    daily = {'phase': 'publishing', 'serial': serial, 'publish_task_ids': [], 'accepted': 1, 'pending': 8,
        'failed': 0, 'total': 9, 'making_failed': 0, 'published': 0, 'can_continue': False,
        'resolved_start': None, 'reason': serial['reason'], 'execution': execution}
    with session_scope(db_engine) as session:
        row = session.get(MixTask, task.id)
        row.state = 'waiting_publish'; row.current_index = row.completed_count = row.success_count = 1
        session.add(Setting(key=f'mix-result:{task.id}:0', value_json=json.dumps({'index': 0, 'state': 'completed'})))
    reads = []
    def read_status(engine, task_id):
        assert engine is db_engine and task_id == task.id
        reads.append(task_id)
        return copy.deepcopy(daily)
    monkeypatch.setattr(daily_mixing, 'read_status', read_status)
    monkeypatch.setattr(mix_execution, 'read_execution', lambda *args, **kwargs: copy.deepcopy(execution))
    return task.id, serial, daily, execution, reads


def test_waiting_publish_is_waiting_not_failure_and_has_pause_cancel_only(db_engine, serial_task):
    identity, serial, *_ = serial_task
    access = TaskCenterAccess(db_engine)
    rows = access.list('', 'make', 'waiting', 0, 20)
    assert rows['total'] == 1
    row = rows['items'][0]
    assert row['state'] == 'waiting_publish' and row['serial']['item_received'] == 1
    assert '等待平台确认' in row['status_label'] and row['failed'] == 0
    assert access.list('', 'all', 'action', 0, 20)['total'] == 0
    assert access.list('', 'all', 'failed', 0, 20)['total'] == 0
    detail = access.detail(identity)
    assert detail['controls'] == ['pause', 'cancel']
    assert detail['daily_publish']['serial'] == detail['task']['serial'] == detail['execution']['serial']
    assert 'DO_NOT_EXPORT' not in json.dumps(detail)
    with session_scope(db_engine) as session:
        row = session.get(MixTask, identity)
        assert (row.state, row.current_index, row.success_count) == ('waiting_publish', 1, 1)


@pytest.mark.parametrize('waiting_reason', ['verification', 'result', 'authorization'])
def test_action_wait_is_actionable_but_not_failed(db_engine, serial_task, waiting_reason):
    _, serial, daily, *_ = serial_task
    serial.update(waiting_reason=waiting_reason, needs_action=True, needs_action_count=1)
    daily.update(phase='needs_attention', can_continue=True)
    access = TaskCenterAccess(db_engine)
    assert access.list('', 'all', 'action', 0, 20)['total'] == 1
    assert access.list('', 'all', 'failed', 0, 20)['total'] == 0
    assert access.list('', 'all', 'waiting', 0, 20)['items'][0]['serial']['error_count'] == 0


def test_real_serial_error_is_discoverable_without_hiding_it_as_wait(db_engine, serial_task):
    _, serial, daily, *_ = serial_task
    serial.update(error_count=1, needs_action=True, reason='当前条视频工具执行错误。')
    daily.update(phase='needs_attention', failed=1, reason=serial['reason'])
    access = TaskCenterAccess(db_engine)
    rows = access.list('', 'all', 'failed', 0, 20)
    assert rows['total'] == 1
    assert '错误' in rows['items'][0]['status_label']


def test_serial_detail_reuses_one_status_snapshot_and_exposes_finite_fields(db_engine, serial_task):
    identity, _, _, _, reads = serial_task
    detail = TaskCenterAccess(db_engine).detail(identity)
    assert reads == [identity]
    assert detail['daily_publish']['serial']['targets_total'] == 9
    assert detail['daily_publish']['serial']['needs_action'] is False
    assert 'private_probe' not in detail['daily_publish']['serial']


def test_serial_copy_retains_global_and_current_counts_as_wait_not_failure(db_engine, serial_task):
    identity, *_ = serial_task
    text = TaskCenterAccess(db_engine).execution_log_text(identity)
    assert '第1/3条' in text and '当前条平台已接收 1/3' in text and '全局 1/9' in text
    assert '等待平台确认接收' in text and 'DO_NOT_EXPORT' not in text


def test_malformed_serial_is_not_exported_or_claimed_as_complete(db_engine, serial_task):
    _, serial, *_ = serial_task
    serial['needs_action'] = 'true'
    row = TaskCenterAccess(db_engine).list('', 'make', 'waiting', 0, 20)['items'][0]
    assert 'serial' not in row and '等待' in row['status_label']


def legacy_waits(serial_task):
    identity, _, daily, execution, _ = serial_task
    daily.pop('serial'); execution.pop('serial')
    daily.update(total=4, accepted=1, failed=2, pending=1)
    execution['publishing'] = {'complete': True, 'omitted': 0, 'targets': [
        {'id': 'accepted', 'account_id': 'a', 'platform': 'douyin', 'state': 'platform_scheduled'},
        {'id': 'unknown', 'account_id': 'b', 'platform': 'douyin', 'state': 'result_unknown'},
        {'id': 'verify', 'account_id': 'c', 'platform': 'douyin', 'state': 'paused_auth', 'needs_verification': True},
        {'id': 'queued', 'account_id': 'b', 'platform': 'douyin', 'state': 'waiting',
         'blocked_by': {'delivery_id': 'unknown', 'state': 'result_unknown'}}]}
    return identity, daily, execution


def test_legacy_copy_recounts_true_errors_and_each_wait_without_copying_old_failed(db_engine, serial_task):
    identity, _, _ = legacy_waits(serial_task)
    text = TaskCenterAccess(db_engine).execution_log_text(identity)
    assert '平台已接收 1/4 项' in text and '错误 0 项' in text
    assert '等待核对 1 项' in text and '等待验证 1 项' in text and '等待账号 1 项' in text
    assert '失败 2 项' not in text


def test_partial_legacy_copy_does_not_invent_zero_error_counts(db_engine, serial_task):
    identity, _, execution = legacy_waits(serial_task)
    execution['publishing'].update(complete=False, omitted=1)
    text = TaskCenterAccess(db_engine).execution_log_text(identity)
    assert '部分发布记录' in text and '错误 0 项' not in text and '失败 2 项' not in text


def test_copy_keeps_an_independent_execution_error_beside_wait_reason(db_engine, serial_task):
    identity, _, _, execution, _ = serial_task
    execution['reason'] = '本地成品文件无法读取。'
    text = TaskCenterAccess(db_engine).execution_log_text(identity)
    assert '等待平台确认接收。' in text and '本地成品文件无法读取。' in text


def test_paused_serial_requires_daily_continue_instead_of_generic_resume(db_engine, serial_task):
    identity, serial, daily, *_ = serial_task
    serial.update(stage='paused', waiting_reason='authorization', needs_action=True)
    daily.update(phase='needs_attention', can_continue=True)
    with session_scope(db_engine) as session:
        session.get(MixTask, identity).state = 'paused'
    data = TaskCenterAccess(db_engine).detail(identity)
    assert data['controls'] == ['cancel']
    assert data['daily_publish']['can_continue'] is True


def test_visible_publishing_record_with_unknown_state_does_not_claim_error_zero(db_engine, serial_task):
    identity, _, execution = legacy_waits(serial_task)
    execution['publishing']['targets'][0]['state'] = 'unrecognized_future_state'
    text = TaskCenterAccess(db_engine).execution_log_text(identity)
    assert '部分发布记录' in text and '错误 0 项' not in text


def test_duplicate_delivery_evidence_does_not_supply_complete_legacy_counts(db_engine, serial_task):
    identity, _, execution = legacy_waits(serial_task)
    execution['publishing']['targets'][1]['id'] = 'accepted'
    text = TaskCenterAccess(db_engine).execution_log_text(identity)
    assert '部分发布记录' in text and '错误 0 项' not in text


def test_invalid_serial_projection_does_not_enable_generic_resume(db_engine, serial_task):
    identity, serial, *_ = serial_task
    serial['needs_action'] = 'true'
    with session_scope(db_engine) as session:
        session.get(MixTask, identity).state = 'paused'
    assert TaskCenterAccess(db_engine).detail(identity)['controls'] == ['cancel']


@pytest.mark.parametrize('marker', [None, 'unsupported-flow'])
def test_unknown_flow_marker_never_exposes_legacy_resume(db_engine, serial_task, marker):
    identity, serial, daily, *_ = serial_task
    serial.update(stage='needs_attention', waiting_reason='configuration', needs_action=True)
    daily.update(phase='needs_attention', can_continue=False)
    with session_scope(db_engine) as session:
        row=session.get(MixTask,identity)
        snapshot=json.loads(row.snapshot_json)
        snapshot['daily_publish']['execution_flow']=marker
        row.snapshot_json=json.dumps(snapshot);row.state='paused'
    detail=TaskCenterAccess(db_engine).detail(identity)
    assert detail['task']['workflow_serial'] is True
    assert detail['controls']==['cancel']
    assert detail['daily_publish']['can_continue'] is False
