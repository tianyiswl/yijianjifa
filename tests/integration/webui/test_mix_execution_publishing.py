"""Read saved delivery outcomes, never execute a task or call a platform."""
from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy import event, select

from yijianjifa.app_services.mix_execution import read_execution
from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.mixing.queue import MixQueue, MixTask
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class NoWork:
    def render_item(self, *args): raise AssertionError('result projection must never render')


@pytest.fixture
def publishing(db_engine, app_paths, tmp_path):
    account = AccountService(db_engine).create('douyin', 'PRIVATE-UID', '同名账号')
    with session_scope(db_engine) as session: session.get(AccountRow, account.id).session_ref = 'PRIVATE-SESSION'
    parent = MixQueue(db_engine, NoWork()).enqueue({'quantity': 2, 'copies': [{'title': '制作文案'}],
        'daily_publish': {'targets': [{'account_id': account.id, 'platform': 'douyin'}]}})
    sequence = []
    def child(title='本批冻结标题', *, state='platform_scheduled', code=None, final=True, parent_id=parent.id, account_id=None):
        sequence.append(title)
        path = tmp_path / f'{len(sequence)}.mp4'; path.write_bytes(str(len(sequence)).encode())
        video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title=title)
        service = MultiPublishService(db_engine, app_paths)
        prepared = service.prepare(request_id='result-' + str(len(sequence)), video_ids=[video.id],
            targets=[{'account_id': account_id or account.id, 'platform': 'douyin'}], common_fields={}, execution_mode='publish_now')
        saved = service.save(prepared['prepared_id'], request_id='result-' + str(len(sequence)))
        with session_scope(db_engine) as session:
            delivery = session.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == saved['id']))
            delivery.scheduled_at = '2026-10-17T12:00:00+08:00'
            session.get(TaskItemRow, delivery.item_id).state = state
            attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery.id))
            attempt.result_json = json.dumps({'state': state, 'error_code': code, 'private_url': 'https://PRIVATE.invalid', 'cookie': 'PRIVATECOOKIE'})
            if final: attempt.final_json = json.dumps({'at': '2026-10-16T04:00:00Z'})
            session.add(Setting(key='multi_publish.daily_source:' + saved['id'], value_json=json.dumps({'mix_task_id': parent_id})))
            parent_row = session.get(MixTask, parent.id)
            parent_row.state = 'completed'; parent_row.completed_count = parent_row.success_count = 2
        return saved['id'], delivery.id
    def link(ids, **extra):
        with session_scope(db_engine) as session:
            key = 'mix.daily.status:' + parent.id
            row = session.get(Setting, key)
            value = {'publish_task_ids': ids, **extra}
            if row: row.value_json = json.dumps(value)
            else: session.add(Setting(key=key, value_json=json.dumps(value)))
    return parent.id, account.id, child, link


def test_saved_scheduled_success_and_pre_submit_timeout_remain_distinct_and_read_only(publishing, db_engine):
    parent, account, child, link = publishing
    scheduled, _ = child('定时接收作品')
    failed, _ = child('失败作品', state='pre_submit_failed', code='timeout', final=False)
    link([scheduled, failed])
    statements = []
    def observed(connection, cursor, statement, parameters, context, many): statements.append(statement.lstrip().upper())
    event.listen(db_engine, 'before_cursor_execute', observed)
    try: value = read_execution(db_engine, parent)
    finally: event.remove(db_engine, 'before_cursor_execute', observed)
    result = value['publishing']
    assert result['complete'] is True and result['omitted'] == 0
    assert [t['title'] for t in result['targets']] == ['定时接收作品', '失败作品']
    success, failure = result['targets']
    assert success['account_id'] == failure['account_id'] == account
    assert success['state'] == 'platform_scheduled' and success['state_label'] == '平台已接收定时安排'
    assert success['scheduled_at'] == '2026-10-17T04:00:00+00:00'
    assert '公开' not in success['state_label'] and '成功' not in failure['state_label']
    assert failure['reason'] == '提交前处理超时，尚未提交平台。'
    assert value['current']['label'] == '发布处理结束'
    assert not any(s.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for s in statements)
    assert all(private not in json.dumps(value) for private in ('PRIVATE', '/private/', 'cookie', 'session_ref', 'platform_user_id'))


