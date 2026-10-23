"""Real browser identity JS with a local clock/fetch boundary, never platform IO."""
import json

import pytest

from test_chromium_multi_publish import browser, adapter
from test_douyin_image_publish import image_browser
from test_douyin_publish_diagnostics import saved_events


@pytest.fixture(scope='module')
def identity_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        chromium = driver.chromium.launch(headless=True)
        context = chromium.new_context(service_workers='block')
        context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<html><body></body></html>'))
        page = context.new_page()
        page.goto('https://creator.douyin.com/creator-micro/content/manage')
        try: yield page
        finally: context.close(); chromium.close()


def controlled_fetch(page, steps):
    page.evaluate('''steps => {
      window.p011Clock=0;window.p011Calls=[];
      Object.defineProperty(performance,'now',{configurable:true,value:()=>window.p011Clock});
      const signals=new WeakMap();
      AbortSignal.timeout=ms=>{const c=new AbortController();signals.set(c.signal,{c,ms});return c.signal;};
      const values=[{uid:'111',status_code:0},{status_code:0,user_profile:{nick_name:'合成抖音'}},{uid:'111',status_code:0}];
      window.fetch=async(path,options)=>{
        const i=window.p011Calls.length,step=steps[i];
        if(!step)throw Error('unexpected extra identity request');
        const signal=signals.get(options.signal),elapsed=step.fetch_ms||0;
        window.p011Calls.push({path,budget:signal.ms});window.p011Clock+=elapsed;
        const timeout=()=>{signal.c.abort();throw new DOMException('local fake timeout','TimeoutError');};
        if(elapsed>=signal.ms)timeout();
        return {ok:step.ok!==false,json:async()=>{
          const json=step.json_ms||0;window.p011Clock+=json;
          if(elapsed+json>=signal.ms&&!step.ignore_json_abort)timeout();
          if(step.json_error)throw new SyntaxError('PRIVATE_LOCAL_JSON');
          return step.body||values[i];
        }};
      };
    }''', steps)


def connect_identity(page, real_page, monkeypatch):
    original = page.evaluate; scripts = []
    def evaluate(code, args=None):
        if code.endswith('return identity;}'):
            scripts.append(code)
            return real_page.evaluate(code, args)
        return original(code, args)
    monkeypatch.setattr(page, 'evaluate', evaluate)
    return scripts


def test_first_get_delayed_ten_seconds_is_valid_with_three_reads_and_fixed_remaining_budget(
        identity_page, browser, db_engine, app_paths, monkeypatch):
    payload, pages, events, _ = browser; worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        controlled_fetch(identity_page, [{'fetch_ms': 10738}, {'fetch_ms': 9000}, {'fetch_ms': 9000}])
        connect_identity(pages[0], identity_page, monkeypatch)
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        calls = identity_page.evaluate('window.p011Calls')
        assert [call['budget'] for call in calls] == [20000, 19262, 10262]
        assert [call['path'] for call in calls] == ['/aweme/v1/creator/pc/user/info/',
            '/aweme/v1/creator/user/info/', '/aweme/v1/creator/pc/user/info/']
        assert not any(event[0] in {'upload', 'final'} for event in events)
    finally: worker.close()


@pytest.mark.parametrize(('steps', 'budgets'), [
    ([{'fetch_ms': 20001}, {}, {}], [20000]),
    ([{'fetch_ms': 100, 'json_ms': 20000, 'ignore_json_abort': True}, {}, {}], [20000]),
    ([{'fetch_ms': 11000}, {'fetch_ms': 11000}, {'fetch_ms': 9000}], [20000, 19000, 8000]),
    ([{'fetch_ms': 100, 'json_ms': 29900, 'ignore_json_abort': True}, {}, {}], [20000]),
    ([{'fetch_ms': 10000}, {'fetch_ms': 10000}, {'fetch_ms': 9000, 'json_ms': 1000, 'ignore_json_abort': True}],
     [20000, 20000, 10000]),
])
def test_single_or_total_deadline_never_authorizes_a_late_identity_or_starts_an_extra_get(
        identity_page, browser, db_engine, app_paths, monkeypatch, steps, budgets):
    payload, pages, events, _ = browser; worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        controlled_fetch(identity_page, steps)
        connect_identity(pages[0], identity_page, monkeypatch)
        with pytest.raises(TimeoutError): worker.validate_target(payload, payload, payload['execution_mode'])
        assert [call['budget'] for call in identity_page.evaluate('window.p011Calls')] == budgets
        assert worker.failure_diagnostic(TimeoutError()) == {'error_stage': 'account_observation', 'error_category': 'timeout'}
        assert not any(event[0] in {'upload', 'journal', 'final'} for event in events)
        failures = [item for item in saved_events(db_engine) if item['checks'].get('state') == 'failed']
        assert failures[-1]['checks']['error_category'] == 'timeout'
        assert 'local fake timeout' not in json.dumps(failures)
    finally: worker.close()


