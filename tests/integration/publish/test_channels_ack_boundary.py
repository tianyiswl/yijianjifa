"""Synthetic frozen attempts prove ACK persistence without any platform access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from test_multi_publish_v2 import setup_v2
from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.features.publish.coordinator import AdapterOutcome, PublishCoordinator
from yijianjifa.features.publish.deliveries import digest
from yijianjifa.storage.db import session_scope


def ack(key):
    return {'kind': 'channels_create_ack', 'platform': 'channels', 'request_digest': key,
            'http_status': 201, 'outer_code': 0, 'inner_code': 0, 'request_matched': True}


def saved(setup, *, platform='channels', final=True, bad=None, execution_mode='publish_now'):
    service, video, accounts, adapters = setup
    account = next(a for a in accounts if a.platform == platform)
    prepared = service.prepare(request_id='ack-case', video_ids=[video.id],
        targets=[{'platform': platform, 'account_id': account.id}], common_fields={}, execution_mode=execution_mode,
        scheduled_at=(datetime.now(timezone.utc) + timedelta(hours=4)).isoformat() if execution_mode != 'publish_now' else None)
    receipt = service.save(prepared['prepared_id'], request_id='ack-case')
    adapter = adapters[platform]
    def submit(payload, *, context, before_final):
        adapter.calls.append(('submit', payload['video_id']))
        if final: before_final()
        proof = ack(digest(payload))
        if bad: proof.update(bad)
        return AdapterOutcome('platform_accepted', submission_ack=proof)
    adapter.submit = submit
    coordinator = PublishCoordinator(service)
    state = coordinator.run_once(receipt['id'])
    delivery = service.get_task(receipt['id'])['deliveries'][0]
    return service, adapter, receipt, delivery, state


def test_record_ack_only_acceptance_is_durable_private_and_not_publication(setup_v2, db_engine):
    service, adapter, receipt, delivery, state = saved(setup_v2)
    assert state == delivery['state'] == 'platform_accepted'
    assert delivery['receipt_summary']['reference'] == {}
    assert 'submission_ack' not in json.dumps(service.get_task(receipt['id']))
    with session_scope(db_engine) as db:
        row, item, history, attempt, *_ = service.deliveries.load(db, delivery['id'])
        result = json.loads(attempt.result_json)
        assert result['submission_ack'] == ack(row.request_digest)
        assert item.state == history.state == 'platform_accepted' and attempt.final_json
        assert result['receipt_reference'] == {}
    assert PublishCoordinator(service).run_once(receipt['id']) is None
    assert [v[0] for v in adapter.calls] == ['submit']


@pytest.mark.parametrize('change', [
    {'platform': 'douyin'}, {'execution_mode': 'platform_schedule'}, {'final': False}, {'bad': {'request_digest': 'b' * 64}},
    {'bad': {'http_status': 500}}, {'bad': {'inner_code': False}},
])
def test_record_does_not_globally_allow_empty_or_invalid_receipts(setup_v2, change):
    _, _, _, delivery, state = saved(setup_v2, **change)
    assert state == delivery['state'] == 'result_unknown'


def test_invalid_ack_never_persists_a_native_or_sensitive_payload(setup_v2, db_engine):
    service, _, _, delivery, state = saved(setup_v2, bad={'private': 'synthetic-sensitive-do-not-store'})
    assert state == 'result_unknown'
    with session_scope(db_engine) as db:
        _, _, _, attempt, *_ = service.deliveries.load(db, delivery['id'])
        assert 'synthetic-sensitive' not in attempt.result_json
        assert 'submission_ack' not in json.loads(attempt.result_json)


def test_reopened_failed_queries_retain_the_original_bound_ack_without_resubmit(setup_v2, db_engine):
    service, adapter, receipt, delivery, state = saved(setup_v2)
    assert state == 'platform_accepted'
    with session_scope(db_engine) as db:
        row, _, _, attempt, *_ = service.deliveries.load(db, delivery['id'])
        frozen = (row.snapshot_json, row.request_digest, attempt.final_json, attempt.result_json)
    reopened = MultiPublishService(service.engine, service.paths, catalog=service.catalog,
        workers=service.deliveries.workers)
    reopened.recover_after_restart()
    for reason in ('timeout', 'verification_required'):
        def query(payload, *, context):
            adapter.calls.append(('reconcile', payload['video_id']))
            return AdapterOutcome('result_unknown', error_code=reason)
        adapter.reconcile = query
        assert reopened.recover_delivery(delivery['id']) == {
            'id': delivery['id'], 'state': 'platform_accepted', 'resubmitted': False}
        with session_scope(db_engine) as db:
            row, item, history, attempt, *_ = service.deliveries.load(db, delivery['id'])
            assert frozen == (row.snapshot_json, row.request_digest, attempt.final_json, attempt.result_json)
            assert item.state == history.state == 'platform_accepted'
            assert json.loads(attempt.recovery_json)['events'][-1]['retained_confirmed_result'] is True
    assert PublishCoordinator(reopened).run_once(receipt['id']) is None
    assert [v[0] for v in adapter.calls] == ['submit', 'reconcile', 'reconcile']


def test_recovery_accepts_a_valid_new_query_ack_but_cannot_upgrade_without_proof(setup_v2, db_engine):
    service, adapter, _, delivery, _ = saved(setup_v2, bad={'inner_code': 1})
    adapter.reconcile = lambda payload, context: AdapterOutcome('platform_accepted')
    assert service.recover_delivery(delivery['id'])['state'] == 'result_unknown'
    adapter.reconcile = lambda payload, context: AdapterOutcome('platform_accepted',
        submission_ack=ack(digest({k: v for k, v in payload.items() if k != 'receipt_reference'})))
    assert service.recover_delivery(delivery['id'])['state'] == 'platform_accepted'
    with session_scope(db_engine) as db:
        row, _, _, attempt, *_ = service.deliveries.load(db, delivery['id'])
        assert json.loads(attempt.result_json)['submission_ack'] == ack(row.request_digest)


@pytest.mark.parametrize('old_change', [
    {'state': 'result_unknown'}, {'protocol_violation': 'receipt_without_final_marker'},
    {'submission_ack': None}, {'request_digest': 'b' * 64},
])
def test_recovery_never_reconfirms_unproven_old_records(setup_v2, db_engine, old_change):
    service, adapter, _, delivery, _ = saved(setup_v2)
    with session_scope(db_engine) as db:
        _, item, history, attempt, *_ = service.deliveries.load(db, delivery['id'])
        result = json.loads(attempt.result_json); result.update(deepcopy(old_change))
        item.state = history.state = result['state']
        attempt.result_json = json.dumps(result)
    adapter.reconcile = lambda payload, context: AdapterOutcome('result_unknown', error_code='timeout')
    if 'request_digest' in old_change:
        with pytest.raises(ValueError, match='journal digest mismatch'):
            service.recover_delivery(delivery['id'])
    else:
        assert service.recover_delivery(delivery['id'])['state'] == 'result_unknown'
