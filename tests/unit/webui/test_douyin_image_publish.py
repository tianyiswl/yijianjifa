import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


def module():
    try:
        return importlib.import_module('yijianjifa.webui.douyin_image_publish')
    except ImportError:
        pytest.fail('抖音图文发布通道尚未接入')


def upload_request(name, data):
    body = (b'--fixture\r\nContent-Disposition: form-data; name="image"; filename="' + name.encode() +
            b'"\r\nContent-Type: image/jpeg\r\n\r\n' + data + b'\r\n--fixture--\r\n')
    return SimpleNamespace(url='https://creator.douyin.com/upload/image/',
                           headers={'content-type': 'multipart/form-data; boundary=fixture'}, post_data_buffer=body)


def upload_response(request, uri):
    return SimpleNamespace(request=request, status=200, json=lambda: {'data': {'ImageUri': uri}, 'status_code': 0})


def test_upload_ledger_binds_frozen_bytes_and_preserves_input_order(tmp_path):
    m = module()
    a, b = tmp_path / '0-0.jpg', tmp_path / '0-1.jpg'
    a.write_bytes(b'first-owned-image'); b.write_bytes(b'second-owned-image')
    images = [{'path': str(p), 'position': i, 'content_hash': hashlib.sha256(p.read_bytes()).hexdigest()} for i, p in enumerate([a, b])]
    ledger = m.ImageUploadLedger(images)
    second = upload_request(b.name, b.read_bytes()); first = upload_request(a.name, a.read_bytes())
    ledger.observe_upload(second, upload_response(second, 'tos-second'))
    assert ledger.ordered_ids == []
    ledger.observe_upload(first, upload_response(first, 'tos-first'))
    assert ledger.ordered_ids == ['tos-first', 'tos-second']
    foreign = upload_request(a.name, b'not-this-task')
    ledger.observe_upload(foreign, upload_response(foreign, 'foreign-uri'))
    assert ledger.ordered_ids == ['tos-first', 'tos-second']


def test_receipt_requires_this_image_order_and_exact_schedule():
    m = module()
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0, microsecond=0)
    payload = {'platform_user_id': '111', 'execution_mode': 'platform_schedule', 'scheduled_at': when.isoformat(),
               'fields': {'title': '测试标题', 'body': '正文', 'tags': '#测试'}}
    request = {'title': '测试标题', 'desc': '正文 #测试', 'images': [{'uri': 'a'}, {'uri': 'b'}],
               'scheduled_publish_time': int(when.timestamp())}
    response = {'status_code': 0, 'aweme': {'aweme_id': '1234567890'}}
    assert m.verified_image_receipt(payload, request, response, ['a', 'b']) == '1234567890'
    assert m.verified_image_receipt(payload, {**request, 'images': [{'uri': 'b'}, {'uri': 'a'}]}, response, ['a', 'b']) is None
    assert m.verified_image_receipt(payload, {**request, 'scheduled_publish_time': 0}, response, ['a', 'b']) is None
    assert m.verified_image_receipt(payload, {**request, 'is_schedule':False}, response, ['a','b']) is None
    assert m.verified_image_receipt(payload, request, {'status_code': 0}, ['a', 'b']) is None
    assert m.verified_image_receipt(payload, request, {'status_code': 1, 'aweme': {'aweme_id': '1234567890'}}, ['a', 'b']) is None


def test_binary_storage_ack_requires_our_bytes_crc_host_and_success():
    import zlib
    m=module();data=b'owned-image-binary'
    images=[{'position':0,'content_hash':hashlib.sha256(data).hexdigest()}]
    request=SimpleNamespace(url='https://tos-uploader.douyin.com/upload/v1/tos-cn-i-jm8ajry58r/'+'a'*32,
        headers={'content-type':'application/octet-stream'},post_data_buffer=data)
    good={'code':2000,'data':{'crc32':str(zlib.crc32(data)&0xffffffff)}}
    for payload in ({**good,'code':4000},{'code':2000,'data':{'crc32':'wrong'}},good):
        ledger=m.ImageUploadLedger(images)
        ledger.observe_upload(request,SimpleNamespace(status=200,json=lambda:payload))
        assert ledger.ordered_ids==(['tos-cn-i-jm8ajry58r/'+'a'*32] if payload is good else [])
    request.url=request.url.replace('tos-uploader.douyin.com','foreign.example')
    ledger=m.ImageUploadLedger(images);ledger.observe_upload(request,SimpleNamespace(status=200,json=lambda:good))
    assert ledger.ordered_ids==[]
    request.url=request.url.replace('foreign.example','tos-d-x-lf.snssdk.com')
    payload={'code':2000,'data':{'crc32':format(zlib.crc32(data)&0xffffffff,'08x')}}
    ledger=m.ImageUploadLedger(images);ledger.observe_upload(request,SimpleNamespace(status=200,json=lambda:payload))
    assert ledger.ordered_ids==['tos-cn-i-jm8ajry58r/'+'a'*32]
    request.url=request.url.replace('tos-d-x-lf.snssdk.com','tos-lq-x.bytedancevod.com')
    ledger=m.ImageUploadLedger(images);ledger.observe_upload(request,SimpleNamespace(status=200,json=lambda:payload))
    assert ledger.ordered_ids==['tos-cn-i-jm8ajry58r/'+'a'*32]


