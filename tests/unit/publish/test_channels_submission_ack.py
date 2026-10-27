"""The bounded internal ACK is a receipt fact, never a published-work ID."""
from copy import deepcopy

import pytest

from yijianjifa.features.publish import coordinator as module


DIGEST = 'a' * 64


def ack(request_digest=DIGEST):
    return {'kind': 'channels_create_ack', 'platform': 'channels',
            'request_digest': request_digest, 'http_status': 201,
            'outer_code': 0, 'inner_code': 0, 'request_matched': True}


def evidence():
    return module.outcome_evidence(module.AdapterOutcome('platform_accepted', submission_ack=ack()), DIGEST)


def test_ack_evidence_is_bounded_and_does_not_invent_a_reference():
    result = evidence()
    assert result['state'] == 'platform_accepted' and result['receipt_reference'] == {}
    assert result['submission_ack'] == ack()
    assert module.has_channels_immediate_ack(result, platform='channels', execution_mode='publish_now',
        request_digest=DIGEST, final_started=True)
    assert 'submission_ack' not in module.outcome_evidence(module.AdapterOutcome('result_unknown'), DIGEST)


@pytest.mark.parametrize('field,value', [
    ('kind', 'unverified'), ('platform', 'douyin'), ('request_digest', 'b' * 64),
    ('http_status', 500), ('http_status', 199), ('http_status', True), ('http_status', '201'),
    ('outer_code', False), ('outer_code', 1), ('inner_code', False), ('inner_code', -1),
    ('request_matched', 1), ('request_matched', False), ('private', 'not-permitted'),
])
def test_ack_rejects_loose_or_unbound_values(field, value):
    value_ack = ack(); value_ack[field] = value
    with pytest.raises(ValueError):
        module.outcome_evidence(module.AdapterOutcome('platform_accepted', submission_ack=value_ack), DIGEST)


@pytest.mark.parametrize('state', ['published', 'platform_scheduled', 'processing', 'reviewing', 'result_unknown'])
def test_ack_cannot_promote_to_other_receipt_or_public_states(state):
    with pytest.raises(ValueError):
        module.outcome_evidence(module.AdapterOutcome(state, submission_ack=ack()), DIGEST)


@pytest.mark.parametrize('change', [
    {'platform': 'bilibili'}, {'execution_mode': 'platform_schedule'}, {'request_digest': 'b' * 64},
    {'final_started': False}, {'final_started': 1},
])
def test_ack_is_only_valid_on_the_exact_final_immediate_channels_attempt(change):
    args = dict(platform='channels', execution_mode='publish_now', request_digest=DIGEST, final_started=True)
    args.update(change)
    assert not module.has_channels_immediate_ack(evidence(), **args)


@pytest.mark.parametrize('change', [
    {'state': 'result_unknown'}, {'error_code': 'timeout'}, {'scheduled_at': '2026-10-25T10:00:00Z'},
    {'protocol_violation': 'receipt_without_final_marker'}, {'request_digest': 'b' * 64},
    {'submission_ack': None},
])
def test_incomplete_or_protocol_invalid_old_evidence_is_not_confirmation(change):
    result = deepcopy(evidence()); result.update(change)
    assert not module.has_channels_immediate_ack(result, platform='channels', execution_mode='publish_now',
        request_digest=DIGEST, final_started=True)
