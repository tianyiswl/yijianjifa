"""Offline stage evidence for the real Douyin adapter, without platform IO."""
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_chromium_multi_publish import browser, adapter


def saved_events(engine):
    with session_scope(engine) as session:
        row = session.get(Setting, 'publish.execution:delivery:1')
        return json.loads(row.value_json) if row else []


@pytest.mark.parametrize(('stage', 'expected'), [
    ('driver_start', 'page_open'), ('browser_launch', 'page_open'), ('session_restore', 'session_restore'),
    ('navigation', 'page_navigation'), ('identity', 'account_observation'),
])
def test_preparation_sdk_timeouts_have_their_actual_finite_stage_without_private_data(
        browser, db_engine, app_paths, monkeypatch, stage, expected):
    from yijianjifa.webui import chromium_publish as legacy
    payload, pages, _, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        worker._release_page(worker._session(payload))
        def fail(*args, **kwargs): raise TimeoutError('PRIVATE_RAW_ERROR /private/profile uid=111 Cookie=SECRET')
        if stage == 'driver_start':
            from yijianjifa.webui import playwright_runtime
            worker.driver.stop(); worker.driver = None
            monkeypatch.setattr(playwright_runtime, 'acquire_playwright', fail)
        elif stage == 'browser_launch': monkeypatch.setattr(worker.driver.chromium, 'launch_persistent_context', fail)
        elif stage == 'session_restore': monkeypatch.setattr(legacy, 'restore_account_session', fail)
        elif stage == 'navigation': monkeypatch.setattr(type(pages[0]), 'goto', fail)
        else:
            original = type(pages[0]).evaluate
            def evaluate(page, code, args=None):
                if 'return identity;' in code: return fail()
                return original(page, code, args)
            monkeypatch.setattr(type(pages[0]), 'evaluate', evaluate)
        with pytest.raises(TimeoutError): worker.validate_target(payload, payload, payload['execution_mode'])
        diagnostic = worker.failure_diagnostic(TimeoutError())
        assert diagnostic == {'error_stage': expected, 'error_category': 'timeout'}
        failures = [event for event in saved_events(db_engine) if event['step'] == 'douyin.' + stage
                    and event['checks'].get('state') == 'failed']
        assert len(failures) == 1 and failures[0]['checks']['elapsed_ms'] >= 0
        assert all(value not in json.dumps(saved_events(db_engine)) for value in ('PRIVATE', 'SECRET', '/private', 'uid='))
    finally:
        worker.close()


def test_a_nonready_baseline_reproduces_the_plain_timeout_but_now_records_why_it_is_not_ready(
        browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_publish as legacy
    from yijianjifa.webui import chromium_multi_publish as module
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        page = pages[0]; original = page.evaluate
        def evaluate(code, args=None):
            if 'p011ResultNeedsSearch' in code:
                return {'identity': identity, 'path': '/creator-micro/content/manage', 'ready': False,
                        'diagnostic': {'search_count': 0, 'search_matches': False, 'loading': False}}
            return original(code, args)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        clock = [0.0]
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda n: clock.__setitem__(0, clock[0] + n / 1000))
        worker.step_timeout = .2
        result = worker.submit(payload, context=None, before_final=lambda: pytest.fail('not ready cannot submit'))
        assert result.state == 'pre_submit_failed' and result.error_code == 'adapter_failed'
        assert result.error_stage == 'page_preparation' and result.error_category == 'timeout'
        assert not any(event[0] in {'upload', 'final'} for event in events)
        saved = saved_events(db_engine)
        assert not any(event['step'] == 'execution.error' for event in saved)
        gates = next(event['checks'] for event in saved if event['step'] == 'douyin.baseline_readback')
        assert gates['ready'] is False and gates['search_count'] == 0 and gates['page_kind'] == 'manage'
        timeout = next(event['checks'] for event in saved if event['step'] == 'douyin.timeout')
        assert timeout['phase'] == 'baseline' and timeout['uploaded'] is False and timeout['final_started'] is False
    finally:
        worker.close()