def test_immediate_receipt_rejects_hidden_scheduled_time_and_wrong_metadata():
    m = module()
    payload = {'platform_user_id': '111', 'execution_mode': 'publish_now', 'scheduled_at': None,
               'fields': {'title': '测试标题', 'body': '正文', 'tags': ''}}
    request = {'title': '测试标题', 'desc': '正文', 'images': ['a'], 'scheduled_publish_time': 0}
    response = {'status_code': 0, 'aweme_id': '12345'}
    assert m.verified_image_receipt(payload, request, response, ['a']) == '12345'
    assert m.verified_image_receipt(payload, {**request, 'title': '另一篇'}, response, ['a']) is None
    assert m.verified_image_receipt(payload, {**request, 'scheduled_publish_time': 12345}, response, ['a']) is None


def test_repeated_photo_requires_each_upload_response(tmp_path):
    m = module()
    data = b'repeated-owned-image'
    images = [{'position': i, 'content_hash': hashlib.sha256(data).hexdigest()} for i in range(2)]
    ledger = m.ImageUploadLedger(images)
    one = upload_request('one.jpg', data)
    ledger.observe_upload(one, upload_response(one, 'same-server-uri'))
    ledger.observe_upload(one, upload_response(one, 'same-server-uri'))
    assert ledger.ordered_ids == []
    two = upload_request('two.jpg', data)
    ledger.observe_upload(two, upload_response(two, 'same-server-uri'))
    assert ledger.ordered_ids == ['same-server-uri', 'same-server-uri']


@pytest.fixture
def image_browser(db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import douyin_image_publish as m
    from yijianjifa.webui import douyin_image_form as f
    from yijianjifa.features.accounts.service import AccountService, AccountRow
    from yijianjifa.features.publish.deliveries import digest
    from yijianjifa.storage.db import session_scope
    account = AccountService(db_engine).create('douyin', '111', '合成测试账号')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id)
        row.session_ref = 'fixture-image-session'
    root = app_paths.data_dir / 'publish_resources' / 'fixture'
    root.mkdir(parents=True)
    photos = []
    for index in range(2):
        path = root / (str(index) + '.jpg')
        path.write_bytes(('owned-image-' + str(index)).encode())
        photos.append({'path': str(path), 'position': index, 'content_hash': hashlib.sha256(path.read_bytes()).hexdigest()})
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0, microsecond=0)
    payload = {'platform': 'douyin', 'content_type': 'image_post', 'account_id': account.id, 'platform_user_id': '111',
        'images': photos, 'fields': {'title': '合成图文', 'body': '合成正文', 'tags': '#测试'},
        'execution_mode': 'platform_schedule', 'scheduled_at': when.isoformat(),
        'session_fingerprint': hashlib.sha256(b'fixture-image-session').hexdigest()}
    events = []
    page = SimpleNamespace(url='https://creator.douyin.com/creator-micro/content/manage', challenge=None,
                           confirm=False, finished=False, bad_order=False, uid='111', closed=False)
    def goto(url, **kwargs):
        page.url = url
        events.append(('goto', url))
    page.goto = goto
    page.wait_for_timeout = lambda n: None
    page.screenshot = lambda **kwargs: b'fixture-screen'
    page.evaluate = lambda script, *args: page.challenge
    page.locator = lambda selector: SimpleNamespace(count=lambda: 1, set_input_files=lambda files: events.append(('upload', files)))
    page.context = SimpleNamespace(close=lambda: events.append(('close', None)))
    adapter = m.DouyinImagePostAdapter(db_engine, app_paths, step_timeout=1)
    adapter.bind_delivery(digest=digest(payload), delivery_id='fixture-delivery', task_id='fixture-task')
    def open_session(s, **kwargs):
        if s.page is not None: return
        s.page = page; s.context = page.context; s.original_ref = 'fixture-image-session'
        s.image_uploads = SimpleNamespace(ordered_ids=['server-one', 'server-two'])
        s.image_receipt = None; s.image_request = None; s.image_request_invalid = False; s.image_confirm_attempted = False
    monkeypatch.setattr(adapter, '_open', open_session)
    monkeypatch.setattr(m.legacy, 'session_blob', lambda *args: b'fixture-not-real-cookie')
    def evaluate(target, script, args):
        if 'return identity;' in script:
            return {'uid': page.uid, 'uid_after': page.uid, 'status': 0, 'after_status': 0, 'profile_status': 0, 'nickname': '合成账号'}
        if script == f.PREPARE_IMAGE_FORM:
            result = {'identity': {'uid': page.uid, 'uid_after': page.uid, 'status': 0, 'after_status': 0, 'profile_status': 0, 'nickname': '合成账号'},
                'path': '/creator-micro/content/post/image', 'image_page': True, 'ready': True,
                'title': args['title_text'], 'body': args['body_text'], 'topics': args['topic_names'],
                'declaration': args['declaration_label'], 'declaration_verified': True,
                'schedule_mode': 'scheduled', 'scheduled_time_text': args['scheduled_time_text'],
                'title_count': 1, 'editor_count': 1, 'mode_controls': 1, 'final_button_count': 1, 'final_button_enabled': True,
                'image_count': 2, 'image_names': args['image_names'], 'image_order_verified': True, 'upload_complete': True,
                'image_upload_ids': args['image_upload_ids']}
            if page.bad_order: result.update(ready=False, failure_code='image_order_unverified', image_order_verified=False)
            return result
        if script == f.FINAL_IMAGE_CLICK:
            events.append(('final', args['scheduled_time_text']))
            page.confirm = True
            return {'attempted': True}
        if script == f.CONFIRM_IMAGE_SUBMISSION:
            if page.confirm:
                events.append(('confirm', args['scheduled_time_text']))
                page.confirm = False
                session = next(iter(adapter.sessions.values()))
                session.image_request = {'fixture': True}; session.image_receipt = '1234567890'
                return {'attempted': True}
            return {'confirmation': False, 'attempted': False}
        if script == f.READ_IMAGE_RESULT:
            return {'ready': False}
        raise AssertionError('Unexpected controller')
    monkeypatch.setattr(m.legacy, 'evaluate_script', evaluate)
    return adapter, payload, page, events


