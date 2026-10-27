"""逐条进度投影只读，当前内容与计时不能停留在上一条发布记录。"""
import json
import pytest
from sqlalchemy import select
from yijianjifa.app_services import daily_serial_flow, mix_execution
from yijianjifa.features.mixing.queue import MixQueue, MixTask
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


@pytest.mark.parametrize('stage,state,reason,label,active', [
    ('making', 'running', 'none', '正在制作', True),
    ('publishing', 'waiting_publish', 'receipt', '等待平台确认接收', False),
    ('waiting_schedule', 'waiting_publish', 'schedule', '等待定时时间', False),
    ('paused', 'paused', 'authorization', '已暂停，等待继续', False),
])
def test_serial_projection_preserves_current_item_and_real_activity(db_engine, monkeypatch, stage, state, reason, label, active):
    task = MixQueue(db_engine, lambda *_: pytest.fail('只读进度不能执行制作')).enqueue({'quantity': 3, 'copies': [{'title': '作品一'}, {'title': '作品二'}, {'title': '作品三'}]})
    with session_scope(db_engine) as session:
        row = session.get(MixTask, task.id)
        row.state = state
        row.current_index = row.completed_count = row.success_count = 1
    serial = dict(mode='per_item_v1', quantity=3, item_index=1, item_number=2, item_title='作品二',
        stage=stage, waiting_reason=reason, target_count=2, item_received=0, items_received=1,
        targets_received=2, targets_total=6, error_count=0, reason='')
    monkeypatch.setattr(daily_serial_flow, 'read_serial_progress', lambda session, row: serial)
    # 已有上一条child，即使它已不再执行，也不能令第二条制作的计时停止。
    monkeypatch.setattr(mix_execution, '_owned_publish_ids', lambda *args: (['prior-child'], True))
    with session_scope(db_engine) as session:
        before = [(r.key, r.value_json) for r in session.scalars(select(Setting).order_by(Setting.key))]
    result = mix_execution.read_execution(db_engine, task.id)
    assert result['serial'] == serial
    assert result['current']['index'] == 2 and result['current']['total'] == 3
    assert result['current']['title'] == '作品二'
    assert result['current']['label'] == label
    assert 'account_label' not in result['current']
    assert result['timing']['active'] is active
    with session_scope(db_engine) as session:
        assert [(r.key, r.value_json) for r in session.scalars(select(Setting).order_by(Setting.key))] == before
        assert session.get(MixTask, task.id).current_index == 1


def test_legacy_projection_does_not_invent_serial_flow(db_engine):
    task = MixQueue(db_engine, lambda *_: pytest.fail('只读进度不能执行制作')).enqueue({'quantity': 1, 'copies': [{'title': '原有仅制作'}]})
    result = mix_execution.read_execution(db_engine, task.id)
    assert 'serial' not in result


def test_serial_internal_error_has_a_timestamped_safe_event_for_panel_and_copy(db_engine):
    task = MixQueue(db_engine, lambda *_: pytest.fail('只读进度不能执行制作')).enqueue({'quantity': 1, 'copies': [{'title': '诊断测试'}]})
    with session_scope(db_engine) as session:
        session.add(Setting(key='mix.daily.diagnostics:'+task.id, value_json=json.dumps([
            {'time':'2026-10-26T09:35:00+00:00','step':'daily.serial_recheck_failed',
             'error_type':'ValueError','reason':'Cookie=private-token C:/private/profile'},
            {'time':'2026-10-26T09:36:00+00:00','step':'private.step','error_type':'private-token'},
            {'time':'2026-10-26T09:37:00+00:00','step':[],'error_type':{}},
        ])))
    result = mix_execution.read_execution(db_engine, task.id)
    events = [event for event in result['events'] if event['step']=='daily.serial_recheck_failed']
    assert len(events)==1
    assert events[0]['time']=='2026-10-26T09:35:00+00:00'
    assert 'ValueError' in events[0]['message'] and '异常' in events[0]['message']
    assert 'private' not in json.dumps(result) and 'Cookie' not in json.dumps(result)