@pytest.mark.parametrize(('preparation_timeout', 'expected_form_reads'), [(.35, 3), (.65, 6)])
def test_a_nonready_uploaded_form_is_distinct_from_baseline_and_does_not_upload_twice(
        browser, db_engine, app_paths, monkeypatch, preparation_timeout, expected_form_reads):
    from yijianjifa.webui import chromium_publish as legacy
    from yijianjifa.webui import chromium_multi_publish as module
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        page = pages[0]; original = page.evaluate; form_reads = []
        def evaluate(code, args=None):
            if code != legacy.CHALLENGE and 'return identity;' not in code and 'p011ResultNeedsSearch' not in code:
                form_reads.append(code)
                page.url = 'https://' + legacy.HOST + legacy.PUBLISH_PATH
                return {'identity': identity, 'path': legacy.PUBLISH_PATH, 'ready': False, 'upload_complete': False,
                        'title_count': 1, 'editor_count': 1, 'final_button_count': 1}
            return original(code, args)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        clock = [0.0]
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda n: clock.__setitem__(0, clock[0] + n / 1000))
        worker.step_timeout = .35
        # Upload preparation owns a separate deadline from baseline/result
        # checks. Bound that virtual interval too, retaining real SQLite logs.
        worker.preparation_timeout = preparation_timeout
        result = worker.submit(payload, context=None, before_final=lambda: pytest.fail('incomplete upload cannot submit'))
        assert result.state == 'pre_submit_failed' and result.error_code == 'adapter_failed'
        assert result.error_stage == 'upload_preparation' and result.error_category == 'timeout'
        assert sum(event[0] == 'upload' for event in events) == 1
        assert not any(event[0] == 'final' for event in events)
        saved = saved_events(db_engine)
        gates = next(event['checks'] for event in saved if event['step'] == 'douyin.form_readback')
        assert gates['uploaded'] is True and gates['upload_complete'] is False
        timeout = next(event['checks'] for event in saved if event['step'] == 'douyin.timeout')
        assert timeout['phase'] == 'prepare' and timeout['uploaded'] is True
        assert timeout['final_started'] is False
        # Multiple real adapter polls must precede timeout. These two budgets
        # also catch accidentally reusing the shorter baseline/result budget.
        assert len(form_reads) == expected_form_reads
    finally:
        worker.close()


