"""Daily clipboard text reads saved evidence and only its own visible children."""
import json

import pytest
from sqlalchemy import event, select

from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.app_services.task_center_access import TaskCenterAccess
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.mixing.queue import MixQueue, MixTask
from yijianjifa.features.publish.deliveries import PublishDeliveryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class NoWork:
    def render_item(self, *args): raise AssertionError('copying cannot make videos')


@pytest.fixture
def daily_copy(db_engine, app_paths, tmp_path):
    account = AccountService(db_engine).create('douyin', 'private-user', '合成账号')
    with session_scope(db_engine) as session:
        session.get(AccountRow, account.id).session_ref = 'private-session'
    task = MixQueue(db_engine, NoWork()).enqueue({'quantity': 3, 'copies': [{'title': '原制作文案'}],
        'daily_publish': {'targets': [{'account_id': account.id, 'platform': 'douyin'}],
                          'resolved_start': '2026-10-17T12:00:00+08:00'}})
    service = MultiPublishService(db_engine, app_paths)
    sequence = []
    def child(title, *, parent=task.id):
        sequence.append(title)
        path = tmp_path / (str(len(sequence)) + '.mp4'); path.write_bytes(title.encode())
        video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title=title)
        prepared = service.prepare(request_id='prepare-' + str(len(sequence)), video_ids=[video.id],
            targets=[{'account_id': account.id, 'platform': 'douyin'}],
            common_fields={'title': title}, execution_mode='publish_now')
        saved = service.save(prepared['prepared_id'], request_id='prepare-' + str(len(sequence)))
        with session_scope(db_engine) as session:
            delivery = session.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == saved['id']))
            session.get(TaskItemRow, delivery.item_id).state = 'running'
            session.add(Setting(key='multi_publish.daily_source:' + saved['id'],
                value_json=json.dumps({'mix_task_id': parent, 'host_session': 'private-host'})))
            session.add(Setting(key=f'publish.execution:{delivery.id}:1', value_json=json.dumps([
                {'time': '2026-10-16T01:02:03Z', 'step': 'channels.prepare',
                 'checks': {'uploaded': True, 'form_filled': False, 'cookie': 'PRIVATECOOKIE'}}])))
        return saved['id'], delivery.id
    def progress(**values):
        with session_scope(db_engine) as session:
            row = session.get(Setting, 'mix.daily.status:' + task.id)
            data = json.loads(row.value_json) if row else {}
            data.update(values)
            if row: row.value_json = json.dumps(data)
            else: session.add(Setting(key='mix.daily.status:' + task.id, value_json=json.dumps(data)))
    return task.id, child, progress


def test_daily_copy_uses_real_counts_current_reason_and_truthful_login_events(daily_copy, db_engine):
    identity, _, progress = daily_copy
    with session_scope(db_engine) as session:
        task = session.get(MixTask, identity)
        task.state = 'completed_with_errors'; task.current_index = task.completed_count = 3
        task.success_count = 2; task.failure_count = 1
    progress(auth_phase='needs_attention', reason_code='daily_login_check_failed',
        reason='视频号检查超时 cookie=PRIVATECOOKIE', login_log=[
            {'time': '2026-10-16T01:00:01Z', 'step': 'daily.login_check_started', 'stage': 'before_publish',
             'index': 1, 'accounts': 2, 'account_label': '视频号 · 测试账号', 'message': '正在检查登录'},
            {'time': '2026-10-16T01:00:26Z', 'step': 'daily.login_check_finished', 'stage': 'before_publish',
             'index': 1, 'accounts': 2, 'account_label': '视频号 · 测试账号', 'message': '检查超时'},
            {'time': '2026-10-15T01:00:01Z', 'step': 'daily.login_verified', 'stage': 'before_make', 'accounts': 2}])
    statements = []
    def observe(connection, cursor, statement, parameters, context, many): statements.append(statement.lstrip().upper())
    event.listen(db_engine, 'before_cursor_execute', observe)
    try: copied = TaskCenterAccess(db_engine).execution_log_text(identity)
    finally: event.remove(db_engine, 'before_cursor_execute', observe)
    assert '已完成 3/3 条' in copied and '成功 2 条' in copied and '失败 1 条' in copied
    assert '当前：等待账号处理后继续' in copied and '视频号检查超时' in copied
    started = next(line for line in copied.splitlines() if 'daily.login_check_started' in line)
    failed = next(line for line in copied.splitlines() if 'daily.login_check_finished' in line)
    verified = next(line for line in copied.splitlines() if 'daily.login_verified' in line)
    assert '正在检查登录' in started and '通过' not in started
    assert '检查超时' in failed and '通过' not in failed
    assert '检查已通过' in verified
    assert 'PRIVATECOOKIE' not in copied
    assert not any(statement.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for statement in statements)


def test_daily_copy_appends_current_owned_publish_steps_once_and_reloads_saved_events(daily_copy, db_engine):
    identity, child, progress = daily_copy
    child_id, delivery_id = child('本批真实发布作品')
    progress(publish_task_ids=[child_id, child_id])
    access = TaskCenterAccess(db_engine)
    copied = access.execution_log_text(identity)
    assert copied.count('对应发布任务：' + child_id) == 1
    assert '发布进度：' not in copied  # A duplicated raw link cannot supply doubled counters.
    assert '本批真实发布作品' in copied and 'channels.prepare' in copied
    assert 'form_filled' in copied and 'false' in copied and 'uploaded' in copied
    assert 'PRIVATECOOKIE' not in copied and 'private-host' not in copied
    with session_scope(db_engine) as session:
        row = session.get(Setting, f'publish.execution:{delivery_id}:1')
        entries = json.loads(row.value_json)
        entries.append({'time': '2026-10-16T01:02:04Z', 'step': 'execution.result', 'checks': {'state': 'result_unknown'}})
        row.value_json = json.dumps(entries)
    assert 'execution.result' in access.execution_log_text(identity)


