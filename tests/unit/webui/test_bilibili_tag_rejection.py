"""A proved ordinary-tag rejection stops before final and stays actionable."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from yijianjifa.app_services.mix_execution import read_execution
from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.app_services.multi_publish_dto import detail
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.features.publish.coordinator import PublishCoordinator
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_bilibili_multi_publish import case


CODE = 'bilibili_tag_requires_topic'
MESSAGE = 'B站有标签只能通过话题入口添加，请移除或通过话题入口选择后再发布'


def reject_tag(*args, **kwargs):
    # The response-to-current-input binding is covered separately. This is the
    # finite signal emitted only after that natural rejection is established.
    raise ValueError(CODE)


@pytest.mark.parametrize('inspection', [False, True])
def test_bound_tag_rejection_stops_without_final_or_changing_intent(case, monkeypatch, inspection):
    adapter, payload, _, events, _ = case
    adapter.inspection_only = inspection
    frozen = deepcopy(payload)
    monkeypatch.setattr(adapter, '_prepare_form', reject_tag)
    try:
        outcome = adapter.submit(payload, context=None,
            before_final=lambda: events.append(('journal', None)))
        assert outcome.state == 'pre_submit_failed' and outcome.error_code == CODE
        assert payload == frozen
        assert [name for name, _ in events if name in {'upload', 'journal', 'final'}] == ['upload']
        assert adapter._session(payload).final is False
    finally:
        adapter.close()


@pytest.mark.parametrize('final,query,error', [
    (True, False, ValueError(CODE)),
    (False, True, ValueError(CODE)),
    (False, False, ValueError(CODE + ' private details')),
    (False, False, RuntimeError(CODE)),
])
def test_tag_reason_cannot_relabel_query_final_or_unproved_exceptions(case, monkeypatch, final, query, error):
    adapter, payload, _, _, _ = case
    session = adapter._session(payload)
    session.final = final
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(adapter, '_advance_steps', fail)
    try:
        with pytest.raises(type(error)):
            adapter._advance(session, query=query)
    finally:
        adapter.close()


def test_tag_rejection_survives_task_and_mixing_views_without_automatic_retry(case, app_paths, monkeypatch):
    adapter, payload, _, events, _ = case
    service = MultiPublishService(adapter.engine, app_paths,
        catalog=PlatformCatalog(transports={'bilibili': adapter}))
    service.capture_account = lambda account: dict(session_fingerprint=payload['session_fingerprint'])
    video = FinishedVideoService(adapter.engine).import_external(payload['video_path'],
        duration_ms=1000, title=payload['fields']['title'])
    common = {key: value for key, value in payload['fields'].items() if key in {'title', 'body', 'tags'}}
    special = {key: value for key, value in payload['fields'].items() if key not in common}
    prepared = service.prepare(request_id='tag-rejection', video_ids=[video.id],
        targets=[dict(account_id=payload['account_id'], platform='bilibili', fields=special)],
        common_fields=common, execution_mode='publish_now')
    saved = service.save(prepared['prepared_id'], request_id='tag-rejection')
    monkeypatch.setattr(adapter, '_prepare_form', reject_tag)
    coordinator = PublishCoordinator(service)
    try:
        assert coordinator.run_once(saved['id']) == 'pre_submit_failed'
        child = detail(service, saved['id'], coordinator=coordinator)['deliveries'][0]
        assert child['error_code'] == CODE and MESSAGE in child['reason_message']
        assert child['final_started'] is False and child['can_submit'] is False
        assert coordinator.run_once(saved['id']) is None
        assert [name for name, _ in events if name in {'upload', 'journal', 'final'}] == ['upload']
        with session_scope(adapter.engine) as db:
            delivery = db.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == saved['id']))
            attempt = db.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery.id))
            assert attempt.final_json is None
            assert json.loads(attempt.result_json)['error_code'] == CODE
            assert json.loads(attempt.snapshot_json)['fields']['tags'] == '#功能测试 #剪辑测试'
        parent = MixQueue(adapter.engine, SimpleNamespace()).enqueue({'quantity': 1,
            'copies': [{'title': '本批作品'}], 'daily_publish': {'targets': [
                {'account_id': payload['account_id'], 'platform': 'bilibili'}]}})
        with session_scope(adapter.engine) as db:
            db.add(Setting(key='multi_publish.daily_source:' + saved['id'],
                value_json=json.dumps({'mix_task_id': parent.id})))
            db.add(Setting(key='mix.daily.status:' + parent.id,
                value_json=json.dumps({'publish_task_ids': [saved['id']]})))
        target = read_execution(adapter.engine, parent.id)['publishing']['targets'][0]
        assert target['state'] == 'pre_submit_failed' and MESSAGE in target['reason']
    finally:
        adapter.close()