@pytest.mark.parametrize(('first', 'profile', 'last'), [
    ({'uid': '222', 'status_code': 0}, None, None),
    (None, None, {'uid': '222', 'status_code': 0}),
    (None, {'status_code': 8, 'user_profile': {'nick_name': '合成抖音'}}, None),
])
def test_new_budget_preserves_complete_identity_status_and_both_uid_checks(
        identity_page, browser, db_engine, app_paths, monkeypatch, first, profile, last):
    payload, pages, events, _ = browser; worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        controlled_fetch(identity_page, [{'body': first}, {'body': profile}, {'body': last}])
        connect_identity(pages[0], identity_page, monkeypatch)
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized is False
        assert len(identity_page.evaluate('window.p011Calls')) == 3
        assert not any(event[0] in {'upload', 'final'} for event in events)
    finally: worker.close()


@pytest.mark.parametrize(('final', 'query'), [(True, False), (True, True), (False, True)])
def test_final_and_explicit_query_still_use_the_exact_legacy_identity_script(
        browser, db_engine, app_paths, monkeypatch, final, query):
    from yijianjifa.webui import chromium_publish as legacy
    payload, pages, _, identity = browser; worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        session = worker._session(payload); session.final = final; scripts = []
        def evaluate_script(page, code, arguments): scripts.append(code); return identity
        monkeypatch.setattr(legacy, 'evaluate_script', evaluate_script)
        assert worker._read_identity(session, query=query) == identity
        assert scripts == [legacy.READ_IDENTITY + 'return identity;']
    finally: worker.close()


@pytest.mark.parametrize('first', [{'ok': False}, {'json_error': True}])
def test_non_timeout_http_or_json_failure_is_not_relabelled_as_timeout_or_expired(
        identity_page, browser, db_engine, app_paths, monkeypatch, first):
    payload, pages, events, _ = browser; worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        controlled_fetch(identity_page, [first, {}, {}])
        connect_identity(pages[0], identity_page, monkeypatch)
        with pytest.raises(Exception) as failure: worker.validate_target(payload, payload, payload['execution_mode'])
        assert not isinstance(failure.value, TimeoutError)
        assert worker.failure_diagnostic(failure.value) == {'error_stage': 'account_observation', 'error_category': 'execution_failed'}
        assert len(identity_page.evaluate('window.p011Calls')) == 1
        assert not any(event[0] in {'upload', 'final'} for event in events)
        assert 'PRIVATE_LOCAL_JSON' not in json.dumps(saved_events(db_engine))
    finally: worker.close()


def test_image_post_identity_does_not_call_the_new_video_identity_reader(image_browser, monkeypatch):
    from yijianjifa.webui import chromium_publish as legacy
    worker, payload, _, events = image_browser
    original = legacy.evaluate_script; scripts = []
    def evaluate(page, code, arguments):
        if 'return identity;' in code:
            scripts.append(code)
            assert code == legacy.READ_IDENTITY + 'return identity;'
        return original(page, code, arguments)
    monkeypatch.setattr(legacy, 'evaluate_script', evaluate)
    monkeypatch.setattr(worker, '_read_identity', lambda *a, **k: pytest.fail('image must keep its own identity chain'))
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        result = worker.submit(payload, context=None, before_final=lambda: events.append(('journal', None)))
        assert result.state == 'platform_scheduled' and len(scripts) >= 2
    finally: worker.close()
