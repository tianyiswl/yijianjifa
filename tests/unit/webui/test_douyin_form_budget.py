"""Actual local DOM preparation under controlled, evidence-shaped IO costs."""
import json
from types import SimpleNamespace

import pytest

from test_chromium_multi_publish import browser, adapter
from test_douyin_publish_diagnostics import saved_events
from douyin_form_dom_fixture import install_form


@pytest.fixture(scope='module')
def form_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        chromium = driver.chromium.launch(headless=True)
        context = chromium.new_context(service_workers='block')
        context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<html><body></body></html>'))
        page = context.new_page()
        page.goto('http://127.0.0.1:31415/creator-micro/content/post/video')
        try: yield page
        finally: context.close(); chromium.close()


@pytest.mark.parametrize(('body_length', 'topics', 'navigation_cost', 'first_identity_cost'), [
    (19, ['日常记录', '镜头观察'], 3.735, 5.620),
    (14, ['现场声音', '日常片段'], 1.441, 3.483),
])
def test_two_topic_real_form_completes_with_progress_instead_of_exhausting_the_shared_budget(
        form_page, browser, db_engine, app_paths, monkeypatch, body_length, topics,
        navigation_cost, first_identity_cost):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    payload, pages, events, identity = browser
    payload['fields'].update(title='合成标题', body='文' * body_length, tags=' '.join('#' + name for name in topics))
    install_form(form_page, topics=topics)
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        assert worker.step_timeout == 30
        page = pages[0]; original = page.evaluate; original_goto = page.goto; original_locator = page.locator
        session = worker._session(payload)
        clock = [0.0]; baseline = []; outer = []; stages = []
        def goto(url, **kwargs):
            if url.endswith(legacy.UPLOAD_PATH): clock[0] += navigation_cost
            return original_goto(url, **kwargs)
        def locator(selector):
            item = original_locator(selector); select = item.set_input_files
            def set_input_files(path):
                select(path)
                page.url = 'https://' + legacy.HOST + legacy.PUBLISH_PATH
            item.set_input_files = set_input_files
            return item
        def evaluate(code, args=None):
            if code == legacy.CHALLENGE: return original(code, args)
            if 'return identity;' in code:
                clock[0] += first_identity_cost if not outer else 1.0
                outer.append(session.phase)
                return original(code, args)
            if 'p011ResultNeedsSearch' in code and session.phase == 'baseline':
                baseline.append(clock[0]); clock[0] += .95
                return {'identity': dict(identity), 'path': '/creator-micro/content/manage',
                        'ready': len(baseline) >= 5, 'matches': []}
            if 'p011FinalClicked=true' in code or 'p011ResultNeedsSearch' in code:
                clock[0] += .8
                result = original(code, args)
                if 'p011ResultNeedsSearch' in code: result['query'] = args['title_text']
                return result
            clock[0] += 1.4
            page.form = args
            # Only HTTP identity IO is replaced. All field edits, cursor moves,
            # candidate clicks and exact readback execute the production JS.
            local_code = code.replace(legacy.READ_IDENTITY, 'const identity=' + json.dumps(identity) + ';')
            raw = form_page.evaluate(local_code, args)
            stages.append(raw.get('stage'))
            return raw
        monkeypatch.setattr(page, 'goto', goto)
        monkeypatch.setattr(page, 'locator', locator)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda n: clock.__setitem__(0, clock[0] + n / 1000))
        def before_final():
            assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
            events.append(('journal',))
        result = worker.submit(payload, context=None, before_final=before_final)
        assert result.state == 'platform_scheduled', {'result': result, 'stages': stages, 'elapsed': clock[0]}
        assert len(stages) == 12 and stages[-1] == '表单已核对，未发布'
        assert clock[0] > 30
        assert [event[0] for event in events if event[0] in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
        assert form_page.locator('[data-mention="#"]').all_text_contents() == ['#' + name for name in topics]
        assert form_page.locator('input[placeholder="日期和时间"]').input_value() == page.form['scheduled_time_text']
    finally:
        worker.close()


@pytest.mark.parametrize(('stage', 'expected'), [
    ('填写标题', 'title'), ('等待完全匹配的话题：PRIVATE_TOPIC', 'topic_candidate'),
    ('填写定时时间', 'schedule_time'), ('等待上传完成', 'upload_wait'),
    ('PRIVATE_COOKIE_UID_PATH', 'unknown'), (['PRIVATE_VALUE'], 'unknown'),
])
def test_form_readback_records_unknown_upload_and_only_code_owned_stage(
        browser, db_engine, app_paths, stage, expected):
    payload, _, _, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        session = worker._session(payload); session.phase = 'prepare'
        worker._readback(session, {'ready': False, 'path': '/creator-micro/content/post/video', 'stage': stage,
            'title_count': 1, 'editor_count': 1})
        checks = saved_events(db_engine)[-1]['checks']
        assert checks['form_gate'] == expected and checks['upload_state'] == 'unknown'
        assert 'upload_complete' not in checks
        assert 'PRIVATE' not in json.dumps(checks)
    finally:
        worker.close()


def test_real_form_diagnostics_report_native_field_progress_without_exporting_text(
        form_page, browser, db_engine, app_paths):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    payload, _, _, identity = browser
    payload['fields'].update(body='PRIVATE_BODY', tags='#PRIVATE_TOPIC #SECOND_TOPIC')
    install_form(form_page, topics=['PRIVATE_TOPIC', 'SECOND_TOPIC'])
    worker = adapter(db_engine, app_paths, payload)
    try:
        form = worker._form(payload)
        code = module.PREPARE_FORM_WITH_DIAGNOSTICS.replace(legacy.READ_IDENTITY, 'const identity=' + json.dumps(identity) + ';')
        first = legacy.evaluate_script(form_page, code, form)
        assert first['stage'] == '填写标题'
        assert first['diagnostic'] == {'title_matches': True, 'body_matches': False,
            'topics_confirmed': 0, 'topic_order_matches': True,
            'schedule_mode_matches': False, 'schedule_time_matches': False}
        session = worker._session(payload); session.phase = 'prepare'
        worker._readback(session, first)
        checks = saved_events(db_engine)[-1]['checks']
        assert checks['title_matches'] is True and checks['topics_confirmed'] == 0
        assert checks['form_gate'] == 'title'
        assert 'PRIVATE' not in json.dumps(checks)
    finally:
        worker.close()


def test_normal_upload_can_remain_pending_beyond_thirty_seconds_without_reselecting_file(
        browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        session = worker._session(payload); session.phase = 'prepare'
        page = pages[0]; page.url = 'https://' + legacy.HOST + legacy.UPLOAD_PATH
        original = page.evaluate; clock = [0.0]; reads = []
        def evaluate(code, args=None):
            if code != legacy.CHALLENGE and 'return identity;' not in code and 'p011FinalClicked=true' not in code and 'p011ResultNeedsSearch' not in code:
                reads.append(code); clock[0] += 5
                if len(reads) <= 8:
                    page.url = 'https://' + legacy.HOST + legacy.PUBLISH_PATH
                    return {'identity': identity, 'path': legacy.PUBLISH_PATH, 'ready': False,
                            'upload_complete': False, 'stage': '等待上传完成'}
            return original(code, args)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda n: clock.__setitem__(0, clock[0] + n / 1000))
        result = worker.submit(payload, context=None, before_final=lambda: events.append(('journal',)))
        assert result.state == 'platform_scheduled' and 30 < clock[0] < 180
        assert [event[0] for event in events if event[0] in {'upload', 'journal', 'final'}] == ['upload', 'journal', 'final']
    finally:
        worker.close()


@pytest.mark.parametrize('alternating', [False, True])
def test_repeated_or_oscillating_form_gates_never_renew_the_fixed_preparation_deadline(
        browser, db_engine, app_paths, monkeypatch, alternating):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        assert worker.preparation_timeout == 180
        session = worker._session(payload); session.phase = 'prepare'
        page = pages[0]; page.url = 'https://' + legacy.HOST + legacy.UPLOAD_PATH
        original = page.evaluate; clock = [0.0]; reads = []
        def evaluate(code, args=None):
            if code == legacy.CHALLENGE or 'return identity;' in code: return original(code, args)
            reads.append(code); clock[0] += 29
            page.url = 'https://' + legacy.HOST + legacy.PUBLISH_PATH
            return {'identity': identity, 'path': legacy.PUBLISH_PATH, 'ready': False,
                    'stage': '填写标题' if alternating and len(reads) % 2 else '未能确认声明为空',
                    'diagnostic': {'title_matches': bool(len(reads) % 2), 'topics_confirmed': len(reads)}}
        monkeypatch.setattr(page, 'evaluate', evaluate)
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda n: clock.__setitem__(0, clock[0] + n / 1000))
        result = worker.submit(payload, context=None, before_final=lambda: pytest.fail('stuck form cannot submit'))
        assert result.state == 'pre_submit_failed' and result.error_stage == 'form_preparation'
        assert 180 <= clock[0] < 210 and len(reads) == 7
        assert sum(event[0] == 'upload' for event in events) == 1
        assert not any(event[0] in {'journal', 'final'} for event in events)
    finally:
        worker.close()