def test_real_task_details_expose_a_baseline_timeout_stage_without_starting_upload_or_final(
        browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.webui import chromium_publish as legacy, chromium_multi_publish as module
    payload, pages, events, identity = browser
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def restore(context, *args):
        page = context.pages[0]; original = page.evaluate
        def evaluate(code, arguments=None):
            if 'p011ResultNeedsSearch' in code:
                return {'identity': identity, 'path': '/creator-micro/content/manage', 'ready': False,
                        'diagnostic': {'search_count': 0, 'search_matches': False, 'loading': False}}
            return original(code, arguments)
        page.evaluate = evaluate
        page.wait_for_timeout = lambda n: clock.__setitem__(0, clock[0] + n / 1000)
    monkeypatch.setattr(legacy, 'restore_account_session', restore)
    video = FinishedVideoService(db_engine).import_external(Path(payload['video_path']), duration_ms=1000, title='合成详情检查')
    worker = module.ChromiumMultiPublishAdapter(db_engine, app_paths, step_timeout=.2)
    api = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': worker})
    def call(operation, params, request_id='read'):
        result = api.dispatch(json.dumps({'v': 1, 'request_id': request_id,
            'operation': 'multi_publish.' + operation, 'params': params}), trusted_origin=True)
        assert result['ok'], result
        return result['data']
    try:
        plan = call('prepare', {'video_ids': [video.id], 'targets': [{'account_id': payload['account_id'], 'platform': 'douyin'}],
            'common_fields': {}, 'execution_mode': 'platform_schedule', 'scheduled_times': {video.id: payload['scheduled_at']}}, 'prepare')
        batch = call('confirm', {'prepared_id': plan['prepared_id'], 'intent_id': 'prepare', 'consent_partial': False}, 'confirm')
        ids = [target['id'] for target in call('detail', {'id': batch['id']})['deliveries']]
        checked = call('execution.check', {'id': batch['id'], 'delivery_ids': ids})
        call('execution.start', {'id': batch['id'], 'delivery_ids': ids, 'digest': checked['digest'],
            'consent': True, 'consent_partial': False}, 'start')
        # The adapter timeout is virtual; allow real Windows SQLite workers to
        # commit the terminal result before asserting its precise diagnostics.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            target = call('detail', {'id': batch['id']})['deliveries'][0]
            if target['state'] == 'pre_submit_failed': break
            time.sleep(.01)
        assert target['state'] == 'pre_submit_failed', target
        assert target['error_stage'] == 'page_preparation' and target['error_category'] == 'timeout'
        assert '发布页面准备失败' in target['reason_message'] and '尚未提交' in target['reason_message']
        assert not any(event[0] in {'upload', 'journal', 'final'} for event in events)
    finally:
        assert api.close(2)


def test_post_final_readiness_timeout_and_query_never_upload_or_click_again(browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_multi_publish as module
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        page = pages[0]; original = page.evaluate
        def evaluate(code, args=None):
            if 'p011ResultNeedsSearch' in code and page.result:
                return {'identity': identity, 'path': '/creator-micro/content/manage', 'ready': False,
                        'diagnostic': {'search_count': 1, 'search_matches': True, 'loading': True}}
            return original(code, args)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        clock = [0.0]
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda n: clock.__setitem__(0, clock[0] + n / 1000))
        worker.step_timeout = .5
        result = worker.submit(payload, context=None, before_final=lambda: events.append(('journal',)))
        assert result.state == 'result_unknown' and result.error_code == 'timeout'
        again = worker.reconcile(payload, context=None)
        assert again.state == 'result_unknown' and again.error_code == 'timeout'
        assert [event[0] for event in events if event[0] in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
        assert all(event['checks']['stage'] == 'submission_result' for event in saved_events(db_engine)
                   if event['step'] == 'douyin.timeout')
    finally:
        worker.close()


def test_cancellation_preserves_the_original_adapter_result_without_any_new_step(browser, db_engine, app_paths):
    payload, _, events, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        worker.cancel.set()
        result = worker.submit(payload, context=None, before_final=lambda: pytest.fail('cancelled cannot submit'))
        assert result.state == 'pre_submit_failed' and result.error_code == 'timeout'
        assert not any(event[0] in {'upload', 'final'} for event in events)
        steps = [event['step'] for event in saved_events(db_engine)]
        assert 'douyin.cancelled' in steps and 'douyin.timeout' not in steps
    finally:
        worker.close()


def test_optional_readback_diagnostics_never_raise_or_export_malformed_page_values(browser, db_engine, app_paths):
    payload, _, _, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        session = worker._session(payload)
        worker._readback(session, {'ready': False, 'path': ['PRIVATE_PATH'],
            'diagnostic': {'search_count': True, 'search_matches': 'PRIVATE', 'loading': {'cookie': 'SECRET'}},
            'message': 'uid=111 Cookie=SECRET'})
        event = saved_events(db_engine)[-1]
        assert event['checks']['page_kind'] == 'other'
        assert 'search_count' not in event['checks'] and 'loading' not in event['checks']
        assert all(private not in json.dumps(event) for private in ('PRIVATE', 'SECRET', 'uid='))
    finally:
        worker.close()


def test_normal_baseline_reuses_its_complete_identity_inside_the_unchanged_thirty_second_budget(
        browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        assert worker.step_timeout == 30
        page = pages[0]; original = page.evaluate; session = worker._session(payload)
        clock = [0.0]; baseline_reads = []; outer_phases = []; form_reads = []; final_reads = []
        def evaluate(code, args=None):
            if code == legacy.CHALLENGE: return original(code, args)
            if 'return identity;' in code:
                outer_phases.append(session.phase)
                clock[0] += 3 if session.phase == 'baseline' else .05
                return original(code, args)
            if 'p011ResultNeedsSearch' in code and session.phase == 'baseline':
                # Model the production search's input/Enter/wait, followed by two
                # stable reads. Each embedded three-GET identity costs 3 s.
                baseline_reads.append(clock[0]); clock[0] += 3
                return {'identity': dict(identity), 'path': '/creator-micro/content/manage',
                        'ready': len(baseline_reads) >= 4, 'matches': []}
            clock[0] += .05
            if 'p011FinalClicked=true' in code: final_reads.append(code)
            elif 'p011ResultNeedsSearch' not in code and 'p011SubmitFeedback' not in code: form_reads.append(code)
            return original(code, args)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda n: clock.__setitem__(0, clock[0] + n / 1000))
        result = worker.submit(payload, context=None, before_final=lambda: events.append(('journal',)))
        assert result.state == 'platform_scheduled'
        assert clock[0] < 30 and len(baseline_reads) == 5
        assert 'baseline' not in outer_phases
        assert 'prepare' in outer_phases and 'result' in outer_phases
        assert len(form_reads) == len(final_reads) == 1
        assert legacy.READ_IDENTITY in form_reads[0] and legacy.READ_IDENTITY in final_reads[0]
        assert [event[0] for event in events if event[0] in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
    finally:
        worker.close()


@pytest.mark.parametrize(('identity_patch', 'ready'), [
    ({'status': 8}, True), ({'profile_status': 8}, False), ({'after_status': 8}, True),
    ({'uid': '222', 'uid_after': '222'}, True), ({'uid_after': '222'}, False),
    ({'nickname': ''}, True), (None, True), ('malformed', False),
])
def test_baseline_checks_its_full_same_read_identity_before_using_readiness_or_matches(
        browser, db_engine, app_paths, monkeypatch, identity_patch, ready):
    from yijianjifa.webui import chromium_publish as legacy
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        page = pages[0]; original = page.evaluate; reads = []
        invalid = {**identity, **identity_patch} if type(identity_patch) is dict else identity_patch
        def evaluate(code, args=None):
            if 'p011ResultNeedsSearch' in code:
                reads.append(code)
                return {'identity': invalid, 'path': '/creator-micro/content/manage', 'ready': ready,
                        'matches': [], 'failure_code': 'must_not_precede_identity_check'}
            return original(code, args)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        result = worker.submit(payload, context=None, before_final=lambda: pytest.fail('invalid identity cannot submit'))
        assert result.state == 'pre_submit_failed' and result.error_code == 'account_mismatch'
        assert len(reads) == 1
        assert not any(event[0] in {'upload', 'journal', 'final'} for event in events)
    finally:
        worker.close()


@pytest.mark.parametrize(('phase', 'final', 'query'), [
    ('prepare', False, False), ('result', True, False), ('result', True, True),
    ('baseline', False, True), ('baseline', True, False),
])
def test_outer_identity_still_precedes_upload_and_every_query_or_postfinal_read(
        browser, db_engine, app_paths, monkeypatch, phase, final, query):
    from yijianjifa.webui import chromium_publish as legacy
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        session = worker._session(payload); session.phase = phase; session.final = final
        page = pages[0]; original = page.evaluate; outer = []
        def evaluate(code, args=None):
            if code == legacy.CHALLENGE: return original(code, args)
            if 'return identity;' in code:
                outer.append(code)
                return {**identity, 'profile_status': 8}
            pytest.fail('invalid outer identity must stop before form/result operations')
        monkeypatch.setattr(page, 'evaluate', evaluate)
        result = worker._advance(session, before_final=lambda: pytest.fail('cannot submit'), query=query)
        assert result.state == ('result_unknown' if final else 'pre_submit_failed')
        assert result.error_code == 'account_mismatch' and len(outer) == 1
        assert not any(event[0] in {'upload', 'journal', 'final'} for event in events)
    finally:
        worker.close()


@pytest.fixture(scope='module')
def local_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page()
        # Every browser request is fulfilled locally; no platform is contacted.
        page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<html><body></body></html>'))
        page.goto('http://127.0.0.1:31415/creator-micro/content/manage')
        yield page
        browser.close()


@pytest.mark.parametrize(('controls', 'loading', 'count', 'ready'), [
    ('', False, 0, False),
    ('<input placeholder="其他搜索" value="合成标题">', False, 0, False),
    ('<input placeholder="搜索作品" value="合成标题">', False, 1, True),
    ('<input placeholder="搜索作品" value="合成标题"><input placeholder="搜索作品" value="合成标题">', False, 2, False),
    ('<input placeholder="搜索作品" value="合成标题">', True, 1, False),
])
def test_real_local_dom_result_gates_are_finite_and_do_not_claim_readiness(local_page, controls, loading, count, ready):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    local_page.set_content('<html><body>' + controls + ('<p>加载中，请稍后</p>' if loading else '') + '</body></html>')
    local_page.evaluate('window.p011ResultWait=Date.now()-4000;window.p011ResultNeedsSearch=false')
    # Only the three HTTP identity reads are replaced with a local fixed value.
    code = module.READ_RESULT_WITH_DIAGNOSTICS.replace(legacy.READ_IDENTITY,
        'const identity={uid:"111",uid_after:"111",status:0,after_status:0,profile_status:0,nickname:"合成账号"};')
    result = legacy.evaluate_script(local_page, code, {'expected_uid': '111', 'title_text': '合成标题', 'body_text': ''})
    assert result['ready'] is ready
    assert result['diagnostic'] == {'search_count': count, 'search_matches': count == 1, 'loading': loading,
                                    **({'text_match_count': 0} if ready else {})}
