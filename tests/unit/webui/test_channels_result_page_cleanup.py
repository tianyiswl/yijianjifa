"""Result cleanup releases only proved idle pages; final authority stays frozen."""
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from test_channels_multi_publish import case, normal_request, response
from yijianjifa.features.publish.deliveries import digest
from yijianjifa.webui import channels_multi_publish as module
from yijianjifa.webui import channels_publish_form as form
from yijianjifa.webui.chromium_publish import account_lock


def immediate(case):
    adapter, payload, _, _, _, _ = case
    payload['execution_mode'] = 'publish_now'
    payload['scheduled_at'] = None
    adapter.bind_delivery(digest=digest(payload), delivery_id='target', task_id='batch')
    assert adapter.validate_target(payload, payload, 'publish_now').authorized
    return adapter._session(payload)


def final_request(payload, media):
    return SimpleNamespace(url='https://' + form.HOST + form.CREATE_PATH,
        method='POST', post_data_json=normal_request(form.validate_form(payload), media))


def check_route(context, path, method='POST'):
    outcome = []
    route = SimpleNamespace(request=SimpleNamespace(url='https://' + form.HOST + path, method=method),
        abort=lambda reason: outcome.append('blocked'), fallback=lambda: outcome.append('allowed'))
    context.route_handler(route)
    assert len(outcome) == 1
    return outcome[0]


def install_native_open(case, monkeypatch):
    """Keep real account-lock, open, query and route setup; replace the browser SDK."""
    adapter, _, _, events, identity, _ = case
    contexts = []
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))

    class Page:
        url = form.MANAGE_URL
        closed = False
        def on(self, *args): pass
        def goto(self, url, **kwargs):
            assert url == form.MANAGE_URL
            self.url = url
            events.append(('query_navigation', url))
        def is_closed(self): return self.closed
        def evaluate(self, script): return identity
        def wait_for_timeout(self, ms): clock[0] += ms / 1000

    class Context:
        def __init__(self): self.pages = [Page()]
        def route(self, pattern, handler):
            assert pattern == '**/*'
            self.route_handler = handler
        def set_default_timeout(self, timeout): pass
        def close(self):
            self.pages[0].closed = True
            events.append(('query_close', None))

    def launch(profile, **options):
        assert options['headless'] is True and options['service_workers'] == 'block'
        context = Context()
        contexts.append(context)
        return context

    adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=lambda: None)
    monkeypatch.setattr(adapter, '_open', module.ChannelsMultiPublishAdapter._open.__get__(adapter))
    monkeypatch.setattr(module.browser, 'browser_options', lambda mode: {'headless': mode == 'background'})
    monkeypatch.setattr(module.creator_auth, 'restore_session', lambda *args: None)
    return contexts


def test_settled_headless_201_without_id_releases_lock_but_keeps_final_and_intent(case, monkeypatch):
    adapter, payload, _, events, _, media = case
    session = immediate(case)
    session.external_browser = False
    lock = account_lock(payload['account_id'])
    assert lock.acquire(False)
    session.locked = True
    frozen = deepcopy(payload)

    def click(current, surface):
        events.append(('final', None))
        request = final_request(payload, media)
        adapter._create_request_started(current, request)
        adapter._response(current, SimpleNamespace(url=request.url, status=201, request=request,
            json=lambda: {'errCode': 0, 'data': {'baseResp': {'errcode': 0}}}))
        adapter._create_request_ended(current, request)

    monkeypatch.setattr(adapter, '_click_final', click)
    try:
        outcome = adapter.submit(payload, context=None, before_final=lambda: events.append(('journal', None)))
        assert outcome.state == 'platform_accepted' and outcome.receipt_reference == {}
        assert outcome.submission_ack['http_status'] == 201
        assert session.create_settled and not session.create_pending
        assert session.final and session.uploaded and payload == frozen
        assert session.context is None and session.page is None and not lock.locked()
        assert outcome.context is None
        with pytest.raises(ValueError, match='final already attempted'):
            adapter.submit(payload, context=outcome.context, before_final=lambda: pytest.fail('no new final'))
        assert [kind for kind, _ in events if kind in {'upload', 'journal', 'final', 'close'}] == [
            'upload', 'journal', 'final', 'close']
    finally:
        adapter.close()
        if lock.locked(): lock.release()