@pytest.mark.parametrize('variant', ['unrelated', 'hidden-task', 'hidden-delivery'])
def test_daily_copy_does_not_disclose_unrelated_or_hidden_publish_children(daily_copy, db_engine, variant):
    identity, child, progress = daily_copy
    child_id, delivery_id = child('不得泄漏的子任务作品', parent='another-parent' if variant == 'unrelated' else identity)
    with session_scope(db_engine) as session:
        if variant == 'hidden-task': session.add(Setting(key='task-hidden:' + child_id, value_json='true'))
        if variant == 'hidden-delivery': session.get(PublishDeliveryRow, delivery_id).hidden = True
    progress(publish_task_ids=[identity, child_id, child_id, 'missing'])
    copied = TaskCenterAccess(db_engine).execution_log_text(identity)
    assert '不得泄漏的子任务作品' not in copied
    assert child_id not in copied and delivery_id not in copied
    assert 'channels.prepare' not in copied and '对应发布任务：' not in copied


def test_make_only_copy_keeps_its_original_scope_without_daily_projection(db_engine):
    task = MixQueue(db_engine, NoWork()).enqueue({'quantity': 2,
        'copies': [{'title': '第一条'}, {'title': '正在制作第二条'}]})
    with session_scope(db_engine) as session:
        row = session.get(MixTask, task.id)
        row.state = 'running'; row.current_index = row.completed_count = row.success_count = 1
    copied = TaskCenterAccess(db_engine).execution_log_text(task.id)
    assert '已完成 1/2 条' in copied and '成功 1 条' in copied and '失败 0 条' in copied
    assert '当前：制作中 · 第2/2条 · 正在制作第二条' in copied
    assert '未记录执行步骤；未补造历史日志' in copied
    assert '制作后发布' not in copied and '对应发布任务' not in copied
    assert '发布结果一览' not in copied


def test_daily_copy_puts_saved_target_result_before_detailed_logs(daily_copy, db_engine):
    from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
    identity, child, progress = daily_copy
    child_id, delivery_id = child('结果应能直接看到')
    progress(publish_task_ids=[child_id], login_log=[
        {'time': '2026-10-16T01:00:01Z', 'step': 'daily.login_verified',
         'stage': 'before_publish', 'accounts': 1}])
    with session_scope(db_engine) as session:
        row = session.get(PublishDeliveryRow, delivery_id)
        session.get(TaskItemRow, row.item_id).state = 'pre_submit_failed'
        attempt = session.scalar(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id == delivery_id, DeliveryAttemptRow.number == row.attempt))
        attempt.result_json = json.dumps({'error_code': 'timeout'})
    copied = TaskCenterAccess(db_engine).execution_log_text(identity)
    assert '发布结果一览' in copied
    overview = copied.split('发布结果一览', 1)[1].split('daily.login_verified', 1)[0]
    assert '抖音 · 合成账号' in overview and '结果应能直接看到' in overview
    assert '提交前失败' in overview and '尚未提交平台' in overview
    assert 'private-user' not in copied and 'private-session' not in copied
    assert copied.index('发布结果一览') < copied.index('daily.login_verified') < copied.index('对应发布任务：')
    with session_scope(db_engine) as session:
        row = session.get(PublishDeliveryRow, delivery_id)
        session.get(TaskItemRow, row.item_id).state = 'platform_scheduled'
        attempt = session.scalar(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id == delivery_id, DeliveryAttemptRow.number == row.attempt))
        attempt.result_json = json.dumps({'state': 'platform_scheduled'})
    overview = TaskCenterAccess(db_engine).execution_log_text(identity).split('发布结果一览', 1)[1].split('daily.login_verified', 1)[0]
    assert '平台已接收定时安排' in overview and '已公开' not in overview


def test_daily_login_evidence_is_not_lost_when_publish_events_fill_the_panel_tail(daily_copy, db_engine):
    identity, child, progress = daily_copy
    child_id, delivery_id = child('已记录很多发布步骤')
    progress(publish_task_ids=[child_id], login_log=[
        {'time': '2026-10-16T01:00:26Z', 'step': 'daily.login_check_finished', 'stage': 'before_publish',
         'index': 1, 'accounts': 1, 'account_label': '视频号 · 原账号', 'message': '检查超时'}])
    with session_scope(db_engine) as session:
        row = session.get(Setting, f'publish.execution:{delivery_id}:1')
        row.value_json = json.dumps([{'time': '2026-10-16T02:00:00Z', 'step': 'channels.prepare',
                                     'checks': {'event_index': index}} for index in range(110)])
    copied = TaskCenterAccess(db_engine).execution_log_text(identity)
    login = next(line for line in copied.splitlines() if 'daily.login_check_finished' in line)
    assert '检查超时' in login and '通过' not in login
    assert copied.count('channels.prepare') == 110
