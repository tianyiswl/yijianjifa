"""Video-channel final-boundary lifecycle, entirely synthetic and isolated."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
import pytest
from test_channels_dom_visibility import offline_browser, offline_page

from yijianjifa.features.accounts.service import AccountService, AccountRow, AccountIdentityMismatch
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome
from yijianjifa.features.publish.deliveries import digest, canonical, PublishDeliveryRow, DeliveryAttemptRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui import channels_multi_publish as module
from yijianjifa.webui import channels_publish_form as form


def test_session_capture_uses_shared_no_navigation_snapshot(db_engine, app_paths, monkeypatch):
    class Context:
        def storage_state(self):raise AssertionError('snapshot must not open temporary pages')
    context=Context();calls=[]
    state={'cookies':[{'name':'synthetic','value':'fixture','domain':form.HOST,'path':'/'}],
           'origins':[]}
    def snapshot(spec, source):
        assert spec is module.auth.SPEC and source is context
        calls.append(source);return state
    monkeypatch.setattr(module.creator_auth,'capture_session_state',snapshot,raising=False)
    adapter=module.ChannelsMultiPublishAdapter(db_engine,app_paths)
    blob=adapter._capture_session_blob(context,'finder-test')
    assert calls==[context]
    assert module.creator_auth.decode_session(module.auth.SPEC,blob,'finder-test')==state


@pytest.mark.parametrize('raw',[
    {'state_ready':False,'form_count':1,'media_count':-1,'caption':''},
    {'state_ready':True,'form_count':0,'media_count':0,'caption':''},
    {'state_ready':True,'form_count':2,'media_count':0,'caption':''},
])
def test_upload_waits_for_one_complete_form_without_selecting_file(db_engine,app_paths,raw):
    adapter=module.ChannelsMultiPublishAdapter(db_engine,app_paths)
    class Page:
        def evaluate(self,script):return raw
        def locator(self,*args):raise AssertionError('incomplete form must not accept files')
    s=SimpleNamespace(uploaded=False,payload={})
    assert adapter._upload(s,Page()) is False
    assert s.uploaded is False
    adapter.close()


def test_prepare_waits_for_store_readiness_without_editing_an_uploaded_form(db_engine,app_paths):
    adapter=module.ChannelsMultiPublishAdapter(db_engine,app_paths)
    class Page:
        def evaluate(self,script):return {'state_ready':False,'upload_complete':True,'editor_count':1,'form_count':1}
        def locator(self,*args):raise AssertionError('unready state cannot edit the form')
    s=SimpleNamespace(form_filled=False)
    assert adapter._prepare_form(s,Page(),{}) is False
    assert s.form_filled is False
    adapter.close()


def normal_request(f, media):
    return {'objectType': 0, 'isFullPost': 1, 'handleFlag': 2, 'mode': 1, 'topics': f['tags'],
        'effectiveTime': f['effective_time'], 'objectDesc': {'description': f['caption'], 'mediaType': 4,
        'media': [deepcopy(media)], 'shortTitle': [], 'topic': {'finderTopicInfo': '<finder>' + ''.join(
            '<value0><topic><![CDATA[#' + t + '#]]></topic></value0>' for t in f['tags']) + '</finder>'}}}


def response(path, request, data, *, status=200, host=form.HOST):
    return SimpleNamespace(url='https://' + host + path, status=status,
        request=SimpleNamespace(method='POST', post_data_json=request), json=lambda: data)


@pytest.fixture
def case(db_engine, app_paths, tmp_path, monkeypatch):
    account = AccountService(db_engine).create('channels', 'finder-test', '合成视频号')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id); row.session_ref = 'version-1'; row.login_state = 'valid'
    video = tmp_path / 'test.mp4'; video.write_bytes(b'owned-fixture-video')
    payload = {'platform': 'channels', 'platform_user_id': 'finder-test', 'account_id': account.id,
        'video_path': str(video), 'video_hash': sha256_file(video), 'video_id': 'fixture',
        'session_fingerprint': hashlib.sha256(b'version-1').hexdigest(),
        'fields': {'title': '测试视频标题', 'body': '测试说明', 'tags': '#功能测试', 'channels_short_title': '',
                   'cover': {'kind': 'first_frame', 'frame_ms': 0}},
        'execution_mode': 'platform_schedule', 'scheduled_at': (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0, microsecond=0).isoformat()}
    media = {'url': 'https://example.invalid/owned.mp4', 'md5sum': 'owned-uuid', 'fileSize': 15360,
             'width': 1920, 'height': 1080, 'videoPlayLen': 3.0, 'thumbUrl': 'https://example.invalid/cover', 'mediaType': 4}
    identity = {'origin': 'https://' + form.HOST, 'logged_in': True, 'expired': False,
                'uid': 'finder-test', 'profile_uid': 'finder-test', 'nickname': '合成视频号'}
    events = []
    # Include SQLite execution-log writes in the synthetic lifecycle budget on Windows.
    adapter = module.ChannelsMultiPublishAdapter(db_engine, app_paths, step_timeout=1)
    adapter.bind_delivery(digest=digest(payload), delivery_id='target', task_id='batch')
    class Page:
        url = form.MANAGE_URL
        def goto(self, url, **kwargs): self.url = url; events.append(('navigate', url))
        def wait_for_timeout(self, ms): pass
        def is_closed(self): return False
        def screenshot(self, **kwargs): return b'fixture-frame'
        def evaluate(self, script): return identity
    page = Page()
    def open_session(s, **kwargs):
        if s.page is not None: return
        s.page = page; s.original_ref = 'version-1'
        s.context = SimpleNamespace(close=lambda: events.append(('close', None)))
        adapter._init_observations(s); s.list_seen = True
    monkeypatch.setattr(adapter, '_open', open_session)
    # This lifecycle fixture replaces the entire browser; actual menu/DOM
    # navigation is exercised by test_channels_publish_navigation.
    def navigate(s, deadline):
        s.page.goto(form.UPLOAD_URL)
        return True
    monkeypatch.setattr(adapter, '_navigate_upload_form', navigate)
    monkeypatch.setattr(adapter, '_challenge', lambda s: None)
    monkeypatch.setattr(adapter, '_capture_session_blob', lambda *args: b'fixture-archive')
    monkeypatch.setattr(adapter, '_persist_sessions', lambda: None)
    def upload(s, surface): s.uploaded = True; events.append(('upload', None)); return True
    def prepare(s, surface, f): s.owned_media = media; s.form_filled = True; return True
    def click(s, surface):
        events.append(('final', None))
        adapter._response(s, response(form.CREATE_PATH, normal_request(form.validate_form(payload), media),
            {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '9007199254740993'}}))
    monkeypatch.setattr(adapter, '_upload', upload)
    monkeypatch.setattr(adapter, '_prepare_form', prepare)
    monkeypatch.setattr(adapter, '_click_final', click)
    return adapter, payload, page, events, identity, media


def test_journal_before_one_final_and_ack_is_not_publication(case):
    a, p, _, events, _, _ = case
    assert a.validate_target(p, p, p['execution_mode']).authorized
    result = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'platform_scheduled' and result.scheduled_at == p['scheduled_at']
    assert result.receipt_reference == {'work_id': '9007199254740993'}
    assert [k for k, _ in events if k in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
    with pytest.raises(ValueError): a.submit(p, context=None, before_final=lambda: None)
    a.close()


def test_native_json_number_encoding_keeps_receipt_and_closes_page(case, offline_page, monkeypatch):
    a, p, _, events, _, media = case
    request = normal_request(form.validate_form(p), media)
    # Actual browser transport serializes a Number 3.0 as JSON integer 3.
    import json
    wire = json.loads(offline_page.evaluate('value => JSON.stringify(value)', request))
    assert type(media['videoPlayLen']) is float and type(wire['objectDesc']['media'][0]['videoPlayLen']) is int
    def final(s, surface):
        events.append(('final', None))
        a._response(s, response(form.CREATE_PATH, wire,
            {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '42'}}))
    monkeypatch.setattr(a, '_click_final', final)
    outcome = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert outcome.state == 'platform_scheduled' and outcome.scheduled_at == p['scheduled_at']
    assert outcome.receipt_reference == {'work_id': '42'}
    assert a._session(p).context is None and a._session(p).page is None
    assert [k for k, _ in events if k in {'upload', 'journal', 'final', 'close'}] == ['upload', 'journal', 'final', 'close']
    a.close()


@pytest.mark.parametrize('prefix', ['/micro/content/cgi-bin/mmfinderassistant-bin', '/cgi-bin/mmfinderassistant-bin'])
def test_content_service_receipt_completes_original_schedule_and_closes(case, monkeypatch, prefix):
    a, p, _, events, _, media = case
    traces = []
    monkeypatch.setattr(a, '_trace', lambda s, step, checks=None: traces.append((step, checks)))
    def final(s, surface):
        events.append(('final', None))
        reply = response(prefix + '/post/post_create', normal_request(form.validate_form(p), media),
            {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '42'}})
        reply.request.url = reply.url
        a._create_request_started(s, reply.request)
        a._response(s, reply)
        a._create_request_ended(s, reply.request)
    monkeypatch.setattr(a, '_click_final', final)
    outcome = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert outcome.state == 'platform_scheduled' and outcome.scheduled_at == p['scheduled_at']
    assert outcome.receipt_reference == {'work_id': '42'}
    assert a._session(p).page is None and a._session(p).context is None
    assert [k for k, _ in events if k in {'upload', 'journal', 'final', 'close'}] == ['upload', 'journal', 'final', 'close']
    assert any(step == 'channels.submit_request' and checks['request_matches'] for step, checks in traces)
    assert any(step == 'channels.submit_response' and checks['response_success'] for step, checks in traces)
    a.close()


def test_content_service_list_readback_identifies_original_scheduled_work(case):
    a, p, _, _, _, _ = case
    s = a._session(p); a._init_observations(s)
    f = form.validate_form(p)
    a._response(s, response('/micro/content/cgi-bin/mmfinderassistant-bin/post/post_list', {},
        {'errCode': 0, 'data': {'list': [{'objectId': '42', 'effectiveTime': f['effective_time'],
            'handleStatus': 2, 'desc': {'description': f['caption']}}]}}))
    assert s.list_seen
    result = form.verified_row(s.list_rows, {'work_id': '42'}, f)
    assert result == {'state': 'platform_scheduled', 'reference': {'work_id': '42'}}
    a.close()


def test_create_response_diagnostics_identify_mismatched_media_without_payload(case, monkeypatch):
    a, p, _, _, _, media = case
    s = a._session(p); a._init_observations(s)
    s.final = True; s.owned_media = media
    request = normal_request(form.validate_form(p), media)
    request['objectDesc']['media'][0]['md5sum'] = 'private-foreign-uuid'
    traces = []
    monkeypatch.setattr(a, '_trace', lambda session, step, checks=None: traces.append((step, checks)))
    a._response(s, response(form.CREATE_PATH, request,
        {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '42'}}))
    checks = next(checks for step, checks in traces if step == 'channels.submit_response')
    assert checks['response_success'] is True and checks['request_matches'] is False
    assert checks['media_md5sum_matches'] is False and checks['caption_matches'] is True
    assert all(type(v) in (bool, int) for v in checks.values())
    assert 'private-foreign-uuid' not in str(traces) and not s.submit_ok
    a.close()


def test_wrong_account_parks_original_page_without_upload_or_close(case):
    a, p, page, events, identity, _ = case
    identity.update(uid='wrong-finder', profile_uid='wrong-finder')
    result = a.validate_target(p, p, p['execution_mode'])
    assert isinstance(result, AdapterOutcome)
    assert result.state == 'paused_auth' and result.error_code == 'verification_required'
    session = a._session(p)
    assert session.page is page and session.context is not None and not session.final
    assert session.identity_failure_stage == 'validation'
    request = a.interventions()[0]
    assert request['kind'] == 'login' and '不一致' in request['message']
    assert request['reason'] == 'identity_mismatch'
    assert '账号管理' in request['message'] and '浏览器' in request['message']
    assert '取消此本地目标' in request['message'] and '等待任务停止' in request['message']
    assert not any(k in {'upload', 'final', 'close'} for k, _ in events)
    a.close()


def test_different_identity_after_navigation_parks_without_upload_or_close(case, monkeypatch):
    a, p, page, events, _, _ = case
    assert a.validate_target(p, p, p['execution_mode']).authorized
    original = a._identity
    def identity(s):
        if page.url == form.UPLOAD_URL: raise AccountIdentityMismatch('changed')
        return original(s)
    monkeypatch.setattr(a, '_identity', identity)
    result = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'paused_auth' and result.error_code == 'verification_required'
    assert a._session(p).page is page and a._session(p).identity_failure_stage == 'prepare'
    assert a.interventions()[0]['reason'] == 'identity_mismatch'
    assert not any(k in {'upload', 'journal', 'final', 'close'} for k, _ in events)
    a.close()


def test_wrong_uid_with_same_nickname_cannot_continue_after_done(case):
    a, p, page, events, identity, _ = case
    identity.update(uid='wrong-finder', profile_uid='wrong-finder')
    first = a.validate_target(p, p, p['execution_mode'])
    assert isinstance(first, AdapterOutcome)
    assert first.state == 'paused_auth'
    old_request = a.interventions()[0]['id']
    a.respond(old_request, 'done')
    result = a.resume(p, context=first.context, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'paused_auth' and result.context == first.context
    assert a._session(p).page is page and not a._session(p).final
    assert a.interventions()[0]['id'] != old_request
    assert not any(k in {'upload', 'journal', 'final', 'close'} for k, _ in events)
    a.close()


def test_exact_target_uid_and_explicit_done_resume_the_same_initial_page(case):
    a, p, page, events, identity, _ = case
    identity.update(uid='wrong-finder', profile_uid='wrong-finder')
    first = a.validate_target(p, p, p['execution_mode'])
    assert isinstance(first, AdapterOutcome)
    assert first.state == 'paused_auth'
    session = a._session(p); original_context = session.context
    identity.update(uid='finder-test', profile_uid='finder-test')
    # Merely switching the page back does not authorize a worker to proceed.
    with pytest.raises(ValueError, match='explicit same-identity resume required'):
        a.resume(p, context=first.context, before_final=lambda: pytest.fail('must not journal'))
    assert session.page is page and session.context is original_context
    assert not any(k in {'upload', 'journal', 'final', 'close'} for k, _ in events)
    a.respond(a.interventions()[0]['id'], 'done')
    result = a.resume(p, context=first.context, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'platform_scheduled'
    assert [k for k, _ in events if k in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
    assert sum(k == 'navigate' for k, _ in events) == 1
    a.close()


def test_post_final_identity_mismatch_stays_unknown_and_cannot_reupload(case, monkeypatch):
    a, p, page, events, identity, _ = case
    monkeypatch.setattr(a, '_click_final', lambda s, surface: events.append(('final', None)))
    assert a.validate_target(p, p, p['execution_mode']).authorized
    first = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert first.state == 'result_unknown'
    # Model a finished original request with no receipt; only then may the
    # existing recovery loop navigate to management to inspect its account.
    a._session(p).create_settled = True
    identity.update(uid='wrong-finder', profile_uid='wrong-finder')
    result = a.reconcile(p, context=first.context)
    assert result.state == 'result_unknown' and result.error_code == 'account_mismatch'
    assert a._session(p).page is page and a._session(p).final
    with pytest.raises(ValueError, match='final already attempted'):
        a.submit(p, context=first.context, before_final=lambda: pytest.fail('no second final'))
    assert [k for k, _ in events if k in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
    a.close()


def test_same_caption_on_other_work_does_not_claim_duplicate_or_success(case):
    a, p, _, events, _, _ = case
    a.validate_target(p, p, p['execution_mode'])
    session = a._session(p)
    session.list_rows = [{'objectId': 'foreign', 'desc': {'description': form.validate_form(p)['caption']}}]
    result = a.submit(p, context=None, before_final=lambda: None)
    assert result.receipt_reference == {'work_id': '9007199254740993'}
    assert sum(k == 'upload' for k, _ in events) == 1
    a.close()


def test_successful_bound_ack_wins_over_late_login_and_profile_failure(case, monkeypatch):
    a, p, _, _, _, _ = case
    assert a.validate_target(p, p, p['execution_mode']).authorized
    monkeypatch.setattr(a, '_challenge', lambda s: 'login' if s.submit_ok else None)
    monkeypatch.setattr(a, '_refresh_after_receipt', lambda s: None)
    result = a.submit(p, context=None, before_final=lambda: None)
    assert result.state == 'platform_scheduled'
    assert not a.interventions()
    a.close()


def test_missing_work_id_does_not_erase_complete_success_ack(case, monkeypatch):
    a, p, _, events, _, media = case
    def click(s, surface):
        events.append(('final', None))
        a._response(s, response(form.CREATE_PATH, normal_request(form.validate_form(p), media),
            {'errCode': 0, 'data': {'baseResp': {'errcode': 0}}}))
    monkeypatch.setattr(a, '_click_final', click)
    a.validate_target(p, p, p['execution_mode'])
    result = a.submit(p, context=None, before_final=lambda: None)
    assert result.state == 'platform_scheduled' and result.receipt_reference == {}
    with pytest.raises(ValueError): a.submit(p, context=None, before_final=lambda: None)
    a.close()


def test_immediate_ack_without_work_id_keeps_final_unknown_and_cannot_resubmit(case, monkeypatch):
    a, p, _, events, _, media = case
    p['execution_mode'] = 'publish_now'
    a.bind_delivery(digest=digest(p), delivery_id='target', task_id='batch')
    def click(s, surface):
        events.append(('final', None))
        a._response(s, response(form.CREATE_PATH, normal_request(form.validate_form(p), media),
            {'errCode': 0, 'data': {'baseResp': {'errcode': 0}}}))
    monkeypatch.setattr(a, '_click_final', click)
    a.validate_target(p, p, p['execution_mode'])
    result = a.submit(p, context=None, before_final=lambda: None)
    assert result.state == 'result_unknown' and a._session(p).submit_ok is True
    assert a._session(p).final is True and result.context == {'handle': a._session(p).handle}
    with pytest.raises(ValueError): a.submit(p, context=None, before_final=lambda: None)
    a.close()


def test_unknown_final_can_only_query_and_never_upload_again(case, monkeypatch):
    a, p, _, events, _, _ = case
    monkeypatch.setattr(a, '_click_final', lambda s, surface: events.append(('final', None)))
    a.validate_target(p, p, p['execution_mode'])
    first = a.submit(p, context=None, before_final=lambda: None)
    assert first.state == 'result_unknown'
    later = a.reconcile(p, context=first.context)
    assert later.state == 'result_unknown'
    assert sum(k == 'upload' for k, _ in events) == sum(k == 'final' for k, _ in events) == 1
    a.close()


def test_post_final_admin_verification_parks_same_page_and_does_not_repeat_click(case, monkeypatch):
    a, p, page, events, _, _ = case
    monkeypatch.setattr(a, '_click_final', lambda s, surface: events.append(('final', None)))
    monkeypatch.setattr(a, '_challenge', lambda s: 'complex' if s.final else None)
    a.validate_target(p, p, p['execution_mode'])
    result = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'result_unknown' and result.error_code == 'verification_required'
    assert a._session(p).page is page
    assert a.interventions()[0]['platform'] == 'channels' and a.interventions()[0]['final_started'] is True
    assert sum(k == 'final' for k, _ in events) == 1
    a.close()


def test_pre_final_verification_resume_uses_original_upload(case, monkeypatch):
    a, p, page, events, _, media = case
    a.validate_target(p, p, p['execution_mode'])
    pending = [False]
    def prepare(s, surface, f): s.owned_media = media; pending[0] = True; return False
    monkeypatch.setattr(a, '_prepare_form', prepare)
    monkeypatch.setattr(a, '_challenge', lambda s: 'complex' if pending[0] else None)
    first = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert first.state == 'paused_auth' and a._session(p).page is page
    assert sum(k == 'upload' for k, _ in events) == 1
    request = a.interventions()[0]
    a.respond(request['id'], 'done'); pending[0] = False
    monkeypatch.setattr(a, '_prepare_form', lambda s, surface, f: True)
    result = a.resume(p, context=first.context, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'platform_scheduled'
    assert sum(k == 'upload' for k, _ in events) == sum(k == 'final' for k, _ in events) == 1
    a.close()


@pytest.mark.parametrize('prefix', ['/micro/content/cgi-bin/mmfinderassistant-bin', '/cgi-bin/mmfinderassistant-bin'])
@pytest.mark.parametrize('change', ['before_final', 'wrong_host', 'wrong_path', 'wrong_method', 'wrong_asset', 'wrong_caption', 'wrong_time', 'wrong_topics', 'nested_false', 'http_error'])
def test_passive_observer_rejects_unbound_successes(case, change, prefix):
    a, p, _, _, _, media = case; session = a._session(p); a._init_observations(session)
    session.final = change != 'before_final'; session.owned_media = media
    f = form.validate_form(p); req = normal_request(f, media)
    body = {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '42'}}
    host = form.HOST; status = 200
    if change == 'wrong_host': host = 'foreign.invalid'
    if change == 'wrong_asset': req['objectDesc']['media'][0]['md5sum'] = 'foreign-uuid'
    if change == 'wrong_caption': req['objectDesc']['description'] = 'foreign'
    if change == 'wrong_time': req['effectiveTime'] += 60
    if change == 'wrong_topics': req['topics'] = ['foreign']
    if change == 'nested_false': body['data']['baseResp']['errcode'] = False
    if change == 'http_error': status = 500
    path = prefix + '/post/post_create' if change != 'wrong_path' else '/foreign' + prefix + '/post/post_create'
    reply = response(path, req, body, host=host, status=status)
    if change == 'wrong_method': reply.request.method = 'GET'
    a._response(session, reply)
    assert not session.submit_ok and session.receipt == {}
    a.close()


def test_cancel_before_final_never_clicks(case):
    a, p, _, events, _, _ = case
    a.cancel.set()
    assert a.validate_target(p, p, p['execution_mode']).authorized is False
    assert not any(k in {'upload', 'final'} for k, _ in events)
    a.close()


def test_durable_ack_without_id_survives_restart_without_new_final(case):
    a, p, _, events, _, _ = case
    snapshot = canonical(p); key = digest(p)
    with session_scope(a.engine) as db:
        db.add(PublishDeliveryRow(id='target', task_id='batch', item_id='item', history_id='history',
            platform='channels', account_id=p['account_id'], platform_user_id=p['platform_user_id'],
            video_id=p['video_id'], video_hash=p['video_hash'], snapshot_json=snapshot, request_digest=key,
            execution_mode='platform_schedule', scheduled_at=p['scheduled_at'], attempt=1, position=0))
        db.add(DeliveryAttemptRow(id='attempt', delivery_id='target', number=1, snapshot_json=snapshot, request_digest=key,
            final_json=canonical({'request_digest': key, 'state': 'result_unknown'}),
            result_json=canonical({'request_digest': key, 'state': 'platform_scheduled', 'scheduled_at': p['scheduled_at'],
                                  'receipt_reference': {}, 'error_code': None})))
    # No in-memory session: this rebinds only from the immutable stored attempt.
    a.bindings.clear()
    result = a.reconcile(p, context=None)
    assert result.state == 'platform_scheduled' and result.receipt_reference == {}
    assert not any(k in {'upload', 'final'} for k, _ in events)
    a.close()


def test_readiness_and_invalid_fields_do_no_browser_io(case, monkeypatch):
    a, p, _, events, _, _ = case
    monkeypatch.setattr(a, '_open', lambda *args, **kwargs: pytest.fail('unexpected browser IO'))
    assert a.plan_readiness(p) is None and a.validate_fields(p)
    p['fields']['location'] = {'name': '三千海'}
    assert a.plan_readiness(p) == 'platform_fields_invalid' and not a.validate_fields(p)
    assert not events
    a.close()


@pytest.mark.parametrize('message,code', [
    ('channels AI declaration option unavailable or changed', 'channels_declaration_options_unverified'),
    ('channels declaration option ambiguous', 'channels_declaration_options_unverified'),
    ('original declaration entrance unavailable', 'channels_original_unavailable'),
    ('channels original selected state mismatch', 'channels_original_unavailable'),
    ('channels original declaration requires at least five seconds', 'channels_original_video_too_short'),
])
def test_finite_declaration_reasons_only_apply_before_final(case, monkeypatch, message, code):
    adapter, payload, _, _, _, _ = case; s = adapter._session(payload)
    def fail(*args, **kwargs):raise ValueError(message)
    monkeypatch.setattr(adapter, '_advance_steps', fail)
    result = adapter._advance(s)
    assert result.state == 'pre_submit_failed' and result.error_code == code
    s.final = True
    with pytest.raises(ValueError, match=message):adapter._advance(s, query=True)
    adapter.close()


def test_declaration_reason_wrapper_never_swallows_navigation_observation(case, monkeypatch):
    adapter, payload, _, _, _, _ = case; s = adapter._session(payload)
    from playwright.sync_api import Error
    failure = Error('Execution context was destroyed, most likely because of a navigation')
    def fail(*args, **kwargs):raise failure
    monkeypatch.setattr(adapter, '_advance_steps', fail)
    with pytest.raises(Error) as result:adapter._advance(s)
    assert result.value is failure
    adapter.close()


@pytest.mark.parametrize('stage,error,category',[
    ('upload_preparation',ValueError('existing video-channel draft must be handled first'),'existing_draft'),
    ('schedule_preparation',ValueError('schedule did not commit'),'form_changed'),
    ('upload_preparation',ValueError('frozen video changed'),'resource_unavailable'),
])
def test_preparation_failure_keeps_specific_finite_diagnostic_only_before_final(case,monkeypatch,stage,error,category):
    adapter,payload,_,_,_,_=case;s=adapter._session(payload)
    def fail(*args,**kwargs):
        adapter._preparation_stage=stage
        raise error
    monkeypatch.setattr(adapter,'_advance_steps',fail)
    with pytest.raises(type(error)):adapter._advance(s)
    assert adapter.failure_diagnostic(error)=={'error_stage':stage,'error_category':category}
    assert adapter.failure_diagnostic(error)=={}
    s.final=True
    with pytest.raises(type(error)):adapter._advance(s,query=True)
    assert adapter.failure_diagnostic(error)=={}
    adapter.close()


def test_javascript_form_read_failure_never_records_private_exception_text(case,monkeypatch):
    from playwright.sync_api import Error
    adapter,payload,_,_,_,_=case;s=adapter._session(payload)
    error=Error('Page.evaluate: TypeError: private signed URL and cookie must not persist')
    def fail(*args,**kwargs):
        adapter._preparation_stage='form_preparation'
        raise error
    monkeypatch.setattr(adapter,'_advance_steps',fail)
    with pytest.raises(Error):adapter._advance(s)
    assert adapter.failure_diagnostic(error)=={
        'error_stage':'form_preparation','error_category':'javascript_read_failed'}
    adapter.close()


def test_expired_login_is_checked_before_upload_and_explained(case,monkeypatch):
    a,p,page,events,_,_=case
    monkeypatch.setattr(a,'_challenge',lambda s:'login')
    result=a.validate_target(p,p,p['execution_mode'])
    assert result.state=='paused_auth'
    assert a.service.get(p['account_id']).login_state=='expired'
    s=a._session(p);message=s.broker.status()['request']['message']
    assert '登录已失效' in message and not s.uploaded and not s.final
    assert not any(k in {'upload','final'} for k,_ in events)
    monkeypatch.setattr(a,'_challenge',lambda s:None)
    assert a.validate_target(p,p,p['execution_mode']).authorized
    assert a.service.get(p['account_id']).login_state=='valid'
    a.close()


def test_native_reminder_continues_original_attempt_without_broker_modal_or_second_upload(case,monkeypatch):
    adapter,payload,page,events,identity,media=case
    from test_channels_publish_form import live_form
    current={'reminder':False};held={}
    def evaluate(script):
        if script==module.ORIGINAL_REMINDER:return current['reminder']
        if script==form.READ_FORM:
            raw=live_form(form.validate_form(payload),media);raw['original_intercept']=True
            return raw
        return identity
    page.evaluate=evaluate
    monkeypatch.setattr(adapter,'_challenge',lambda s:'complex' if current['reminder'] else None)
    def click(s,surface):
        held['session']=s;current['reminder']=True;events.append(('final',None))
    monkeypatch.setattr(adapter,'_click_final',click)
    class Button:
        def count(self):return 1
        def is_visible(self):return True
        def is_enabled(self):return True
        def click(self):
            events.append(('reminder',None));current['reminder']=False
            s=held['session']
            adapter._response(s,response(form.CREATE_PATH,normal_request(form.validate_form(payload),media),
                {'errCode':0,'data':{'baseResp':{'errcode':0},'objectId':'9007199254740993'}}))
    page.locator=lambda selector:SimpleNamespace(count=lambda:0) if selector==module.ORIGINAL_REMINDER_PREFERENCE else Button()
    adapter.step_timeout=.5
    result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='platform_scheduled'
    assert result.scheduled_at==payload['scheduled_at']
    assert [e[0] for e in events].count('upload')==1
    assert [e[0] for e in events].count('final')==1
    assert [e[0] for e in events].count('reminder')==1
    assert adapter.interventions()==[]


def test_bound_success_returned_at_ui_deadline_is_still_acknowledged(case,monkeypatch):
    a,p,page,events,identity,media=case
    clock=[0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda:clock[0]))
    original=a._click_final
    def late_ack(s,surface):
        clock[0]+=a.step_timeout+1
        original(s,surface)
    monkeypatch.setattr(a,'_click_final',late_ack)
    result=a.submit(p,context=None,before_final=lambda:None)
    assert result.state=='platform_scheduled'
    assert result.scheduled_at==p['scheduled_at']
    assert events.count(('final',None))==1


def test_no_tip_preference_closes_then_submits_one_native_create_with_original_schedule(case,monkeypatch):
    a,p,page,events,identity,media=case
    from itertools import count
    ticks=count()
    # Fake browser callbacks are immediate; SQLite/CPU load is not their UI time.
    # Advance deterministically so an incomplete synthetic flow still times out.
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:next(ticks)/1000))
    from test_channels_publish_form import live_form
    current={'reminder':False,'clicks':0}
    def evaluate(script):
        if script==module.ORIGINAL_REMINDER:return current['reminder']
        if script==form.READ_FORM:
            raw=live_form(form.validate_form(p),media);raw['original_intercept']=current['reminder']
            return raw
        return identity
    page.evaluate=evaluate;a.step_timeout=.5
    monkeypatch.setattr(a,'_challenge',lambda s:'complex' if current['reminder'] else None)
    def click(s,surface):
        current['clicks']+=1
        if current['clicks']==1:current['reminder']=True;events.append(('reminder_open',None));return
        events.append(('native_create',None))
        a._response(s,response(form.CREATE_PATH,normal_request(form.validate_form(p),media),
            {'errCode':0,'data':{'baseResp':{'errcode':0},'objectId':'9007199254740993'}}))
    monkeypatch.setattr(a,'_click_final',click)
    class Preference:
        def count(self):return 1
        def is_visible(self):return True
        def is_enabled(self):return True
        def inner_text(self):return '不再提醒'
        def click(self):current['reminder']=False;events.append(('no_tip',None))
    page.locator=lambda selector:Preference()
    result=a.submit(p,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='platform_scheduled' and result.scheduled_at==p['scheduled_at']
    assert events.count(('journal',None))==events.count(('upload',None))==events.count(('native_create',None))==1
    assert events.count(('no_tip',None))==1 and a.interventions()==[]
