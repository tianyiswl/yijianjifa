"""Prepare-only checks exercise real adapter boundaries, with no platform IO."""
import threading
from types import SimpleNamespace

import pytest

from test_bilibili_multi_publish import case as bilibili_case
from test_channels_multi_publish import case as channels_case
from test_kuaishou_multi_publish import case as kuaishou_case
from yijianjifa.webui.bilibili_multi_publish import BilibiliMultiPublishAdapter
from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
from yijianjifa.webui.kuaishou_multi_publish import KuaishouMultiPublishAdapter
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.publish.deliveries import digest
from yijianjifa.storage.db import session_scope


@pytest.fixture(params=['channels_case', 'bilibili_case', 'kuaishou_case'])
def inspection_case(request):
    case = request.getfixturevalue(request.param)
    case[0].inspection_only = True
    return case


def forbidden_final():
    raise AssertionError('inspection must never request final authority')


def test_checked_form_closes_without_journal_final_or_receipt(inspection_case):
    adapter, payload, _, events, *_ = inspection_case
    assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
    result = adapter.submit(payload, context=None, before_final=forbidden_final)
    assert result.state == 'inspection_complete'
    assert result.context == {'checks': ['account', 'upload', 'fields', 'schedule']}
    assert result.receipt_reference is None and result.scheduled_at is None
    assert [event[0] for event in events if event[0] in {'upload', 'journal', 'final', 'close'}] == ['upload', 'close']
    session = adapter._session(payload)
    assert session.final is False and session.page is None and session.context is None
    with pytest.raises(ValueError, match='inspection already complete'):
        adapter.submit(payload, context=None, before_final=forbidden_final)
    adapter.close()


def test_inspection_verification_resumes_the_same_upload_without_final(inspection_case, monkeypatch):
    adapter, payload, page, events, *_ = inspection_case
    pending = [True]
    original = adapter._prepare_form

    def prepare(session, surface, form):
        if pending[0]:
            return False
        return original(session, surface, form)

    def challenge(session):
        return 'complex' if session.uploaded and pending[0] else None

    monkeypatch.setattr(adapter, '_prepare_form', prepare)
    monkeypatch.setattr(adapter, '_challenge', challenge)
    parked = adapter.submit(payload, context=None, before_final=forbidden_final)
    assert parked.state == 'paused_auth'
    session = adapter._session(payload)
    assert session.page is page and session.uploaded
    request = adapter.interventions()[0]
    assert request['final_started'] is False
    with pytest.raises(ValueError, match='explicit same-identity resume required'):
        adapter.resume(payload, context=parked.context, before_final=forbidden_final)
    adapter.respond(request['id'], 'done')
    pending[0] = False
    result = adapter.resume(payload, context=parked.context, before_final=forbidden_final)
    assert result.state == 'inspection_complete'
    assert [e[0] for e in events if e[0] in {'upload', 'final', 'close'}] == ['upload', 'close']
    assert not adapter.interventions()
    adapter.close()


@pytest.mark.parametrize('cls', [ChannelsMultiPublishAdapter, BilibiliMultiPublishAdapter, KuaishouMultiPublishAdapter])
def test_inspection_factory_is_dedicated_and_cannot_query_submission(db_engine, app_paths, cls):
    regular = cls(db_engine, app_paths, expected_uid='same-uid', step_timeout=12)
    event = threading.Event()
    inspection = regular.create_inspection_adapter(event)
    assert type(inspection) is cls and inspection is not regular
    assert inspection.inspection_only is True and regular.inspection_only is False
    assert inspection.cancel is event and regular.cancel is not event
    assert inspection.expected_uid == 'same-uid' and inspection.step_timeout == 12
    assert inspection.sessions is not regular.sessions
    with pytest.raises(ValueError, match='inspection cannot query submission'):
        inspection.reconcile({}, context=None)


