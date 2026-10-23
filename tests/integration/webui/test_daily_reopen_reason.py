"""A reopened, settled daily batch reports its saved child failure evidence."""
import json

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.daily_mixing import AUTH, SOURCE, STATUS, MESSAGES
from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.app_services.multi_publish_runtime import update_authorization, authorized_targets
from yijianjifa.app_services.task_center_access import TaskCenterAccess
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.mixing.queue import MixQueue, MixTask
from yijianjifa.features.publish.coordinator import AdapterOutcome, outcome_evidence
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, DeliveryAttemptRow, canonical
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class NoWork:
    def render_item(self, *args): raise AssertionError('reading reopened results cannot make videos')


@pytest.fixture
def settled(db_engine, app_paths, tmp_path):
    account = AccountService(db_engine).create('douyin', 'original-user', '原账号')
    with session_scope(db_engine) as session:
        session.get(AccountRow, account.id).session_ref = 'private-original-session'
    task = MixQueue(db_engine, NoWork()).enqueue({'quantity': 4,
        'daily_publish': {'host_session': 'old-host', 'targets': [{'account_id': account.id, 'platform': 'douyin'}],
                          'resolved_start': '2026-10-17T12:00:00+08:00'}})
    videos = []
    for index in range(4):
        path = tmp_path / f'{index}.mp4'; path.write_bytes(f'fixture-{index}'.encode())
        videos.append(FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title=f'作品{index}').id)
    service = MultiPublishService(db_engine, app_paths)
    prepared = service.prepare(request_id='original', video_ids=videos,
        targets=[{'account_id': account.id, 'platform': 'douyin'}], common_fields={}, execution_mode='publish_now')
    child = service.save(prepared['prepared_id'], request_id='original')['id']
    with session_scope(db_engine) as session:
        row = session.get(MixTask, task.id)
        row.state = 'completed'; row.current_index = row.completed_count = row.success_count = 4
        session.add(Setting(key=SOURCE + child, value_json=json.dumps({'mix_task_id': task.id, 'host_session': 'old-host'})))
        session.add(Setting(key=STATUS + task.id, value_json=json.dumps({'handoff_authorized': True,
            'auth_phase': '', 'publish_task_ids': [child]})))
        deliveries = list(session.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child).order_by(PublishDeliveryRow.position)))
        for delivery in deliveries:
            session.get(TaskItemRow, delivery.item_id).state = 'pre_submit_failed'
            attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery.id))
            attempt.result_json = canonical(outcome_evidence(AdapterOutcome('pre_submit_failed', error_code='timeout'), delivery.request_digest))
        update_authorization(session, child, add=[row.id for row in deliveries])
    return task.id, child


def test_reopen_keeps_real_timeout_in_status_recent_task_center_and_copy(settled, db_engine, app_paths):
    identity, child = settled
    api = DesktopApi(db_engine, app_paths)
    try:
        status = api._daily.status(identity)
        assert status['phase'] == 'needs_attention' and status['failed'] == 4
        assert status['reason_code'] == 'timeout' and not status['can_continue']
        assert '超时' in status['reason'] and MESSAGES['daily_authorization_required'] not in status['reason']
        data = api._daily.read()
        assert data['active_batch'] is None and data['recent_batch'] is None
        assert api._daily.status(identity)['reason_code']=='timeout'
        access = TaskCenterAccess(db_engine)
        assert '超时' in access.detail(identity)['daily_publish']['reason']
        assert MESSAGES['daily_authorization_required'] not in access.execution_log_text(identity)
        with session_scope(db_engine) as session:
            assert session.get(Setting, AUTH + child) is None and authorized_targets(session, child) == {}
            assert session.get(MixTask, identity).state == 'completed'
    finally: api.close(.1)


def test_stale_authorization_already_saved_by_an_old_client_does_not_hide_child_failure(settled, db_engine, app_paths):
    identity, _ = settled
    api = DesktopApi(db_engine, app_paths)
    try:
        with session_scope(db_engine) as session:
            progress = session.get(Setting, STATUS + identity)
            saved = json.loads(progress.value_json)
            assert saved['reason_code'] == 'daily_authorization_required'
        assert api._daily.status(identity)['reason_code'] == 'timeout'
    finally: api.close(.1)


@pytest.mark.parametrize('state', ['waiting', 'running'])
def test_unfinished_children_keep_reopen_authorization_gate(settled, db_engine, app_paths, state):
    identity, child = settled
    with session_scope(db_engine) as session:
        delivery = session.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child))
        session.get(TaskItemRow, delivery.item_id).state = state
        attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery.id))
        attempt.result_json = None
    api = DesktopApi(db_engine, app_paths)
    try:
        assert api._daily.status(identity)['reason_code'] == 'daily_authorization_required'
        with session_scope(db_engine) as session: assert authorized_targets(session, child) == {}
    finally: api.close(.1)


def test_active_login_gate_is_not_overridden_by_older_settled_child_failure(settled, db_engine, app_paths):
    identity, _ = settled
    with session_scope(db_engine) as session:
        row = session.get(Setting, STATUS + identity)
        progress = json.loads(row.value_json)
        progress.update(auth_phase='needs_attention', auth_host='old-host')
        row.value_json = json.dumps(progress)
    api = DesktopApi(db_engine, app_paths)
    try:
        status = api._daily.status(identity)
        assert status['reason_code'] == 'daily_authorization_required' and status['can_continue']
        assert api._daily.read()['active_batch'] is None
    finally: api.close(.1)
