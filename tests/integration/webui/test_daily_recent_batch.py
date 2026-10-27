"""Recent daily results are a read projection, never resumed authority."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import event

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.mixing.queue import MixQueue, MixTask
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class NoWork:
    def render_item(self, *args): raise AssertionError('reading recent results cannot make videos')


@pytest.fixture
def recent(db_engine, app_paths, monkeypatch):
    api = DesktopApi(db_engine, app_paths)
    queue = MixQueue(db_engine, NoWork())
    def stopped(*args, **kwargs): raise AssertionError('reading recent results cannot execute or authorize work')
    monkeypatch.setattr(api._daily, 'start_check', stopped)
    monkeypatch.setattr(api._daily, 'wake_making', stopped)
    monkeypatch.setattr(api._daily, 'reconcile', stopped)
    monkeypatch.setattr(api._multi_publish, 'wake', stopped)
    monkeypatch.setattr(api._multi_publish, 'start', stopped)
    monkeypatch.setattr(api._runtime, 'start', stopped)
    def make(index, *, state='completed_with_errors', hidden=False, daily=True, auth_phase=''):
        snapshot = {'quantity': 1, 'copies': [{'title': '最近结果' + str(index)}]}
        if daily:
            snapshot['daily_publish'] = {'targets': [{'account_id': 'original-account', 'platform': 'douyin'}],
                'resolved_start': '2026-10-17T12:05:00+08:00', 'host_session': api._daily.session_id}
        task = queue.enqueue(snapshot)
        with session_scope(db_engine) as session:
            row = session.get(MixTask, task.id)
            row.state = state
            row.created_at = datetime(2026, 10, 16, tzinfo=timezone.utc) + timedelta(seconds=index)
            if state in {'completed', 'completed_with_errors'}:
                row.current_index = row.completed_count = 1
                row.success_count = int(state == 'completed')
                row.failure_count = int(state == 'completed_with_errors')
            if daily:
                session.add(Setting(key='mix.daily.status:' + task.id, value_json=json.dumps({
                    'auth_phase': auth_phase, 'handoff_authorized': True,
                    'reason_code': 'daily_making_failed', 'reason': '本批制作失败；原成品和记录保留。'})))
            if hidden: session.add(Setting(key='task-hidden:' + task.id, value_json='true'))
        return task.id
    yield api, make
    api.close(.1)


def test_recent_daily_result_keeps_its_phase_execution_and_does_not_write_or_start(recent, db_engine):
    api, make = recent
    identity = make(1)
    settings = {'publishing': {'start_date': '2026-10-18', 'time': '12:05'}}
    with session_scope(db_engine) as session:
        session.add(Setting(key='mix.daily.settings.v1', value_json=json.dumps(settings)))
    statements = []
    def observe(connection, cursor, statement, parameters, context, many): statements.append(statement.lstrip().upper())
    event.listen(db_engine, 'before_cursor_execute', observe)
    try: data = api._daily.read()
    finally: event.remove(db_engine, 'before_cursor_execute', observe)
    assert data['active_batch'] is None
    assert data['recent_batch'] is None
    result = api._daily.status(identity)
    assert result['id'] == identity and result['phase'] == 'needs_attention' and not result['can_continue']
    assert result['execution']['making'] == {'completed': 1, 'total': 1, 'succeeded': 0, 'failed': 1}
    assert result['reason'] == '本批制作失败；原成品和记录保留。'
    assert data['settings'] == settings
    assert not any(statement.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for statement in statements)
    with session_scope(db_engine) as session:
        assert session.get(MixTask, identity).state == 'completed_with_errors'
        assert not list(session.query(Setting).filter(Setting.key.startswith('multi_publish.authorization:')))


def test_recent_skips_hidden_cancelled_and_make_only_tasks(recent):
    api, make = recent
    visible = make(1)
    make(2, hidden=True)
    make(3, state='cancelled')
    make(4, state='cancelling')
    make(5, daily=False)
    assert api._daily.read()['recent_batch'] is None
    assert api._daily.status(visible)['phase']=='needs_attention'


def test_active_batch_always_takes_priority_over_recent_results(recent):
    api, make = recent
    make(1)
    active = make(2, state='waiting')
    data = api._daily.read()
    assert data['active_batch']['id'] == active
    assert data['recent_batch'] is None


def test_a_recent_result_that_now_requires_continue_is_not_promoted_to_active(recent, monkeypatch):
    api, make = recent
    identity = make(1)
    status = api._daily.status
    monkeypatch.setattr(api._daily, 'status', lambda task_id: {**status(task_id), 'can_continue': True})
    data = api._daily.read()
    assert data['active_batch'] is None and data['recent_batch'] is None
    assert status(identity)['phase'] == 'needs_attention'


def test_empty_daily_read_has_no_recent_or_active_batch(recent):
    data = recent[0]._daily.read()
    assert data['active_batch'] is None and data['recent_batch'] is None


@pytest.mark.parametrize('phase',['needs_attention','needs_login'])
@pytest.mark.parametrize('state',['paused','waiting'])
def test_pending_batches_stay_in_tasks_without_occupying_new_making(recent,phase,state):
    api,make=recent
    identity=make(1,state=state,auth_phase=phase)
    data=api._daily.read()
    assert data['active_batch'] is None and data['recent_batch'] is None
    assert api._daily.status(identity)['phase']==phase


@pytest.mark.parametrize('phase', ['accepted', 'completed'])
def test_recent_keeps_the_existing_status_projection_without_relabeling(recent, monkeypatch, phase):
    api, make = recent
    identity = make(1, state='completed')
    saved = {**api._daily.status(identity), 'phase': phase}
    monkeypatch.setattr(api._daily, 'status', lambda task_id: saved)
    data = api._daily.read()
    assert data['active_batch'] is None and data['recent_batch'] == saved
    assert data['recent_batch']['phase'] == phase


def test_reopening_an_ended_batch_exposes_history_without_reauthorizing(recent, db_engine, app_paths):
    api, make = recent
    identity = make(1)
    reopened = DesktopApi(db_engine, app_paths)
    try:
        data = reopened._daily.read()
        assert data['active_batch'] is None and data['recent_batch'] is None
        assert reopened._daily.status(identity)['phase'] == 'needs_attention'
        assert not reopened._daily.status(identity)['can_continue']
        with session_scope(db_engine) as session:
            assert session.get(MixTask, identity).state == 'completed_with_errors'
            assert not list(session.query(Setting).filter(Setting.key.startswith('multi_publish.authorization:')))
    finally: reopened.close(.1)


@pytest.mark.parametrize('state', ['result_unknown', 'interrupted'])
def test_ended_batch_with_unresolved_publishing_is_recent_read_only(recent, db_engine, monkeypatch, state):
    """重开可看到原结果和账号暂缓，不把显示记录当成继续授权。"""
    api, make = recent
    identity = make(1, state='completed')
    saved = {**api._daily.status(identity), 'can_continue': False, 'execution': {
        'publishing': {'targets': [{'id': 'unknown', 'state': state},
                                  {'id': 'waiting', 'state': 'waiting',
                                   'blocked_by': {'delivery_id': 'unknown', 'state': state}}]},
        'timing': {'active': False}}}
    monkeypatch.setattr(api._daily, 'status', lambda task_id: saved)
    statements = []
    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().upper())
    event.listen(db_engine, 'before_cursor_execute', observe)
    try: data = api._daily.read()
    finally: event.remove(db_engine, 'before_cursor_execute', observe)
    assert data['active_batch'] is None
    assert data['recent_batch'] == saved
    assert not data['recent_batch']['can_continue']
    assert not data['recent_batch']['execution']['timing']['active']
    assert not any(statement.startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for statement in statements)


def test_cleared_latest_does_not_restore_older_completed_batch(recent, app_paths):
    api, make = recent
    make(1, state='completed')
    latest = make(2, state='completed')
    from yijianjifa.app_services.task_removal import remove_task
    remove_task(api._daily.engine, latest)
    assert api._daily.read()['recent_batch'] is None
    assert api._daily.read()['active_batch'] is None
    # 用新的服务实例读取同一测试库，验证持久化而非页面内存隐藏。
    reopened = DesktopApi(api._daily.engine, app_paths)
    try:
        assert reopened._daily.read()['recent_batch'] is None
        assert reopened._daily.read()['active_batch'] is None
    finally:
        reopened.close(.1)