@pytest.mark.parametrize('unproved', ['not_final', 'unsettled', 'pending', 'verification', 'visible'])
def test_result_cleanup_requires_idle_headless_final_without_verification(case, unproved):
    adapter, payload, _, _, _, _ = case
    session = immediate(case)
    session.final = True
    session.external_browser = False
    session.create_settled = True
    if unproved == 'not_final': session.final = False
    if unproved == 'unsettled': session.create_settled = False
    if unproved == 'pending': session.create_pending[1] = 0
    if unproved == 'verification': session.request_id = 'synthetic-verification'
    if unproved == 'visible': session.external_browser = True
    assert adapter._result_page_releasable(session) is False
    adapter.close()


def test_pending_original_request_stays_live_until_its_late_receipt(case, monkeypatch):
    adapter, payload, page, events, _, media = case
    session = immediate(case)
    session.external_browser = False
    request = final_request(payload, media)
    def click(current, surface):
        events.append(('final', None))
        adapter._create_request_started(current, request)
    monkeypatch.setattr(adapter, '_click_final', click)
    first = adapter.submit(payload, context=None, before_final=lambda: events.append(('journal', None)))
    assert first.state == 'result_unknown' and session.page is page and session.create_pending
    assert not any(kind == 'close' for kind, _ in events)
    adapter._response(session, SimpleNamespace(url=request.url, status=201, request=request,
        json=lambda: {'errCode': 0, 'data': {'baseResp': {'errcode': 0}, 'objectId': '42'}}))
    adapter._create_request_ended(session, request)
    result = adapter.reconcile(payload, context=first.context)
    assert result.state == 'platform_accepted' and result.receipt_reference == {'work_id': '42'}
    assert session.context is None and session.final
    assert [kind for kind, _ in events if kind in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
    adapter.close()


def test_active_headless_post_final_verification_keeps_its_original_page(case, monkeypatch):
    adapter, payload, page, events, _, _ = case
    session = immediate(case)
    session.external_browser = False
    def click(current, surface):
        events.append(('final', None))
        current.create_settled = True
    monkeypatch.setattr(adapter, '_click_final', click)
    monkeypatch.setattr(adapter, '_challenge', lambda current: 'complex' if current.final else None)
    outcome = adapter.submit(payload, context=None, before_final=lambda: events.append(('journal', None)))
    assert outcome.state == 'result_unknown' and outcome.error_code == 'verification_required'
    assert session.request_id and session.page is page and session.context is not None
    assert not adapter._result_page_releasable(session)
    assert not any(kind == 'close' for kind, _ in events)
    assert [kind for kind, _ in events if kind in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
    adapter.close()


def test_two_reopened_management_queries_release_again_without_reupload_or_new_create(case, monkeypatch):
    adapter, payload, _, events, _, _ = case
    session = immediate(case)
    session.final = session.uploaded = True
    session.phase = 'result'
    adapter._release_page(session)
    contexts = install_native_open(case, monkeypatch)
    frozen = deepcopy(payload)
    for _ in range(2):
        outcome = adapter.reconcile(payload, context={'handle': session.handle})
        assert outcome.state == 'result_unknown'
        assert session.final and session.uploaded and payload == frozen
        assert session.page is None and session.context is None
        assert not account_lock(payload['account_id']).locked()
        assert session.query_wait_unresolved is False
        assert check_route(contexts[-1], form.CREATE_PATH) == 'blocked'
        assert check_route(contexts[-1], '/cgi-bin/mmfinderassistant-bin/post/post_edit') == 'blocked'
        assert check_route(contexts[-1], form.LIST_PATH) == 'allowed'
        assert check_route(contexts[-1], '/platform/post/list', method='GET') == 'allowed'
    assert len(contexts) == 2
    assert sum(kind == 'query_close' for kind, _ in events) == 2
    assert not any(kind in {'upload', 'journal', 'final'} for kind, _ in events)
    with pytest.raises(ValueError, match='final already attempted'):
        adapter.submit(payload, context=None, before_final=lambda: pytest.fail('query cannot authorize final'))
    adapter.close()


def test_existing_live_final_context_is_not_replaced_by_a_new_query_guard(case, monkeypatch):
    adapter, payload, _, events, _, _ = case
    session = adapter._session(payload)
    contexts = install_native_open(case, monkeypatch)
    adapter._open(session)
    original = session.context
    session.final = session.uploaded = True
    session.phase = 'result'
    session.create_pending = {1: 0}
    adapter._open(session, query=True)
    assert session.context is original and len(contexts) == 1
    assert check_route(original, form.CREATE_PATH) == 'allowed'
    assert not adapter._result_page_releasable(session)
    assert not any(kind == 'query_close' for kind, _ in events)
    adapter.close()


def test_new_management_query_does_not_inherit_historical_final_write_permission(case, monkeypatch):
    adapter, payload, _, _, _, _ = case
    session = adapter._session(payload)
    session.final = session.uploaded = True
    session.phase = 'result'
    contexts = install_native_open(case, monkeypatch)
    try:
        adapter._open(session, query=True)
        assert check_route(contexts[0], form.CREATE_PATH) == 'blocked'
        assert check_route(contexts[0], form.LIST_PATH) == 'allowed'
        assert session.final and session.uploaded
    finally:
        adapter.close()


@pytest.mark.parametrize('failure', ['launch', 'guard', 'navigation'])
def test_new_query_setup_failure_releases_only_its_new_context_and_account(case, monkeypatch, failure):
    adapter, payload, _, _, _, _ = case
    session = adapter._session(payload)
    session.final = session.uploaded = True
    contexts = install_native_open(case, monkeypatch)
    launch = adapter.driver.chromium.launch_persistent_context
    def fail(*args, **kwargs): raise RuntimeError('synthetic query ' + failure)
    def prepare(*args, **kwargs):
        if failure == 'launch': return fail()
        context = launch(*args, **kwargs)
        monkeypatch.setattr(context if failure == 'guard' else context.pages[0],
                            'route' if failure == 'guard' else 'goto', fail)
        return context
    monkeypatch.setattr(adapter.driver.chromium, 'launch_persistent_context', prepare)
    try:
        with pytest.raises(RuntimeError, match='synthetic query ' + failure):
            adapter._open(session, query=True)
        assert not account_lock(payload['account_id']).locked()
        assert session.context is None and session.page is None
        assert session.final and session.uploaded
        assert all(context.pages[0].closed for context in contexts)
    finally:
        adapter.close()


def test_new_query_navigation_and_cleanup_failure_keeps_retry_without_hiding_original_error(case, monkeypatch):
    adapter, payload, _, _, _, _ = case
    session = adapter._session(payload)
    session.final = session.uploaded = True
    contexts = install_native_open(case, monkeypatch)
    launch = adapter.driver.chromium.launch_persistent_context
    actual_close = []
    def fail_navigation(*args, **kwargs): raise ValueError('synthetic original navigation error')
    def fail_close(): raise RuntimeError('synthetic cleanup error')
    def prepare(*args, **kwargs):
        context = launch(*args, **kwargs)
        actual_close.append(context.close)
        monkeypatch.setattr(context.pages[0], 'goto', fail_navigation)
        monkeypatch.setattr(context, 'close', fail_close)
        return context
    monkeypatch.setattr(adapter.driver.chromium, 'launch_persistent_context', prepare)
    try:
        with pytest.raises(ValueError, match='synthetic original navigation error'):
            adapter._open(session, query=True)
        assert session.cleanup_pending and session.context is contexts[0]
        assert account_lock(payload['account_id']).locked()
        adapter._open(session, query=True)
        assert len(contexts) == 1 and session.context is contexts[0] and session.cleanup_pending
        monkeypatch.setattr(contexts[0], 'close', actual_close[0])
        session.cleanup_retry_at = 0
        assert adapter.account_available(payload)
        assert session.context is None and not account_lock(payload['account_id']).locked()
        assert session.final and session.uploaded
    finally:
        if contexts:
            monkeypatch.setattr(contexts[0], 'close', actual_close[0])
        adapter.close()


def test_new_query_observation_failure_before_shared_step_releases_the_query(case, monkeypatch):
    adapter, payload, _, _, _, _ = case
    session = adapter._session(payload)
    session.final = session.uploaded = True
    session.phase = 'result'
    install_native_open(case, monkeypatch)
    def fail(current): raise RuntimeError('synthetic query observation error')
    monkeypatch.setattr(adapter, '_challenge', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic query observation error'):
            adapter.reconcile(payload, context={'handle': session.handle})
        assert session.context is None and session.page is None
        assert not account_lock(payload['account_id']).locked()
        assert session.final and session.uploaded
    finally:
        adapter.close()
