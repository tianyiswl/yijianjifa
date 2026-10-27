"""真实持久化边界上的回执诊断与复制，不运行平台页面。"""
import json

from yijianjifa.app_services.task_center_access import TaskCenterAccess
from yijianjifa.features.publish.coordinator import AdapterOutcome, PublishCoordinator
from yijianjifa.features.publish.execution_log import append_event

from test_multi_publish_failure_diagnostics import frozen


def test_post_final_result_gate_survives_cold_detail_and_copy_without_resubmission(frozen, db_engine, monkeypatch):
    service, adapter, task_id = frozen
    calls = []
    def uncertain(payload, *, context, before_final):
        before_final(); calls.append('one-final')
        return AdapterOutcome('result_unknown', error_code='timeout')
    monkeypatch.setattr(adapter, 'submit', uncertain)
    coordinator = PublishCoordinator(service)
    assert coordinator.run_once(task_id) == 'result_unknown'
    target = service.get_task(task_id)['deliveries'][0]
    append_event(db_engine, target['id'], 1, 'douyin.result_readback', {
        'ready': True, 'final_started': True, 'page_kind': 'manage',
        'result_count': 1, 'schedule_match_count': 0, 'stable_reads': 2,
        'review_state': 'missing', 'receipt_gate': 'no_schedule_match'})
    access = TaskCenterAccess(db_engine)
    data = access.detail(task_id)['deliveries'][0]
    assert data['state'] == 'result_unknown' and not data['can_submit']
    assert '排期匹配0条' in data['reason_message'] and '请勿重复投稿' in data['reason_message']
    text = access.execution_log_text(task_id)
    assert '诊断摘要：平台结果回读' in text and '排期匹配0条' in text
    assert 'receipt_gate' in text and 'no_schedule_match' in text
    assert coordinator.run_once(task_id) is None
    assert calls == ['one-final']
    assert json.loads(json.dumps(data))['state'] == 'result_unknown'