def test_a_form_becoming_ready_after_preparation_deadline_gets_no_final_authority(
        browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    payload, pages, events, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        session = worker._session(payload); session.phase = 'prepare'
        page = pages[0]; page.url = 'https://' + legacy.HOST + legacy.UPLOAD_PATH
        original = page.evaluate; clock = [0.0]
        def evaluate(code, args=None):
            result = original(code, args)
            if code != legacy.CHALLENGE and 'return identity;' not in code: clock[0] += 181
            return result
        monkeypatch.setattr(page, 'evaluate', evaluate)
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        result = worker.submit(payload, context=None, before_final=lambda: pytest.fail('expired budget cannot authorize final'))
        assert result.state == 'pre_submit_failed' and result.error_category == 'timeout'
        assert not any(event[0] in {'journal', 'final'} for event in events)
    finally:
        worker.close()


def test_a_postfinal_query_keeps_its_short_budget_and_never_enters_preparation(
        browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    payload, pages, events, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        session = worker._session(payload); session.phase = 'result'; session.final = True
        page = pages[0]; original = page.evaluate; clock = [0.0]
        def evaluate(code, args=None):
            if 'p011ResultNeedsSearch' in code:
                clock[0] += .1
                return {'identity': identity, 'path': '/creator-micro/content/manage', 'ready': False}
            return original(code, args)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda n: clock.__setitem__(0, clock[0] + n / 1000))
        worker.step_timeout = .2
        result = worker.reconcile(payload, context=None)
        assert result.state == 'result_unknown' and result.error_code == 'timeout' and clock[0] < 1
        assert not any(event[0] in {'upload', 'journal', 'final'} for event in events)
    finally:
        worker.close()