@pytest.mark.parametrize('state,code,final,label', [
    ('result_unknown', 'timeout', True, '提交结果待核对'),
    ('interrupted', 'timeout', True, '执行中断待核对'),
    ('platform_rejected', None, True, '平台已拒绝'),
    ('paused_auth', None, False, '等待账号验证'),
    ('waiting', None, False, '等待处理'),
    ('published', None, True, '已公开')])
def test_saved_pending_unknown_rejected_and_public_states_are_never_inferred_from_login(
        publishing, db_engine, state, code, final, label):
    parent, _, child, link = publishing
    batch, _ = child(state=state, code=code, final=final)
    link([batch], login_log=[{'time': '2026-10-16T04:00:00Z', 'step': 'daily.login_verified', 'stage': 'before_publish', 'accounts': 1}])
    target = read_execution(db_engine, parent)['publishing']['targets'][0]
    assert target['state'] == state and target['state_label'] == label
    if final and state != 'published': assert '尚未提交' not in target['reason']
    if state in {'result_unknown', 'interrupted'}: assert '核对' in target['reason']
    if state in {'result_unknown', 'interrupted'}: assert read_execution(db_engine, parent)['current']['label'] == '发布结果待核对'


@pytest.mark.parametrize('variant', ['unrelated', 'missing-source', 'hidden-task', 'hidden-target', 'wrong-kind'])
def test_hidden_or_unowned_publish_records_never_supply_a_result_or_execution_context(publishing, db_engine, variant):
    parent, _, child, link = publishing
    batch, delivery_id = child('禁止跨批泄漏', parent_id='other-parent' if variant == 'unrelated' else parent)
    with session_scope(db_engine) as session:
        from yijianjifa.features.tasks.service import TaskCenterRow
        delivery = session.get(PublishDeliveryRow, delivery_id)
        session.get(TaskItemRow, delivery.item_id).state = 'running'
        session.add(Setting(key=f'publish.execution:{delivery_id}:1', value_json=json.dumps([
            {'time': '2026-10-16T05:00:00Z', 'step': 'execution.start'}])))
        if variant == 'missing-source': session.delete(session.get(Setting, 'multi_publish.daily_source:' + batch))
        if variant == 'hidden-task': session.add(Setting(key='task-hidden:' + batch, value_json='true'))
        if variant == 'hidden-target': session.get(PublishDeliveryRow, delivery_id).hidden = True
        if variant == 'wrong-kind': session.get(TaskCenterRow, batch).kind = 'other'
    link([batch])
    result = read_execution(db_engine, parent)
    assert result['publishing']['targets'] == []
    assert '禁止跨批泄漏' not in json.dumps(result)
    assert result['timing']['active'] is False
    assert not any(e['step'] == 'execution.start' for e in result['events'])
    assert result['publishing']['complete'] is False


def test_duplicate_links_do_not_duplicate_targets_and_only_make_has_no_publishing(db_engine, publishing):
    parent, _, child, link = publishing
    batch, _ = child()
    link([batch, batch])
    result = read_execution(db_engine, parent)['publishing']
    assert len(result['targets']) == 1 and result['complete'] is False
    only = MixQueue(db_engine, NoWork()).enqueue({'quantity': 1})
    assert read_execution(db_engine, only.id)['publishing'] == {'targets': [], 'complete': True, 'omitted': 0}


