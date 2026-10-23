"""Navigation recovery uses the existing account and durable final boundary."""
from playwright.sync_api import Error as PlaywrightError
from sqlalchemy import select

from test_channels_publish_boundary import channels, freeze, command, journal
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
from yijianjifa.storage.db import session_scope


def test_transient_initial_navigation_recovers_one_upload_and_one_final(channels, db_engine, monkeypatch):
    api, _, _, adapter, events, _ = channels
    original = adapter._challenge; calls = []
    def moving(session):
        calls.append(session.page)
        if len(calls) == 1: raise PlaywrightError('Execution context was destroyed because of a navigation')
        return original(session)
    monkeypatch.setattr(adapter, '_challenge', moving)
    batch, did = freeze(channels, db_engine, 'platform_schedule')
    assert api._multi_publish.coordinator.run_once(batch, [did]) == 'platform_scheduled'
    assert all(page is calls[0] for page in calls)
    assert events.count('upload') == events.count('final') == 1
    assert journal(db_engine, did)[0] == journal(db_engine, did)[1]['request_digest']
    assert api._multi_publish.coordinator.run_once(batch, [did]) is None


def test_persistent_initial_navigation_retains_page_without_granting_resume(channels, db_engine, monkeypatch):
    api, _, _, adapter, events, _ = channels
    original = adapter._challenge
    def moving(session): raise PlaywrightError('Execution context was destroyed because of a navigation SECRET')
    monkeypatch.setattr(adapter, '_challenge', moving)
    monkeypatch.setattr(api._multi_publish, 'enqueue', lambda *args: None)
    batch, did = freeze(channels, db_engine, 'platform_schedule')
    coordinator = api._multi_publish.coordinator
    assert coordinator.run_once(batch, [did]) == 'paused_auth'
    session = next(iter(adapter.sessions.values()));page = session.page
    assert page is not None and not session.final and not session.resume_authorized
    assert not any(event in {'upload', 'final', 'close'} for event in events)
    assert coordinator.run_once(batch, [did]) is None
    with session_scope(db_engine) as db:
        attempt = db.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == did))
        assert attempt.final_json is None
    monkeypatch.setattr(adapter, '_challenge', original)
    request = adapter.interventions()[0]
    command(api, 'intervention.respond', {'id': request['id'], 'delivery_id': did,
        'action': 'done', 'consent_same_identity': True}, 'navigation-explicit-done')
    assert coordinator.resume_verification(did) == 'platform_scheduled'
    assert events.count('upload') == events.count('final') == 1
    assert journal(db_engine, did)[0] == journal(db_engine, did)[1]['request_digest']


def test_navigation_at_final_account_recheck_does_not_skip_identity_or_repeat_upload(channels, db_engine, monkeypatch):
    api, _, _, adapter, events, _ = channels
    original = adapter._identity; interrupted = []
    def moving(session):
        if session.form_filled and not session.final and not interrupted:
            interrupted.append(session.page)
            assert events.count('upload') == 1 and events.count('final') == 0
            raise PlaywrightError('Frame has been detached')
        return original(session)
    monkeypatch.setattr(adapter, '_identity', moving)
    batch, did = freeze(channels, db_engine, 'platform_schedule')
    assert api._multi_publish.coordinator.run_once(batch, [did]) == 'platform_scheduled'
    assert len(interrupted) == 1 and events.count('upload') == events.count('final') == 1
    assert journal(db_engine, did)[0] == journal(db_engine, did)[1]['request_digest']
