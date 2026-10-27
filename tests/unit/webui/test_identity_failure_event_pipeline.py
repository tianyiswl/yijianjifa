"""真实适配器与SQLite事件链路：账号读取细节不得改变提交边界。"""
import json

import pytest
from playwright.sync_api import Error

from yijianjifa.features.publish.read_diagnostics import IdentityReadFailure
from test_chromium_multi_publish import browser, adapter
from test_douyin_publish_diagnostics import saved_events


@pytest.mark.parametrize('index,status', [(1, 403), (2, 429), (3, 503)])
def test_pre_submit_read_failure_persists_request_evidence_in_both_actual_failure_events(
        browser, db_engine, app_paths, monkeypatch, index, status):
    from yijianjifa.webui import chromium_publish as legacy
    payload, pages, operations, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    detail = {'error_kind': 'http_error', 'endpoint': ['pc_user_info_before', 'profile_user_info', 'pc_user_info_after'][index - 1],
              'request_index': index, 'read_operation': 'fetch', 'request_elapsed_ms': 328,
              'http_status': status, 'content_kind': 'html'}
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        session = worker._session(payload)
        session.phase = 'prepare'
        original = legacy.evaluate_script
        def read(page, code, arguments):
            if code.endswith('return identity;'):
                return {'p011_identity_failure': detail}
            return original(page, code, arguments)
        monkeypatch.setattr(legacy, 'evaluate_script', read)
        with pytest.raises(IdentityReadFailure):
            worker.submit(payload, context=None, before_final=lambda: pytest.fail('读取失败不能最终提交'))
        events = saved_events(db_engine)
        identity = [e['checks'] for e in events if e['step'] == 'douyin.identity' and e['checks'].get('state') == 'failed'][-1]
        execution = [e['checks'] for e in events if e['step'] == 'execution.error'][-1]
        for checks in [identity, execution]:
            assert all(checks[key] == value for key, value in detail.items())
            assert checks['final_started'] is False and checks['phase'] == 'prepare'
            assert checks['log_schema'] == 1
            assert checks['source_module'] == 'chromium_multi_publish'
            assert type(checks['source_line']) is int
        assert execution['stage'] == 'account_observation'
        assert worker.failure_diagnostic(IdentityReadFailure(detail)) == {
            'error_stage': 'account_observation', 'error_category': 'execution_failed'}
        assert not any(operation[0] in {'upload', 'journal', 'final'} for operation in operations)
    finally:
        worker.close()


def test_post_final_error_records_host_final_boundary_without_raw_exception_or_repeat_submit(
        browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_publish as legacy
    payload, _, operations, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        session = worker._session(payload)
        session.phase = 'result'
        session.final = True
        def failed(*args):
            raise Error('Execution context was destroyed. SECRETSONLYALPHA Cookie=SECRET https://private/?token=SECRET')
        monkeypatch.setattr(legacy, 'evaluate_script', failed)
        with pytest.raises(Error):
            worker.reconcile(payload, context=None)
        events = saved_events(db_engine)
        execution = [e['checks'] for e in events if e['step'] == 'execution.error'][-1]
        assert execution['final_started'] is True and execution['phase'] == 'result'
        assert execution['error_kind'] == 'page_context_unavailable'
        assert all(secret not in json.dumps(events) for secret in ['SECRET', 'private', 'Cookie', 'token='])
        assert not any(operation[0] in {'upload', 'journal', 'final'} for operation in operations)
    finally:
        worker.close()


def test_unknown_exception_class_name_is_not_copied_as_an_untrusted_letter_only_secret(
        browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_publish as legacy
    payload, _, operations, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    failure_type = type('SECRETSONLYALPHA', (Exception,), {})
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        worker._session(payload).phase = 'prepare'
        def failed(*args):
            raise failure_type('SECRETARGS Cookie=SECRET')
        monkeypatch.setattr(legacy, 'evaluate_script', failed)
        with pytest.raises(failure_type):
            worker.submit(payload, context=None, before_final=lambda: pytest.fail('未知错误不能提交'))
        events = saved_events(db_engine)
        execution = [e['checks'] for e in events if e['step'] == 'execution.error'][-1]
        assert execution['exception_type'] == 'UnknownError'
        assert execution['error_kind'] == 'unknown'
        assert 'SECRET' not in json.dumps(events)
        assert not any(operation[0] in {'upload', 'journal', 'final'} for operation in operations)
    finally:
        worker.close()


@pytest.mark.parametrize('bad', [
    {'http_status': True}, {'http_status': 999}, {'request_index': True},
    {'private': 'SECRETSONLYALPHA'}, {'error_kind': 'SECRETSONLYALPHA'},
])
def test_malformed_browser_failure_is_rejected_before_any_detail_reaches_actual_sqlite_events(
        browser, db_engine, app_paths, monkeypatch, bad):
    from yijianjifa.webui import chromium_publish as legacy
    payload, _, operations, _ = browser
    worker = adapter(db_engine, app_paths, payload)
    details = {'error_kind': 'http_error', 'endpoint': 'profile_user_info', 'request_index': 2,
               'read_operation': 'fetch', 'request_elapsed_ms': 328, 'http_status': 503, **bad}
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        worker._session(payload).phase = 'prepare'
        original = legacy.evaluate_script
        def invalid(page, code, arguments):
            return {'p011_identity_failure': details} if code.endswith('return identity;') else original(page, code, arguments)
        monkeypatch.setattr(legacy, 'evaluate_script', invalid)
        with pytest.raises(ValueError, match='invalid identity diagnostic'):
            worker.submit(payload, context=None, before_final=lambda: pytest.fail('无效诊断不能提交'))
        events = saved_events(db_engine)
        execution = [e['checks'] for e in events if e['step'] == 'execution.error'][-1]
        assert execution['error_kind'] == 'unknown'
        assert 'http_status' not in execution and 'request_index' not in execution
        assert execution['final_started'] is False and 'SECRET' not in json.dumps(events)
        assert not any(operation[0] in {'upload', 'journal', 'final'} for operation in operations)
    finally:
        worker.close()