def test_same_platform_and_nickname_keep_distinct_local_account_ids(publishing, db_engine):
    parent, first_account, child, link = publishing
    second = AccountService(db_engine).create('douyin', 'PRIVATE-SECOND-UID', '同名账号')
    with session_scope(db_engine) as session:
        session.get(AccountRow, second.id).session_ref = 'PRIVATE-SECOND-SESSION'
        task = session.get(MixTask, parent)
        snapshot = json.loads(task.snapshot_json)
        snapshot['daily_publish']['targets'].append({'account_id': second.id, 'platform': 'douyin'})
        task.snapshot_json = json.dumps(snapshot)
    first, _ = child('第一账号作品')
    other, _ = child('第二账号作品', account_id=second.id)
    link([first, other])
    targets = read_execution(db_engine, parent)['publishing']['targets']
    assert targets[0]['account_label'] == targets[1]['account_label']
    assert [t['account_id'] for t in targets] == [first_account, second.id]


def test_result_limit_counts_only_known_visible_targets_and_marks_hidden_incomplete(publishing, db_engine):
    parent, _, child, link = publishing
    batch, first_id = child()
    link([batch])
    with session_scope(db_engine) as session:
        row = session.get(PublishDeliveryRow, first_id)
        original = {column.name: getattr(row, column.name) for column in PublishDeliveryRow.__table__.columns}
        history = session.get(PublishHistoryRow, row.history_id)
        history_values = {column.name: getattr(history, column.name) for column in PublishHistoryRow.__table__.columns}
        for position in range(1, 401):
            delivery_id, item_id, history_id = (str(uuid.uuid4()) for _ in range(3))
            session.add(PublishHistoryRow(**{**history_values, 'id': history_id}))
            session.add(TaskItemRow(id=item_id, task_id=batch, item_key=delivery_id, state='platform_scheduled'))
            session.add(PublishDeliveryRow(**{**original, 'id': delivery_id, 'item_id': item_id,
                'history_id': history_id, 'position': position}))
            session.add(DeliveryAttemptRow(id=str(uuid.uuid4()), delivery_id=delivery_id, number=1,
                snapshot_json=row.snapshot_json, request_digest=row.request_digest, result_json='{}'))
    result = read_execution(db_engine, parent)['publishing']
    assert len(result['targets']) == 400 and result['omitted'] == 1 and result['complete'] is False
    with session_scope(db_engine) as session: session.get(PublishDeliveryRow, first_id).hidden = True
    result = read_execution(db_engine, parent)['publishing']
    assert len(result['targets']) == 400 and result['omitted'] == 0 and result['complete'] is False
    assert first_id not in {t['id'] for t in result['targets']}


@pytest.mark.parametrize('saved_state', ['pre_submit_failed', 'paused_auth'])
def test_failure_diagnostic_uses_only_fixed_stage_and_category_and_final_guard(publishing, db_engine, saved_state):
    parent, _, child, link = publishing
    batch, delivery_id = child(state=saved_state, code='adapter_failed', final=False)
    link([batch])
    with session_scope(db_engine) as session:
        attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery_id))
        attempt.result_json = json.dumps({'error_code': 'adapter_failed', 'error_stage': 'session_restore',
            'error_category': 'timeout', 'exception': '/PRIVATE/secret session_ref=PRIVATE'})
    target = read_execution(db_engine, parent)['publishing']['targets'][0]
    assert '所选账号登录资料恢复失败' in target['reason'] and '尚未提交' in target['reason']
    with session_scope(db_engine) as session:
        attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery_id))
        attempt.final_json = '{}'
    target = read_execution(db_engine, parent)['publishing']['targets'][0]
    assert target['state'] == 'result_unknown' and target['state_label'] == '提交结果待核对'
    assert '核对' in target['reason'] and '尚未提交' not in target['reason'] and 'PRIVATE' not in target['reason']
    assert read_execution(db_engine, parent)['current']['label'] == '发布结果待核对'
    with session_scope(db_engine) as session:
        delivery = session.get(PublishDeliveryRow, delivery_id)
        assert session.get(TaskItemRow, delivery.item_id).state == saved_state
