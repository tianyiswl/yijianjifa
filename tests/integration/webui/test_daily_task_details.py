import json

from yijianjifa.app_services.task_center_access import TaskCenterAccess
from yijianjifa.features.mixing.queue import MixQueue, MixTask
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_daily_execution_copy import daily_copy


class NoRender:
    def render_item(self, *args):
        raise AssertionError('a detail read cannot start making')


def test_daily_make_detail_explains_why_successful_outputs_have_not_started_publishing(db_engine):
    queue = MixQueue(db_engine, NoRender())
    task = queue.enqueue({'quantity': 1, 'daily_publish': {'host_session': 'private-grant', 'targets': [{'platform': 'douyin', 'account_id': 'account'}], 'resolved_start': '2026-10-16T12:00:00+08:00'}})
    with session_scope(db_engine) as session:
        row = session.get(MixTask, task.id)
        row.state = 'completed'
        row.completed_count = row.success_count = 1
        session.add(Setting(key='mix.daily.status:' + task.id, value_json=json.dumps({
            'publish_task_ids': [], 'reason_code': 'account_verification_required',
            'reason': '账号需要重新登录 cookie=private-cookie', 'private': 'private-snapshot'})))
    access = TaskCenterAccess(db_engine)
    detail = access.detail(task.id)
    assert 'daily_publish' in detail
    assert '账号需要重新登录' in detail['daily_publish']['reason']
    assert detail['daily_publish']['publish_task_ids'] == []
    assert all(secret not in json.dumps(detail) for secret in ('private-grant', 'private-cookie', 'private-snapshot'))
    copied = access.execution_log_text(task.id)
    assert '制作后发布' in copied and '账号需要重新登录' in copied
    assert 'private-cookie' not in copied
    pending = access.list('', 'action', '', 0, 20)
    assert [row['id'] for row in pending['items']] == [task.id]
    assert pending['items'][0]['status_label'] == '制作完成，发布需要处理'


def test_make_only_detail_has_no_publish_chain(db_engine):
    task = MixQueue(db_engine, NoRender()).enqueue({'quantity': 1})
    detail = TaskCenterAccess(db_engine).detail(task.id)
    assert 'daily_publish' not in detail
    assert 'execution' not in detail


def test_daily_parent_detail_names_each_failed_account_and_content(daily_copy, db_engine):
    from sqlalchemy import select
    from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
    from yijianjifa.features.tasks.service import TaskItemRow
    identity, child, progress = daily_copy
    pid, did = child('具体失败的作品')
    progress(publish_task_ids=[pid])
    with session_scope(db_engine) as session:
        delivery = session.get(PublishDeliveryRow, did)
        session.get(TaskItemRow, delivery.item_id).state = 'pre_submit_failed'
        attempt = session.scalar(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id == did, DeliveryAttemptRow.number == delivery.attempt))
        attempt.result_json = json.dumps({'error_code': 'timeout'})
    detail = TaskCenterAccess(db_engine).detail(identity)
    target = detail['execution']['publishing']['targets'][0]
    assert target['account_label'] == '抖音 · 合成账号'
    assert target['title'] == '具体失败的作品'
    assert target['state'] == 'pre_submit_failed' and '尚未提交平台' in target['reason']
    assert 'private-session' not in json.dumps(detail)
