"""Strict natural ACK observation and durable no-ID recovery are offline."""
from copy import deepcopy

import pytest

from test_channels_multi_publish import case, normal_request, response
from yijianjifa.features.publish.coordinator import has_channels_immediate_ack
from yijianjifa.features.publish.deliveries import canonical, digest, PublishDeliveryRow, DeliveryAttemptRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui import channels_publish_form as form


def proof(key):
    return {'kind': 'channels_create_ack', 'platform': 'channels', 'request_digest': key,
        'http_status': 201, 'outer_code': 0, 'inner_code': 0, 'request_matched': True}


@pytest.mark.parametrize('status', [200, 201, 202, 299])
def test_only_the_natural_matched_double_zero_create_can_prove_acceptance(case, status):
    adapter, payload, _, _, _, media = case
    payload['execution_mode'] = 'publish_now'
    adapter.bind_delivery(digest=digest(payload), delivery_id='target', task_id='batch')
    session = adapter._session(payload); adapter._init_observations(session)
    session.final = True; session.owned_media = media
    adapter._response(session, response(form.CREATE_PATH, normal_request(form.validate_form(payload), media),
        {'errCode': 0, 'data': {'baseResp': {'errcode': 0}}}, status=status))
    result = adapter._accepted_outcome(session, form.validate_form(payload))
    assert result.state == 'platform_accepted' and result.receipt_reference == {}
    assert result.submission_ack == {**proof(digest(payload)), 'http_status': status}
    adapter.close()


@pytest.mark.parametrize('bad', ['missing', 'unmatched', 'not_final', 'outer_false', 'inner_false', 'rejected', 'non_2xx'])
def test_no_id_acceptance_is_never_inferred_from_submit_ok_or_a_loose_response(case, bad):
    adapter, payload, _, _, _, media = case
    payload['execution_mode'] = 'publish_now'
    adapter.bind_delivery(digest=digest(payload), delivery_id='target', task_id='batch')
    session = adapter._session(payload); adapter._init_observations(session)
    session.final = bad != 'not_final'; session.owned_media = media
    request = normal_request(form.validate_form(payload), media)
    data = {'errCode': 0, 'data': {'baseResp': {'errcode': 0}}}
    if bad == 'unmatched': request['objectDesc']['description'] += 'foreign'
    if bad == 'outer_false': data['errCode'] = False
    if bad == 'inner_false': data['data']['baseResp']['errcode'] = False
    if bad == 'rejected': data['data']['baseResp']['errcode'] = 1
    if bad != 'missing':
        adapter._response(session, response(form.CREATE_PATH, request, data, status=500 if bad == 'non_2xx' else 201))
    # An old boolean alone has never proved what request was accepted.
    session.submit_ok = True
    result = adapter._accepted_outcome(session, form.validate_form(payload))
    assert result.state == 'result_unknown' and result.receipt_reference is None
    assert result.submission_ack is None
    adapter.close()


@pytest.mark.parametrize('change', [None, {'submission_ack': None}, {'state': 'result_unknown'},
    {'protocol_violation': 'invalid_submitted_receipt'}, {'submission_ack': proof('b' * 64)}])
def test_durable_no_id_ack_is_bound_to_the_exact_frozen_final_attempt(case, monkeypatch, change):
    adapter, payload, _, events, _, _ = case
    payload['execution_mode'] = 'publish_now'
    key = digest(payload); snapshot = canonical(payload)
    result = {'state': 'platform_accepted', 'request_digest': key, 'scheduled_at': None,
        'receipt_reference': {}, 'error_code': None, 'submission_ack': proof(key)}
    if change: result.update(deepcopy(change))
    with session_scope(adapter.engine) as db:
        db.add(PublishDeliveryRow(id='target', task_id='batch', item_id='item', history_id='history',
            platform='channels', account_id=payload['account_id'], platform_user_id=payload['platform_user_id'],
            video_id=payload['video_id'], video_hash=payload['video_hash'], snapshot_json=snapshot, request_digest=key,
            execution_mode='publish_now', scheduled_at=None, attempt=1, position=0))
        db.add(DeliveryAttemptRow(id='attempt', delivery_id='target', number=1, snapshot_json=snapshot, request_digest=key,
            final_json=canonical({'request_digest': key, 'state': 'result_unknown'}), result_json=canonical(result)))
    adapter.bindings.clear()
    session = adapter._session(payload); session.final = True
    durable = adapter._persisted_ack(session)
    if change:
        assert durable is None
    else:
        monkeypatch.setattr(adapter, '_open', lambda *args, **kwargs: pytest.fail('durable ACK requires no browser'))
        actual = adapter.reconcile(payload, context=None)
        assert actual.state == 'platform_accepted' and actual.receipt_reference == {}
        assert actual.submission_ack == proof(key)
        assert not any(k in {'upload', 'final'} for k, _ in events)
    adapter.close()
