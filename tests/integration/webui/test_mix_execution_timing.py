"""Elapsed timing projects saved batch evidence; it is not CPU work time."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.mix_execution import read_execution
from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.app_services.multi_publish_runtime import update_authorization
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.mixing.queue import MixQueue, MixTask
from yijianjifa.features.publish.deliveries import PublishDeliveryRow
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


UTC = timezone.utc
START = datetime(2026, 10, 16, 1, tzinfo=UTC)


class NoWork:
    def render_item(self, *args): raise AssertionError('timing reads cannot make videos')


@pytest.fixture
def timed(db_engine):
    task = MixQueue(db_engine, NoWork()).enqueue({'quantity': 2})
    with session_scope(db_engine) as session:
        row = session.get(MixTask, task.id)
        row.created_at = START; row.updated_at = START + timedelta(minutes=2)
    return task.id


def daily_progress(engine, identity, **values):
    with session_scope(engine) as session:
        task = session.get(MixTask, identity)
        snapshot = json.loads(task.snapshot_json)
        snapshot['daily_publish'] = {'host_session': 'old-host',
            'targets': [{'account_id': 'original-account', 'platform': 'douyin'}],
            'resolved_start': '2026-10-17T12:00:00+08:00'}
        task.snapshot_json = json.dumps(snapshot)
        task.updated_at = START + timedelta(minutes=2)
        row = session.get(Setting, 'mix.daily.status:' + identity)
        progress = json.loads(row.value_json) if row else {}
        progress.update(values)
        if row: row.value_json = json.dumps(progress)
        else: session.add(Setting(key='mix.daily.status:' + identity, value_json=json.dumps(progress)))


@pytest.mark.parametrize(('state', 'active'), [
    ('waiting', True), ('running', True), ('cancelling', True),
    ('paused', False), ('interrupted', False), ('completed', False),
    ('completed_with_errors', False), ('cancelled', False)])
def test_make_timing_uses_real_utc_task_times_and_current_state(timed, db_engine, state, active):
    with session_scope(db_engine) as session:
        task = session.get(MixTask, timed); task.state = state
        task.updated_at = START + timedelta(minutes=2)
    assert read_execution(db_engine, timed)['timing'] == {
        'started_at': '2026-10-16T01:00:00+00:00',
        'last_activity_at': '2026-10-16T01:02:00+00:00', 'active': active}


@pytest.mark.parametrize(('state', 'phase', 'stopped', 'active'), [
    ('paused', 'checking_login', False, True),
    ('completed', 'checking_login', False, True),
    ('running', 'needs_login', False, False),
    ('waiting', 'needs_attention', False, False),
    ('completed', '', False, False),
    ('waiting', 'checking_login', True, False),
    ('cancelled', 'checking_login', False, False)])
def test_daily_login_stage_has_priority_including_after_render_handoff(timed, db_engine, state, phase, stopped, active):
    daily_progress(db_engine, timed, auth_phase=phase, auth_stage='before_publish', automatic_stopped=stopped)
    with session_scope(db_engine) as session:
        task = session.get(MixTask, timed); task.state = state
        task.updated_at = START + timedelta(minutes=2)
    assert read_execution(db_engine, timed)['timing']['active'] is active


def publishing(engine, paths, tmp_path, identity, states, *, authorized=True):
    account = AccountService(engine).create('douyin', 'original-user', '原账号')
    with session_scope(engine) as session: session.get(AccountRow, account.id).session_ref = 'private-session'
    videos = []
    for index in range(len(states)):
        path = tmp_path / f'{index}.mp4'; path.write_bytes(f'local-fixture-{index}'.encode())
        videos.append(FinishedVideoService(engine).import_external(path, duration_ms=1000, title=f'本地作品{index}').id)
    service = MultiPublishService(engine, paths)
    prepared = service.prepare(request_id='timing', video_ids=videos,
        targets=[{'account_id': account.id, 'platform': 'douyin'}], common_fields={}, execution_mode='publish_now')
    child = service.save(prepared['prepared_id'], request_id='timing')['id']
    daily_progress(engine, identity, publish_task_ids=[child], handoff_authorized=True)
    with session_scope(engine) as session:
        session.add(Setting(key='multi_publish.daily_source:' + child,
            value_json=json.dumps({'mix_task_id': identity, 'host_session': 'old-host'})))
        task = session.get(MixTask, identity); task.state = 'completed'
        task.updated_at = START + timedelta(minutes=2)
        deliveries = list(session.scalars(select(PublishDeliveryRow).where(
            PublishDeliveryRow.task_id == child).order_by(PublishDeliveryRow.position)))
        for delivery, state in zip(deliveries, states): session.get(TaskItemRow, delivery.item_id).state = state
        if authorized: update_authorization(session, child, add=[delivery.id for delivery in deliveries])
    return child, [delivery.id for delivery in deliveries]


@pytest.mark.parametrize(('state', 'authorized', 'active'), [
    ('waiting', True, False), ('waiting', False, False), ('running', False, True),
    ('paused_auth', True, False), ('interrupted', True, False), ('result_unknown', True, False),
    ('pre_submit_failed', True, False), ('platform_rejected', True, False),
    ('platform_accepted', True, False), ('platform_scheduled', True, False),
    ('processing', True, False), ('reviewing', True, False), ('published', True, False), ('cancelled', True, False)])
def test_publish_timing_only_runs_for_processing_targets_not_queued_authorization(
        timed, db_engine, app_paths, tmp_path, state, authorized, active):
    publishing(db_engine, app_paths, tmp_path, timed, [state], authorized=authorized)
    assert read_execution(db_engine, timed)['timing']['active'] is active


@pytest.mark.parametrize('blocked', ['batch-paused', 'target-paused', 'cancel-requested', 'hidden'])
def test_authorized_waiting_target_does_not_run_when_stopped(timed, db_engine, app_paths, tmp_path, blocked):
    child, deliveries = publishing(db_engine, app_paths, tmp_path, timed, ['waiting'])
    with session_scope(db_engine) as session:
        if blocked == 'batch-paused': session.get(TaskCenterRow, child).state = 'paused'
        if blocked == 'target-paused': session.add(Setting(key='multi_publish.target_pause:' + deliveries[0], value_json='true'))
        if blocked == 'cancel-requested': session.get(PublishDeliveryRow, deliveries[0]).cancel_requested = True
        if blocked == 'hidden': session.get(PublishDeliveryRow, deliveries[0]).hidden = True
    assert read_execution(db_engine, timed)['timing']['active'] is False


@pytest.mark.parametrize('blocked_state', ['paused_auth', 'result_unknown', 'interrupted'])
def test_parked_target_explains_same_account_queue_without_claiming_active_work(timed, db_engine, app_paths, tmp_path, blocked_state):
    _, deliveries = publishing(db_engine, app_paths, tmp_path, timed, [blocked_state, 'waiting'])
    result = read_execution(db_engine, timed)
    assert result['timing']['active'] is False
    waiting = next(target for target in result['publishing']['targets'] if target['id'] == deliveries[1])
    assert waiting['blocked_by'] == {'delivery_id': deliveries[0], 'state': blocked_state}
    assert '同一账号' in waiting['reason'] and '暂缓' in waiting['reason']
    if blocked_state != 'paused_auth':
        assert result['current']['label'] == '等待核对原结果，1项暂缓'
    daily_progress(db_engine, timed, auth_phase='needs_login')
    assert read_execution(db_engine, timed)['timing']['active'] is False


def test_other_account_running_is_kept_active_with_only_its_scope(timed, db_engine, app_paths, tmp_path):
    _, deliveries = publishing(db_engine, app_paths, tmp_path, timed, ['result_unknown', 'waiting', 'running'])
    account = AccountService(db_engine).create('douyin', 'another-user', '另一个账号')
    with session_scope(db_engine) as session:
        row = session.get(PublishDeliveryRow, deliveries[2])
        row.account_id = account.id; row.platform_user_id = 'another-user'
    result = read_execution(db_engine, timed)
    assert result['timing']['active'] is True
    assert '另一个账号' in result['current']['account_label']
    assert result['publishing']['targets'][1]['blocked_by']['delivery_id'] == deliveries[0]
    assert 'blocked_by' not in result['publishing']['targets'][2]


def test_final_readback_is_active_until_unknown_result_is_recorded(timed, db_engine, app_paths, tmp_path):
    from yijianjifa.features.publish.coordinator import AdapterOutcome
    _, deliveries = publishing(db_engine, app_paths, tmp_path, timed, ['waiting', 'waiting'])
    from yijianjifa.features.publish.history import PublishHistoryRow
    with session_scope(db_engine) as session:
        for delivery in session.scalars(select(PublishDeliveryRow)):
            session.get(PublishHistoryRow, delivery.history_id).state = 'waiting'
    service = MultiPublishService(db_engine, app_paths)
    claim = service.deliveries.claim(deliveries[0])
    service.deliveries.mark_before_final(deliveries[0], claim['digest'], owner_token=claim['owner_token'])
    reading = read_execution(db_engine, timed)
    assert reading['timing']['active'] is True
    assert reading['publishing']['targets'][0]['execution_active'] is True
    assert reading['current']['label'] == '正在核对提交结果'
    service.deliveries.record(deliveries[0], claim['digest'], owner_token=claim['owner_token'],
        outcome=AdapterOutcome('result_unknown', error_code='timeout'))
    unknown = read_execution(db_engine, timed)
    assert unknown['timing']['active'] is False
    assert unknown['publishing']['targets'][0]['execution_active'] is False
    assert unknown['current']['label'] == '等待核对原结果，1项暂缓'


@pytest.mark.parametrize('stopped', ['batch-paused', 'target-paused', 'cancel-requested'])
def test_explicitly_stopped_waiting_targets_are_not_counted_as_account_deferred(timed,db_engine,app_paths,tmp_path,stopped):
    child, deliveries = publishing(db_engine,app_paths,tmp_path,timed,['result_unknown','waiting'])
    with session_scope(db_engine) as session:
        if stopped=='batch-paused':session.get(TaskCenterRow,child).state='paused'
        if stopped=='target-paused':session.add(Setting(key='multi_publish.target_pause:'+deliveries[1],value_json='true'))
        if stopped=='cancel-requested':session.get(PublishDeliveryRow,deliveries[1]).cancel_requested=True
    result=read_execution(db_engine,timed)
    assert 'blocked_by' not in result['publishing']['targets'][1]
    assert '1项暂缓' not in result['current']['label']


def test_last_activity_uses_saved_events_and_never_setting_update_time(timed, db_engine, app_paths, tmp_path):
    _, deliveries = publishing(db_engine, app_paths, tmp_path, timed, ['pre_submit_failed'])
    daily_progress(db_engine, timed, login_log=[{'time': '2026-10-16T09:04:00+08:00',
        'step': 'daily.login_verified', 'stage': 'before_publish', 'accounts': 1}])
    with session_scope(db_engine) as session:
        session.add(Setting(key=f'mix-result:{timed}:0', value_json=json.dumps({'state': 'completed',
            'execution_log': [{'time': '2026-10-16T01:03:00Z', 'step': 'mix.completed'}]})))
        session.add(Setting(key=f'publish.execution:{deliveries[0]}:1', value_json=json.dumps([
            {'time': '2026-10-16T01:05:00Z', 'step': 'execution.result', 'checks': {'state': 'pre_submit_failed'}},
            {'time': 'invalid', 'step': 'execution.result'}]), updated_at=datetime(2030, 1, 1, tzinfo=UTC)))
        session.get(Setting, 'mix.daily.status:' + timed).updated_at = datetime(2031, 1, 1, tzinfo=UTC)
    statements = []
    def observe(connection, cursor, statement, parameters, context, many): statements.append(statement.lstrip().upper())
    event.listen(db_engine, 'before_cursor_execute', observe)
    try: timing = read_execution(db_engine, timed)['timing']
    finally: event.remove(db_engine, 'before_cursor_execute', observe)
    assert timing == {'started_at': '2026-10-16T01:00:00+00:00',
                      'last_activity_at': '2026-10-16T01:05:00+00:00', 'active': False}
    assert not any(statement.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for statement in statements)


@pytest.mark.parametrize(('created', 'updated', 'started', 'activity'), [
    (None, None, None, None),
    ('invalid', 'invalid', None, None),
    (datetime(2026, 10, 16, 9, tzinfo=timezone(timedelta(hours=8))),
     datetime(2026, 10, 16, 9, 2, tzinfo=timezone(timedelta(hours=8))),
     '2026-10-16T01:00:00+00:00', '2026-10-16T01:02:00+00:00')])
def test_missing_historical_times_stay_missing_and_aware_dates_normalize_to_utc(
        timed, db_engine, created, updated, started, activity):
    task = SimpleNamespace(snapshot_json='{}', quantity=1, success_count=0, failure_count=0,
        completed_count=0, state='paused', current_index=0, created_at=created, updated_at=updated)
    assert read_execution(db_engine, timed, task=task)['timing'] == {
        'started_at': started, 'last_activity_at': activity, 'active': False}


def test_reopen_grant_revocation_does_not_extend_saved_activity_time(timed, db_engine, app_paths, tmp_path):
    child, deliveries = publishing(db_engine, app_paths, tmp_path, timed, ['waiting'])
    with session_scope(db_engine) as session:
        session.add(Setting(key=f'publish.execution:{deliveries[0]}:1', value_json=json.dumps([
            {'time': '2026-10-16T01:05:00Z', 'step': 'execution.start'}])))
    api = DesktopApi(db_engine, app_paths)
    try:
        timing = read_execution(db_engine, timed)['timing']
        assert timing['active'] is False and timing['last_activity_at'] == '2026-10-16T01:05:00+00:00'
        assert timing['started_at'] == '2026-10-16T01:00:00+00:00'
    finally: api.close(.1)
