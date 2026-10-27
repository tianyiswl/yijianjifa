"""Inspection presents the same safe reason and grants no publishing authority."""
from yijianjifa.features.publish.coordinator import AdapterOutcome
from test_multi_publish_inspection import (
    InspectionAdapter, assert_no_publish_records, call, case, prepare, wait,
)


def test_topic_only_tag_rejection_is_actionable_in_inspection(case, db_engine, monkeypatch):
    api, _, _, adapter = case
    def rejected(self, payload, *, context, before_final):
        return AdapterOutcome('pre_submit_failed', error_code='bilibili_tag_requires_topic')
    monkeypatch.setattr(InspectionAdapter, 'submit', rejected)
    response = call(api, 'inspection.start', {'prepared_id': prepare(case)}, 'tag-check')
    assert response['ok'], response
    status = wait(api, response['data']['inspection_id'], 'failed')
    assert status['error_code'] == 'bilibili_tag_requires_topic'
    assert 'B站有标签只能通过话题入口添加，请移除或通过话题入口选择后再发布' in status['message']
    assert status['submitted'] is False and adapter.clones[0].closed
    assert_no_publish_records(db_engine)