def test_image_session_uploads_once_confirms_same_clock_and_closes(image_browser):
    a, p, page, events = image_browser
    result = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'platform_scheduled' and result.scheduled_at == p['scheduled_at']
    assert [event[0] for event in events] == ['goto', 'upload', 'journal', 'final', 'confirm', 'close']
    assert events[3][1] == events[4][1]
    assert result.receipt_reference == {'kind': 'image_post', 'item_id': '1234567890'}


def test_unverified_image_order_never_submits(image_browser):
    a, p, page, events = image_browser
    page.bad_order = True
    result = a.submit(p, context=None, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'pre_submit_failed'
    assert not any(event[0] in {'journal', 'final', 'confirm'} for event in events)


def test_changed_live_image_account_never_uploads(image_browser):
    a, p, page, events = image_browser
    page.uid = '222'
    result = a.submit(p, context=None, before_final=lambda: pytest.fail('wrong account submitted'))
    assert result.error_code == 'account_mismatch'
    assert not any(event[0] == 'upload' for event in events)


def test_image_verification_yields_and_resume_preserves_original_page(image_browser):
    a, p, page, events = image_browser
    page.challenge = {'complex': True}
    first = a.submit(p, context=None, before_final=lambda: pytest.fail('challenge submitted'))
    assert first.state == 'paused_auth'
    page.challenge = None
    a.authorize_resume('fixture-delivery')
    request = a.interventions()[0]
    a.respond(request['id'], 'done')
    result = a.resume(p, context=first.context, before_final=lambda: events.append(('journal', None)))
    assert result.state == 'platform_scheduled'
    assert sum(event[0] == 'upload' for event in events) == 1
    assert sum(event[0] == 'final' for event in events) == 1


def test_result_query_never_confirms_or_repeats_original_submission(image_browser):
    a, p, page, events = image_browser
    s = a._session(p)
    a._open(s)
    s.final = True
    page.url = 'https://creator.douyin.com/creator-micro/content/post/image'
    page.confirm = True
    a._prepare_result_query(s)
    assert page.confirm and not events
    result = a.reconcile(p, context={'handle': s.handle})
    assert result.state == 'result_unknown'
    assert page.confirm and not events


def test_upload_proof_survives_restart_and_rejects_changed_scope(image_browser):
    a,p,page,events=image_browser
    s=a._session(p);a._open(s)
    a._persist_upload_proof(s)
    m=module()
    from yijianjifa.webui.chromium_publish_session import LiveSession
    restored=LiveSession(p,dict(s.binding))
    restored.image_uploads=m.ImageUploadLedger(p['images'])
    a._restore_upload_proof(restored)
    assert restored.image_uploads.ordered_ids==['server-one','server-two']
    restored.binding['attempt']+=1
    restored.image_uploads=m.ImageUploadLedger(p['images'])
    a._restore_upload_proof(restored)
    assert restored.image_uploads.ordered_ids==[]
    restored.binding=dict(s.binding)
    restored.payload={**p,'platform_user_id':'another-account'}
    a._restore_upload_proof(restored)
    assert restored.image_uploads.ordered_ids==[]


def test_upload_proof_cannot_be_replaced_by_later_upload(image_browser):
    a,p,page,events=image_browser
    s=a._session(p);a._open(s);a._persist_upload_proof(s)
    s.image_uploads=SimpleNamespace(ordered_ids=['replacement-one','replacement-two'])
    with pytest.raises(ValueError,match='proof changed'):
        a._persist_upload_proof(s)
