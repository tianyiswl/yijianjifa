"""Actual channels adapter + frozen coordinator boundary; no platform IO."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_runtime import update_authorization
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterOutcome, outcome_evidence
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
from yijianjifa.webui import channels_publish_form as form


def command(api, operation, params, request_id):
    response = api.dispatch(json.dumps({'v': 1, 'request_id': request_id,
        'operation': 'multi_publish.' + operation, 'params': params}), trusted_origin=True)
    assert response['ok'], response
    return response['data']


@pytest.fixture(params=['/micro/content/cgi-bin/mmfinderassistant-bin/post/post_create',
                        '/cgi-bin/mmfinderassistant-bin/post/post_create'])
def channels(db_engine, app_paths, tmp_path, monkeypatch, request):
    create_path = request.param
    account = AccountService(db_engine).create('channels', 'finder-isolated', '合成视频号')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id)
        row.session_ref = 'isolated-version'; row.login_state = 'valid'
    path = tmp_path / 'owned.mp4'; path.write_bytes(b'synthetic owned channels video')
    video = FinishedVideoService(db_engine).import_external(path, duration_ms=3000, title='合成视频标题')
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths, step_timeout=1)
    events = []
    identity = {'origin': 'https://' + form.HOST, 'logged_in': True, 'expired': False,
                'uid': 'finder-isolated', 'profile_uid': 'finder-isolated', 'nickname': '合成视频号'}
    media = {'url': 'https://example.invalid/isolated.mp4', 'md5sum': 'isolated-material',
        'fileSize': 15360, 'width': 1920, 'height': 1080, 'videoPlayLen': 3,
        'thumbUrl': 'https://example.invalid/isolated-cover', 'mediaType': 4}
    ack = {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '9007199254740993'}}

    class Page:
        url = form.MANAGE_URL
        def goto(self, url, **kwargs): self.url = url; events.append('navigate')
        def evaluate(self, script): return identity
        def wait_for_timeout(self, ms): pass
        def is_closed(self): return False

    def open_session(s, **kwargs):
        if s.page is not None: return
        s.page = Page(); s.original_ref = 'isolated-version'
        s.context = SimpleNamespace(close=lambda: events.append('close'))
        adapter._init_observations(s); s.list_seen = True

    def upload(s, surface): s.uploaded = True; events.append('upload'); return True
    def prepare(s, surface, fields): s.owned_media = media; s.form_filled = True; return True
    def final(s, surface):
        events.append('final')
        fields = form.validate_form(s.payload)
        request = {'objectType': 0, 'isFullPost': 1, 'handleFlag': 2, 'mode': 1,
            'topics': fields['tags'], 'effectiveTime': fields['effective_time'],
            'objectDesc': {'description': fields['caption'], 'mediaType': 4,
                'media': [deepcopy(media)], 'shortTitle': [], 'topic': {'finderTopicInfo': '<finder>' + ''.join(
                    '<value0><topic><![CDATA[#' + t + '#]]></topic></value0>' for t in fields['tags']) + '</finder>'}}}
        response = SimpleNamespace(url='https://' + form.HOST + create_path, status=200,
            request=SimpleNamespace(method='POST', post_data_json=request), json=lambda: deepcopy(ack))
        adapter._response(s, response)

    monkeypatch.setattr(adapter, '_open', open_session)
    # Fake browser lifecycle only; the actual navigation helper has a real
    # offline Chromium menu/shadow-DOM regression of its own.
    def navigate(s, deadline):
        s.page.goto(form.UPLOAD_URL)
        return True
    monkeypatch.setattr(adapter, '_navigate_upload_form', navigate)
    monkeypatch.setattr(adapter, '_challenge', lambda s: None)
    monkeypatch.setattr(adapter, '_upload', upload)
    monkeypatch.setattr(adapter, '_prepare_form', prepare)
    monkeypatch.setattr(adapter, '_click_final', final)
    monkeypatch.setattr(adapter, '_refresh_after_receipt', lambda s: None)
    monkeypatch.setattr(adapter, '_persist_sessions', lambda: None)
    api = DesktopApi(db_engine, app_paths, publish_adapters={'channels': adapter})
    yield api, account, video, adapter, events, ack
    assert api.close(3)


def freeze(channels, db_engine, mode):
    api, account, video, *_ = channels
    params = {'video_ids': [video.id], 'targets': [{'account_id': account.id, 'platform': 'channels'}],
        'common_fields': {'title': '合成视频标题', 'body': '合成说明', 'tags': '#功能测试',
                          'channels_short_title': ''}, 'execution_mode': mode}
    if mode == 'platform_schedule':
        params['scheduled_times'] = {video.id: (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0, microsecond=0).isoformat()}
    prepared = command(api, 'prepare', params, 'prepare-channels')
    assert prepared['summary']['ready'] == 1
    batch = command(api, 'confirm', {'prepared_id': prepared['prepared_id'],
        'intent_id': 'prepare-channels', 'consent_partial': False}, 'save-channels')
    child = command(api, 'detail', {'id': batch['id']}, 'detail-channels')['deliveries'][0]
    with session_scope(db_engine) as db:
        update_authorization(db, batch['id'], add=[child['id']])
    return batch['id'], child['id']


def journal(db_engine, delivery_id):
    with session_scope(db_engine) as db:
        row = db.get(PublishDeliveryRow, delivery_id)
        attempt = db.scalar(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id == delivery_id, DeliveryAttemptRow.number == row.attempt))
        return row.request_digest, json.loads(attempt.final_json), json.loads(attempt.result_json)


def test_wrong_page_uid_remains_parked_until_explicit_exact_account_completion(
        channels, db_engine, monkeypatch):
    api, _, _, adapter, events, _ = channels
    uid = ['different-finder']; pages = []; queued = []
    original_open = adapter._open
    def open_session(s, **kwargs):
        original_open(s, **kwargs)
        if s.page not in pages: pages.append(s.page)
        # Same display name, different stable UID: never authorize by nickname.
        s.page.evaluate = lambda script: {'origin': 'https://' + form.HOST,
            'logged_in': True, 'expired': False, 'uid': uid[0],
            'profile_uid': uid[0], 'nickname': '合成视频号'}
    monkeypatch.setattr(adapter, '_open', open_session)
    monkeypatch.setattr(api._multi_publish, 'enqueue', lambda *args: queued.append(args))
    batch, did = freeze(channels, db_engine, 'platform_schedule')
    coordinator = api._multi_publish.coordinator
    assert coordinator.run_once(batch, [did]) == 'paused_auth'
    original_page = pages[0]
    session = next(s for s in adapter.sessions.values() if s.binding['delivery_id'] == did)
    original_context = session.context
    assert session.page is original_page and not session.final
    assert coordinator.run_once(batch, [did]) is None
    assert not any(event in {'upload', 'final', 'close'} for event in events)
    request = adapter.interventions()[0]
    command(api, 'intervention.respond', {'id': request['id'], 'delivery_id': did,
        'action': 'done', 'consent_same_identity': True}, 'wrong-account-done')
    assert queued == [('resume', did)]
    assert coordinator.resume_verification(did) == 'paused_auth'
    assert session.page is original_page and session.context is original_context
    assert not any(event in {'upload', 'final', 'close'} for event in events)
    with session_scope(db_engine) as db:
        row = db.get(PublishDeliveryRow, did)
        attempt = db.scalar(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id == did, DeliveryAttemptRow.number == row.attempt))
        assert attempt.final_json is None
    uid[0] = 'finder-isolated'
    request = adapter.interventions()[0]
    command(api, 'intervention.respond', {'id': request['id'], 'delivery_id': did,
        'action': 'done', 'consent_same_identity': True}, 'correct-account-done')
    assert coordinator.resume_verification(did) == 'platform_scheduled'
    assert pages == [original_page]
    assert events.count('upload') == events.count('final') == 1
    assert journal(db_engine, did)[1]['request_digest'] == journal(db_engine, did)[0]


def test_final_account_recheck_parks_existing_upload_and_never_journals_wrong_uid(
        channels, db_engine, monkeypatch):
    api, _, _, adapter, events, _ = channels
    uid = ['finder-isolated']; pages = []; changed = [False]
    original_open, original_prepare = adapter._open, adapter._prepare_form
    def open_session(s, **kwargs):
        original_open(s, **kwargs)
        if s.page not in pages: pages.append(s.page)
        s.page.evaluate = lambda script: {'origin': 'https://' + form.HOST,
            'logged_in': True, 'expired': False, 'uid': uid[0],
            'profile_uid': uid[0], 'nickname': '合成视频号'}
    def prepare(s, surface, fields):
        result = original_prepare(s, surface, fields)
        if not changed[0]: uid[0] = 'different-finder'; changed[0] = True
        return result
    monkeypatch.setattr(adapter, '_open', open_session)
    monkeypatch.setattr(adapter, '_prepare_form', prepare)
    monkeypatch.setattr(api._multi_publish, 'enqueue', lambda *args: None)
    batch, did = freeze(channels, db_engine, 'platform_schedule')
    coordinator = api._multi_publish.coordinator
    assert coordinator.run_once(batch, [did]) == 'paused_auth'
    session = next(s for s in adapter.sessions.values() if s.binding['delivery_id'] == did)
    assert session.uploaded and session.page is pages[0] and not session.final
    assert events.count('upload') == 1 and not any(event in {'final', 'close'} for event in events)
    with session_scope(db_engine) as db:
        row = db.get(PublishDeliveryRow, did)
        attempt = db.scalar(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id == did, DeliveryAttemptRow.number == row.attempt))
        assert attempt.final_json is None
    uid[0] = 'finder-isolated'
    request = adapter.interventions()[0]
    command(api, 'intervention.respond', {'id': request['id'], 'delivery_id': did,
        'action': 'done', 'consent_same_identity': True}, 'final-check-correct-account')
    assert coordinator.resume_verification(did) == 'platform_scheduled'
    assert len(pages) == 1 and events.count('upload') == events.count('final') == 1


@pytest.mark.parametrize('mode,work_id,expected', [
    ('publish_now', '9007199254740993', 'platform_accepted'),
    ('publish_now', None, 'result_unknown'),
    ('platform_schedule', '9007199254740993', 'platform_scheduled'),
    ('platform_schedule', None, 'platform_scheduled'),
])
def test_real_channels_ack_crosses_only_one_coordinator_final_and_survives_query(
        channels, db_engine, mode, work_id, expected):
    api, _, _, adapter, events, ack = channels
    if work_id is None: ack['data'].pop('objectId')
    batch, did = freeze(channels, db_engine, mode)
    actual=api._multi_publish.coordinator.run_once(batch, [did])
    assert actual == expected, (actual, events, api._multi_publish.detail(batch))
    key, final, result = journal(db_engine, did)
    assert final['request_digest'] == result['request_digest'] == key
    reference = {'work_id': work_id} if work_id else {}
    assert result['receipt_reference'] == reference
    assert result['state'] == expected
    assert result == outcome_evidence(AdapterOutcome(expected,
        scheduled_at=result['scheduled_at'], receipt_reference=reference,
        error_code='result_unknown' if expected == 'result_unknown' else None), key)
    assert events.count('upload') == events.count('final') == 1
    if expected == 'platform_scheduled':
        detail = command(api, 'detail', {'id': batch}, 'completed-schedule-detail')
        assert detail['state'] == 'completed' and detail['accepted_count'] == 1
        assert detail['deliveries'][0]['state'] == 'platform_scheduled'
        assert events.count('close') == 1
    assert api._multi_publish.coordinator.run_once(batch, [did]) is None
    # A query can retain the original durable ACK or remain unknown. It cannot
    # call upload/final, and cannot promote an immediate no-ID ACK to acceptance.
    recovered = api._multi_publish.coordinator.reconcile_delivery(did)
    assert recovered == expected
    assert events.count('upload') == events.count('final') == 1
    assert journal(db_engine, did)[1] == final


@pytest.mark.parametrize('reported', ['platform_accepted', 'processing', 'reviewing'])
def test_shared_boundary_refuses_formal_no_id_even_if_adapter_overclaims(channels, db_engine, monkeypatch, reported):
    api, _, _, adapter, events, _ = channels
    batch, did = freeze(channels, db_engine, 'publish_now')
    def malformed_submit(payload, *, context, before_final):
        before_final(); events.append('final')
        return AdapterOutcome(reported, receipt_reference={})
    monkeypatch.setattr(adapter, 'submit', malformed_submit)
    assert api._multi_publish.coordinator.run_once(batch, [did]) == 'result_unknown'
    key, final, result = journal(db_engine, did)
    assert final['request_digest'] == result['request_digest'] == key
    assert result['state'] == 'result_unknown' and not result['receipt_reference']
    assert api._multi_publish.coordinator.run_once(batch, [did]) is None
    assert events.count('final') == 1


def test_schedule_receipt_must_match_actual_frozen_time(channels, db_engine, monkeypatch):
    api, _, _, adapter, events, _ = channels
    batch, did = freeze(channels, db_engine, 'platform_schedule')
    def wrong_schedule(payload, *, context, before_final):
        before_final(); events.append('final')
        return AdapterOutcome('platform_scheduled', scheduled_at='2000-01-01T12:00:00+00:00')
    monkeypatch.setattr(adapter, 'submit', wrong_schedule)
    assert api._multi_publish.coordinator.run_once(batch, [did]) == 'result_unknown'
    assert journal(db_engine, did)[2]['state'] == 'result_unknown'
    assert api._multi_publish.coordinator.run_once(batch, [did]) is None
    assert events.count('final') == 1
