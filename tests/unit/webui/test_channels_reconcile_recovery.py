"""Post-final verification recovery; synthetic browser events, no platform IO."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace

import pytest

from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.deliveries import digest
from yijianjifa.storage.db import session_scope
from yijianjifa.webui import channels_multi_publish as module
from yijianjifa.webui import channels_publish_form as form
from yijianjifa.webui.chromium_publish_session import VERIFICATION_POINTER


@pytest.fixture
def recovery(db_engine, app_paths, tmp_path, monkeypatch):
    account = AccountService(db_engine).create('channels', 'finder-recovery', '合成账号')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id)
        row.session_ref = 'fixture-version'; row.login_state = 'valid'
    video = tmp_path / 'fixture.mp4'; video.write_bytes(b'isolated-video')
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0, microsecond=0)
    payload = {'platform': 'channels', 'platform_user_id': 'finder-recovery', 'account_id': account.id,
        'video_path': str(video), 'video_hash': sha256_file(video), 'video_id': 'fixture',
        'session_fingerprint': hashlib.sha256(b'fixture-version').hexdigest(),
        'fields': {'title': '验证测试', 'body': '原页恢复', 'tags': '#功能测试',
                   'cover': {'kind': 'first_frame', 'frame_ms': 0}},
        'execution_mode': 'platform_schedule', 'scheduled_at': when.isoformat()}
    media = {'url': 'https://fixture.invalid/video', 'md5sum': 'fixture-media', 'fileSize': 15360,
        'width': 1920, 'height': 1080, 'videoPlayLen': 3.0,
        'thumbUrl': 'https://fixture.invalid/cover', 'mediaType': 4}
    normal_body = {'objectType': 0, 'isFullPost': 1, 'handleFlag': 2, 'mode': 1,
        'topics': ['功能测试'], 'effectiveTime': int(when.timestamp()),
        'objectDesc': {'description': '验证测试\n原页恢复 #功能测试', 'mediaType': 4,
            'media': [deepcopy(media)], 'shortTitle': [],
            'topic': {'finderTopicInfo': '<finder><value0><topic><![CDATA[#功能测试#]]></topic></value0></finder>'}}}
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    events = []; challenge = [False]
    adapter = module.ChannelsMultiPublishAdapter(db_engine, app_paths, step_timeout=.6)
    adapter.bind_delivery(digest=digest(payload), delivery_id='fixture-target', task_id='fixture-task')

    class Page:
        url = form.MANAGE_URL
        def __init__(self):
            self.callbacks = {}; self.queue = []; self.closed = False
            self.mouse = SimpleNamespace(click=self.confirm)
        def on(self, name, callback): self.callbacks.setdefault(name, []).append(callback)
        def emit(self, name, value):
            for callback in self.callbacks.get(name, []): callback(value)
        def goto(self, url, **kwargs):
            if self.url == form.UPLOAD_URL and url != self.url:
                self.queue.clear()  # Navigation cancels a pending form request.
            self.url = url; events.append(('navigate', url))
            if url == form.MANAGE_URL:
                self.emit('response', SimpleNamespace(url='https://' + form.HOST + form.LIST_PATH,
                    status=200, request=SimpleNamespace(method='POST'),
                    json=lambda: {'errCode': 0, 'data': {'list': []}}))
        def wait_for_timeout(self, ms):
            clock.value += ms / 1000
            if self.queue: self.queue.pop(0)()
        def is_closed(self): return self.closed
        def screenshot(self, **kwargs): return b'isolated-frame'
        def evaluate(self, script, args=None):
            if script == VERIFICATION_POINTER:
                # This fixture represents the already-open original confirmation
                # dialog. Real DOM hit testing and rejection are tested separately.
                assert args == {'points': [[20, 20]], 'kind': 'complex',
                                'allow_original_confirmation': True, 'allow_original_declaration': False}
                return True
            return {'origin': 'https://' + form.HOST, 'logged_in': True, 'expired': False,
                'uid': 'finder-recovery', 'profile_uid': 'finder-recovery', 'nickname': '合成账号'}
        def confirm(self, *args):
            challenge[0] = False; events.append(('verification', None))
            self.queue.extend(self.after_confirmation)
        after_confirmation = []

    page = Page()
    context = SimpleNamespace(pages=[page], set_default_timeout=lambda ms: None,
                              route=lambda pattern, handler: None,
                              close=lambda: setattr(page, 'closed', True))
    adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a, **k: context),
                                    stop=lambda: None)
    monkeypatch.setattr(module.creator_auth, 'restore_session', lambda *a, **k: None)
    monkeypatch.setattr(adapter, '_challenge', lambda s: 'complex' if challenge[0] else None)
    monkeypatch.setattr(adapter, '_refresh_after_receipt', lambda s: None)
    monkeypatch.setattr(adapter, '_persist_sessions', lambda: None)
    # This fixture starts recovery from a synthetic, already-submitted page;
    # real baseline menu navigation is covered in the offline Chromium tests.
    def navigate(s, deadline):
        s.page.goto(form.UPLOAD_URL)
        return True
    monkeypatch.setattr(adapter, '_navigate_upload_form', navigate)
    def upload(s, surface): s.uploaded = True; events.append(('upload', None)); return True
    def prepare(s, surface, expected): s.owned_media = deepcopy(media); return True
    def click(s, surface): events.append(('final', None)); challenge[0] = True
    monkeypatch.setattr(adapter, '_upload', upload)
    monkeypatch.setattr(adapter, '_prepare_form', prepare)
    monkeypatch.setattr(adapter, '_click_final', click)

    def request(*, body=None, host=form.HOST):
        return SimpleNamespace(url='https://' + host + form.CREATE_PATH, method='POST',
                               post_data_json=deepcopy(normal_body if body is None else body))
    def respond(req, *, status=200, body=None):
        ack = {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '9007199254740993'}}
        page.emit('response', SimpleNamespace(url=req.url, request=req, status=status,
            json=lambda: ack if body is None else body))

    assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
    first = adapter.submit(payload, context=None, before_final=lambda: events.append(('journal', None)))
    assert first.state == 'result_unknown' and first.error_code == 'verification_required'
    token = adapter.interventions()[0]['id']
    events.clear()
    yield SimpleNamespace(adapter=adapter, payload=payload, page=page, context=first.context,
        token=token, events=events, request=request, respond=respond, challenge=challenge, clock=clock)
    adapter.close()


def continue_verification(case):
    case.adapter.respond(case.token, 'pointer', [20, 20])
    return case.adapter.reconcile(case.payload, context=case.context)


def test_verification_waits_for_delayed_bound_ack_on_original_page(recovery):
    c = recovery; req = c.request()
    c.page.after_confirmation = [lambda: c.page.emit('request', req), lambda: c.respond(req)]
    result = continue_verification(c)
    assert result.state == 'platform_scheduled'
    assert result.receipt_reference == {'work_id': '9007199254740993'}
    assert not any(name == 'navigate' for name, _ in c.events)
    assert c.adapter._session(c.payload).final is True


def test_pending_submit_timeout_retains_original_page_without_reupload_or_final(recovery):
    c = recovery; req = c.request()
    c.page.after_confirmation = [lambda: c.page.emit('request', req)]
    result = continue_verification(c)
    assert result.state == 'result_unknown' and result.context == c.context
    assert c.adapter._session(c.payload).page is c.page and not c.page.closed
    assert c.page.url == form.UPLOAD_URL
    assert not any(name in {'navigate', 'upload', 'final', 'journal'} for name, _ in c.events)
    with pytest.raises(ValueError, match='final already attempted'):
        c.adapter.submit(c.payload, context=c.context, before_final=lambda: pytest.fail('must not journal'))


def test_completed_unreadable_submit_queries_only_after_request_finished(recovery):
    c = recovery; req = c.request()
    def finished():
        c.events.append(('request_finished', None)); c.page.emit('requestfinished', req)
    c.page.after_confirmation = [lambda: c.page.emit('request', req),
        lambda: c.respond(req, body={'changed': True}), finished]
    result = continue_verification(c)
    assert result.state == 'result_unknown'
    assert [name for name, _ in c.events] == ['verification', 'request_finished', 'navigate']
    assert c.page.url == form.MANAGE_URL
    assert c.adapter._session(c.payload).final is True


def test_pre_verification_rejection_does_not_end_a_new_verified_request(recovery):
    c = recovery; previous = c.request(); current = c.request()
    c.page.emit('request', previous)
    c.respond(previous, body={'errCode': 1, 'data': {'baseResp': {'errcode': 101}}})
    c.page.emit('requestfinished', previous)
    c.page.after_confirmation = [lambda: c.page.emit('request', current), lambda: c.respond(current)]
    result = continue_verification(c)
    assert result.state == 'platform_scheduled'
    assert result.receipt_reference == {'work_id': '9007199254740993'}
    assert not any(name == 'navigate' for name, _ in c.events)


def test_previous_request_completion_cannot_cancel_a_queued_verification_submit(recovery):
    c = recovery; previous = c.request(); current = c.request()
    c.page.emit('request', previous)
    c.page.after_confirmation = [lambda: c.page.emit('requestfinished', previous),
        lambda: c.page.emit('request', current), lambda: c.respond(current)]
    result = continue_verification(c)
    assert result.state == 'platform_scheduled'
    assert not any(name == 'navigate' for name, _ in c.events)


def test_late_old_rejection_is_not_a_current_verified_submission_rejection(recovery):
    c = recovery; previous = c.request(); current = c.request()
    c.page.emit('request', previous)
    c.page.after_confirmation = [
        lambda: c.respond(previous, body={'errCode': 1, 'data': {'baseResp': {'errcode': 101}}}),
        lambda: c.page.emit('requestfinished', previous), lambda: c.page.emit('request', current),
        lambda: c.respond(current)]
    result = continue_verification(c)
    assert result.state == 'platform_scheduled'
    assert not any(name == 'navigate' for name, _ in c.events)


@pytest.mark.parametrize('mismatch', ['host', 'caption', 'media', 'time'])
def test_unrelated_request_completion_cannot_release_the_original_page(recovery, mismatch):
    c = recovery; req = c.request()
    if mismatch == 'host': req.url = 'https://foreign.invalid' + form.CREATE_PATH
    if mismatch == 'caption': req.post_data_json['objectDesc']['description'] = '别人的内容'
    if mismatch == 'media': req.post_data_json['objectDesc']['media'][0]['md5sum'] = 'other-media'
    if mismatch == 'time': req.post_data_json['effectiveTime'] += 60
    c.page.after_confirmation = [lambda: c.page.emit('request', req),
        lambda: c.respond(req), lambda: c.page.emit('requestfinished', req)]
    result = continue_verification(c)
    assert result.state == 'result_unknown' and not result.receipt_reference
    assert c.adapter._session(c.payload).page is c.page
    assert not any(name in {'navigate', 'upload', 'final'} for name, _ in c.events)


def test_failed_transport_only_allows_query_and_never_proves_rejection(recovery):
    c = recovery; req = c.request()
    def failed():
        c.events.append(('request_failed', None)); c.page.emit('requestfailed', req)
    c.page.after_confirmation = [lambda: c.page.emit('request', req), failed]
    result = continue_verification(c)
    assert result.state == 'result_unknown' and result.context == c.context
    assert [name for name, _ in c.events] == ['verification', 'request_failed', 'navigate']
    assert c.adapter._session(c.payload).final is True


def test_cancellation_during_wait_keeps_unknown_and_does_not_navigate(recovery):
    c = recovery; req = c.request()
    c.page.after_confirmation = [lambda: c.page.emit('request', req), c.adapter.cancel.set]
    result = continue_verification(c)
    assert result.state == 'result_unknown' and result.context == c.context
    assert c.adapter._session(c.payload).page is c.page and not c.page.closed
    assert not any(name in {'navigate', 'upload', 'final'} for name, _ in c.events)


def test_user_cancels_verification_without_changing_final_or_navigating(recovery):
    c = recovery
    c.adapter.respond(c.token, 'cancel')
    with pytest.raises(ValueError, match='verification cancelled'):
        c.adapter.reconcile(c.payload, context=c.context)
    assert c.adapter._session(c.payload).final is True
    assert c.adapter._session(c.payload).page is c.page
    assert not c.events


def test_unchanged_challenge_remains_parked_on_the_original_page(recovery):
    c = recovery
    c.adapter.respond(c.token, 'done')
    result = c.adapter.reconcile(c.payload, context=c.context)
    assert result.state == 'result_unknown' and result.error_code == 'verification_required'
    assert c.adapter._session(c.payload).page is c.page and c.adapter.interventions()
    assert not c.events


def test_done_after_manual_verification_waits_for_normal_delayed_request(recovery):
    c = recovery; req = c.request()
    c.challenge[0] = False
    c.page.queue = [lambda: c.page.emit('request', req), lambda: c.respond(req)]
    c.adapter.respond(c.token, 'done')
    result = c.adapter.reconcile(c.payload, context=c.context)
    assert result.state == 'platform_scheduled'
    assert not any(name == 'navigate' for name, _ in c.events)


def test_no_request_end_evidence_returns_bounded_unknown_on_original_page(recovery):
    c = recovery; started = c.clock.value
    result = continue_verification(c)
    assert result.state == 'result_unknown' and result.context == c.context
    assert 0 < c.clock.value - started <= 1
    assert c.adapter._session(c.payload).page is c.page
    assert not any(name in {'navigate', 'upload', 'final'} for name, _ in c.events)


def test_invalid_verification_response_does_not_clear_a_recorded_rejection(recovery):
    c = recovery; req = c.request()
    c.respond(req, body={'errCode': 1, 'data': {'baseResp': {'errcode': 101}}})
    with pytest.raises(ValueError): c.adapter.respond(c.token, 'pointer', [-1, 20])
    c.challenge[0] = False
    result = c.adapter.reconcile(c.payload, context=c.context)
    assert result.state == 'platform_rejected'
    assert not any(name in {'navigate', 'upload', 'final'} for name, _ in c.events)


def test_late_ack_after_wait_timeout_is_reconciled_without_a_new_submission(recovery):
    c = recovery; req = c.request()
    c.page.after_confirmation = [lambda: c.page.emit('request', req)]
    first = continue_verification(c)
    assert first.state == 'result_unknown' and c.adapter._session(c.payload).page is c.page
    c.page.queue = [lambda: c.respond(req)]
    result = c.adapter.reconcile(c.payload, context=first.context)
    assert result.state == 'platform_scheduled'
    assert result.receipt_reference == {'work_id': '9007199254740993'}
    assert not any(name in {'navigate', 'upload', 'final'} for name, _ in c.events)


def test_one_finished_request_does_not_navigate_away_from_another_pending_request(recovery):
    c = recovery; first = c.request(); second = c.request()
    c.page.after_confirmation = [lambda: c.page.emit('request', first),
        lambda: c.page.emit('request', second), lambda: c.page.emit('requestfinished', first),
        lambda: c.respond(second)]
    result = continue_verification(c)
    assert result.state == 'platform_scheduled'
    assert not any(name == 'navigate' for name, _ in c.events)


def test_verified_request_explicit_rejection_stops_without_query_or_final(recovery):
    c = recovery; req = c.request()
    c.page.after_confirmation = [lambda: c.page.emit('request', req),
        lambda: c.respond(req, body={'errCode': 2, 'data': {'baseResp': {'errcode': 3}}}),
        lambda: c.page.emit('requestfinished', req)]
    result = continue_verification(c)
    assert result.state == 'platform_rejected'
    assert not any(name in {'navigate', 'upload', 'final'} for name, _ in c.events)


def test_rejection_cannot_close_original_page_while_another_bound_request_awaits_ack(recovery):
    c = recovery; first = c.request(); second = c.request()
    c.page.after_confirmation = [lambda: c.page.emit('request', first),
        lambda: c.page.emit('request', second),
        lambda: c.respond(first, body={'errCode': 2, 'data': {'baseResp': {'errcode': 3}}}),
        lambda: c.page.emit('requestfinished', first), lambda: c.respond(second)]
    result = continue_verification(c)
    assert result.state == 'platform_scheduled'
    assert result.receipt_reference == {'work_id': '9007199254740993'}
    assert not c.page.queue
    assert not any(name in {'navigate', 'upload', 'final'} for name, _ in c.events)


def test_rejection_with_a_bound_request_still_pending_times_out_unknown_without_close(recovery):
    c = recovery; req = c.request()
    c.page.after_confirmation = [lambda: c.page.emit('request', req),
        lambda: c.respond(req, body={'errCode': 2, 'data': {'baseResp': {'errcode': 3}}})]
    result = continue_verification(c)
    assert result.state == 'result_unknown' and result.context == c.context
    assert c.adapter._session(c.payload).page is c.page and not c.page.closed
    assert c.adapter._session(c.payload).final is True
    assert not any(name in {'navigate', 'upload', 'final'} for name, _ in c.events)