BLOCKED = [
    ('channels', 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create', 'POST'),
    ('channels', 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create', 'GET'),
    ('channels', 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_draft_save', 'POST'),
    ('channels', 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/new_api', 'POST'),
    ('bilibili', 'https://member.bilibili.com/x/vu/web/add/v3', 'POST'),
    ('bilibili', 'https://member.bilibili.com/x/vu/web/edit', 'POST'),
    ('bilibili', 'https://member.bilibili.com/x/vu/web/draft/save', 'POST'),
    ('bilibili', 'https://member.bilibili.com/x/vu/web/archive/new_api', 'POST'),
    ('kuaishou', 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/submit', 'POST'),
    ('kuaishou', 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/draft/save', 'POST'),
    ('kuaishou', 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/new_api', 'PUT'),
]

ALLOWED = [
    ('channels', 'https://channels.weixin.qq.com/platform/post/create', 'GET'),
    ('channels', 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_list', 'POST'),
    ('channels', 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/helper/helper_upload', 'POST'),
    ('channels', 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/verify', 'POST'),
    ('bilibili', 'https://member.bilibili.com/platform/upload/video/frame', 'GET'),
    ('bilibili', 'https://member.bilibili.com/x/vupre/web/archive/pre', 'POST'),
    ('bilibili', 'https://member.bilibili.com/x/vu/web/cover/up', 'POST'),
    ('bilibili', 'https://passport.bilibili.com/x/passport-login/web/login', 'POST'),
    ('bilibili', 'https://upos-sz-upcdn.bilivideo.com/upload-token', 'PUT'),
    ('kuaishou', 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/photo/list', 'POST'),
    ('kuaishou', 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/upload/pre', 'POST'),
    ('kuaishou', 'https://cp.kuaishou.com/rest/cp/works/v2/video/pc/upload/finish', 'POST'),
]

CLASSES = {'channels': ChannelsMultiPublishAdapter, 'bilibili': BilibiliMultiPublishAdapter, 'kuaishou': KuaishouMultiPublishAdapter}


@pytest.mark.parametrize('platform,url,method', BLOCKED + ALLOWED)
def test_inspection_network_guard_blocks_writes_but_keeps_upload_auth_queries(db_engine, app_paths, platform, url, method):
    adapter = CLASSES[platform](db_engine, app_paths, inspection_only=True)
    calls = []
    context = SimpleNamespace(route=lambda pattern, handler: calls.append((pattern, handler)))
    adapter._install_inspection_guard(context)
    assert len(calls) == 1 and calls[0][0] == '**/*'
    operations = []
    request = SimpleNamespace(url=url, method=method)
    route = SimpleNamespace(request=request, abort=lambda *args: operations.append('abort'), fallback=lambda: operations.append('fallback'))
    calls[0][1](route)
    assert operations == ['abort' if (platform, url, method) in BLOCKED else 'fallback']


@pytest.mark.parametrize('cls', list(CLASSES.values()))
def test_regular_mode_does_not_install_inspection_network_policy(db_engine, app_paths, cls):
    adapter = cls(db_engine, app_paths)
    calls = []
    adapter._install_inspection_guard(SimpleNamespace(route=lambda *args: calls.append(args)))
    assert calls == []


@pytest.mark.parametrize('platform', list(CLASSES))
def test_guard_installed_before_restore_or_first_navigation(db_engine, app_paths, monkeypatch, platform):
    import importlib
    module = importlib.import_module('yijianjifa.webui.' + platform + '_multi_publish')
    account = AccountService(db_engine).create(platform, '111', '合成账号')
    with session_scope(db_engine) as db:
        db.get(AccountRow, account.id).session_ref = 'synthetic-ref'
    events = []
    page = SimpleNamespace(on=lambda *args: None, goto=lambda *args, **kwargs: events.append('navigate'))
    context = SimpleNamespace(pages=[page], route=lambda *args: events.append('guard'),
                              set_default_timeout=lambda *args: events.append('timeout'), close=lambda: None)
    options = []

    def launch(*args, **kwargs):
        options.append(kwargs)
        return context

    adapter = CLASSES[platform](db_engine, app_paths, inspection_only=True)
    adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    if platform == 'kuaishou':
        monkeypatch.setattr(module.auth, 'chromium_profile', lambda *args: app_paths.cache_dir)
        monkeypatch.setattr(module.auth, 'restore_session', lambda *args: events.append('restore'))
    else:
        monkeypatch.setattr(module.creator_auth, 'chromium_profile', lambda *args: app_paths.cache_dir)
        monkeypatch.setattr(module.creator_auth, 'restore_session', lambda *args: events.append('restore'))
    payload = dict(platform=platform, account_id=account.id, platform_user_id='111')
    adapter.bind_delivery(digest=digest(payload), delivery_id='inspection', task_id='inspection')
    session = adapter._session(payload)
    try:
        adapter._open(session)
        assert options[0]['service_workers'] == 'block'
        assert events == ['guard', 'timeout', 'restore', 'navigate']
    finally:
        adapter._release_page(session)


def test_cancelled_verification_closes_inspection_and_removes_request(inspection_case, monkeypatch):
    adapter, payload, page, events, *_ = inspection_case
    monkeypatch.setattr(adapter, '_challenge', lambda session: 'complex')
    parked = adapter.submit(payload, context=None, before_final=forbidden_final)
    assert parked.state == 'paused_auth' and adapter._session(payload).page is page
    adapter.cancel_delivery_session('target')
    assert not adapter.interventions() and not adapter.sessions
    assert [e[0] for e in events if e[0] in {'upload', 'final', 'close'}] == ['close']
    adapter.close()


def test_direct_final_helper_is_disabled_in_inspection(inspection_case):
    adapter, payload, _, events, *_ = inspection_case
    # Call the real class method, rather than the platform test's final stub.
    with pytest.raises(ValueError, match='inspection cannot publish'):
        type(adapter)._click_final(adapter, adapter._session(payload), object())
    assert not events


def test_inspection_completion_does_not_hide_failed_browser_close(inspection_case):
    adapter, payload, page, events, *_ = inspection_case
    assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
    session = adapter._session(payload)
    context = session.context

    def fail_close():
        events.append(('close-failed', None))
        raise RuntimeError('synthetic close failure')

    context.close = fail_close
    with pytest.raises(ValueError, match='inspection browser close failed'):
        adapter.submit(payload, context=None, before_final=forbidden_final)
    assert session.page is page and session.context is context and not session.final
    assert not session.locked and not adapter.interventions()
    context.close = lambda: events.append(('close-retried', None))
    adapter.close()
    assert events[-1][0] == 'close-retried' and not adapter.sessions


def test_inspection_shutdown_stops_dedicated_driver_even_when_context_close_fails(inspection_case):
    adapter, payload, _, events, *_ = inspection_case
    adapter.validate_target(payload, payload, payload['execution_mode'])

    def fail_close():
        raise RuntimeError('synthetic close failure')

    adapter._session(payload).context.close = fail_close
    adapter.driver = SimpleNamespace(stop=lambda: events.append(('stop-driver', None)))
    with pytest.raises(ValueError, match='inspection browser close failed'):
        adapter.close()
    assert events[-1][0] == 'stop-driver'
    assert adapter.driver is None and not adapter.sessions


@pytest.fixture(scope='module')
def offline_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, channel='chromium')
        try:
            yield browser
        finally:
            browser.close()


@pytest.mark.parametrize('platform', list(CLASSES))
def test_pointer_final_from_original_page_is_aborted_before_transport(db_engine, app_paths, offline_browser, platform):
    adapter = CLASSES[platform](db_engine, app_paths, inspection_only=True)
    context = offline_browser.new_context(service_workers='block')
    transport = []

    def fixture_transport(route):
        transport.append((route.request.method, route.request.url))
        route.fulfill(status=200, body='{}', content_type='application/json', headers={'Access-Control-Allow-Origin': '*'})

    context.route('**/*', fixture_transport)
    adapter._install_inspection_guard(context)
    page = context.new_page()
    final_url = next(url for name, url, method in BLOCKED if name == platform and method == 'POST')
    upload_url = next(url for name, url, method in ALLOWED if name == platform and method == 'POST' and ('upload' in url or 'cover/up' in url))
    try:
        page.set_content('<button id="final">发表</button><p id="result"></p>')
        page.evaluate('''url => document.querySelector('#final').onclick = () => {
          fetch(url, {method:'POST', body:'{}'}).then(() => document.querySelector('#result').textContent='sent')
            .catch(() => document.querySelector('#result').textContent='blocked');
        }''', final_url)
        box = page.locator('#final').bounding_box()
        page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        page.wait_for_function("document.querySelector('#result').textContent === 'blocked'")
        assert not transport
        assert page.evaluate("url => fetch(url,{method:'POST',body:'{}'}).then(() => true).catch(() => false)", upload_url)
        assert transport == [('POST', upload_url)]
    finally:
        context.close()
