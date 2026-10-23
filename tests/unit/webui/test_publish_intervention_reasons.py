"""A missing identity observation is not evidence of a login challenge."""
from types import SimpleNamespace
from pathlib import Path

import pytest

from yijianjifa.webui.chromium_publish_session import ChromiumPublishSessionAdapter, LiveSession
from yijianjifa.webui.publish_intervention import InterventionBroker


def test_identity_unavailable_explains_account_check_without_granting_resume():
    adapter = ChromiumPublishSessionAdapter(None, SimpleNamespace(data_dir=Path('/tmp/p011-offline-reasons')))
    adapter._account = lambda s: SimpleNamespace(id='fixture', nickname='合成账号')
    adapter._refresh_intervention_frame = lambda *a, **k: False
    session = LiveSession({}, {'delivery_id': 'fixture', 'attempt': 1, 'digest': 'fixture'})

    result = adapter._park(session, 'login', '程序尚未确认所选账号。', reason='identity_unavailable')
    request = session.broker.status()['request']
    assert result.state == 'paused_auth' and request['reason'] == 'identity_unavailable'
    assert session.stage == '等待重新核对所选账号'
    assert session.resume_authorized is False and session.version_grant is None
    assert session.final is False

    original = request['id']
    adapter._park(session, 'login', '请登录原账号。', reason='login_required')
    updated = session.broker.status()['request']
    assert updated['id'] != original and updated['reason'] == 'login_required'
    with pytest.raises(ValueError):
        session.broker.respond(original, 'done')


@pytest.mark.parametrize('kind,reason', [
    ('login', 'raw-page-error'), ('complex', 'identity_unavailable'),
    ('login', 'sms_required'), ('sms', 'challenge_required'), ('login', {}),
])
def test_diagnostics_are_finite_and_cannot_change_action_type(kind, reason):
    broker = InterventionBroker()
    with pytest.raises(ValueError):
        broker.request('fixture', '合成账号', kind, '合成说明', reason=reason)
    assert broker.status()['request'] is None


@pytest.mark.parametrize('kind,reason', [('login', 'login_required'), ('sms', 'sms_required'), ('complex', 'challenge_required')])
def test_existing_challenge_requests_get_explicit_safe_reason(kind, reason):
    broker = InterventionBroker()
    broker.request('fixture', '合成账号', kind, '合成说明')
    assert broker.status()['request']['reason'] == reason
