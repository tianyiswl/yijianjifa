"""验证发布读取诊断的证据边界，异常原文绝不进入可复制日志。"""
import importlib
import json

import pytest


def read_api():
    return importlib.import_module('yijianjifa.features.publish.read_diagnostics')


def valid_details():
    return {'error_kind': 'http_error', 'endpoint': 'profile_user_info',
            'request_index': 2, 'read_operation': 'fetch', 'request_elapsed_ms': 328,
            'http_status': 429, 'content_kind': 'json'}


def test_identity_failure_keeps_the_observed_status_without_retaining_the_caller_mapping():
    api = read_api()
    original = valid_details()
    result = api.identity_failure_details(original)
    assert result == {'error_kind': 'http_error', 'endpoint': 'profile_user_info',
                      'request_index': 2, 'read_operation': 'fetch', 'request_elapsed_ms': 328,
                      'http_status': 429, 'content_kind': 'json'}
    assert result is not original
    original['http_status'] = 403
    assert result['http_status'] == 429


@pytest.mark.parametrize(('endpoint', 'index'), [
    ('pc_user_info_before', 1), ('profile_user_info', 2), ('pc_user_info_after', 3),
])
def test_each_identity_request_has_an_unambiguous_endpoint_and_ordinal(endpoint, index):
    details = {'error_kind': 'fetch_error', 'endpoint': endpoint, 'request_index': index,
               'read_operation': 'fetch', 'request_elapsed_ms': 0}
    assert read_api().identity_failure_details(details) == details


@pytest.mark.parametrize(('status', 'elapsed', 'content'), [
    (100, 0, 'missing'), (200, 30000, 'html'), (599, 1, 'other'),
])
def test_response_evidence_accepts_the_finite_boundary_values(status, elapsed, content):
    details = valid_details()
    details.update(http_status=status, request_elapsed_ms=elapsed, content_kind=content,
                   error_kind='json_error', read_operation='json')
    assert read_api().identity_failure_details(details) == details


@pytest.mark.parametrize('kind', [
    'http_error', 'fetch_error', 'json_error', 'body_read_error', 'timeout',
    'page_context_unavailable', 'browser_unavailable', 'javascript_error', 'unknown',
])
def test_browser_failure_accepts_only_the_agreed_finite_error_vocabulary(kind):
    details = valid_details()
    details['error_kind'] = kind
    assert read_api().identity_failure_details(details)['error_kind'] == kind


@pytest.mark.parametrize('field', [
    'error_kind', 'endpoint', 'request_index', 'read_operation', 'request_elapsed_ms',
])
def test_incomplete_browser_evidence_is_rejected_as_a_whole(field):
    details = valid_details()
    del details[field]
    assert read_api().identity_failure_details(details) is None


@pytest.mark.parametrize(('field', 'value'), [
    ('error_kind', 'http_429'), ('error_kind', 'SECRET'), ('error_kind', True),
    ('endpoint', '/aweme/v1/creator/user/info/?token=SECRET'), ('endpoint', 'SECRET'),
    ('request_index', True), ('request_index', 0), ('request_index', 4), ('request_index', 2.0),
    ('request_index', 1), ('read_operation', 'response SECRET'), ('read_operation', False),
    ('request_elapsed_ms', True), ('request_elapsed_ms', -1), ('request_elapsed_ms', 30001),
    ('request_elapsed_ms', float('inf')), ('request_elapsed_ms', 1.5),
    ('http_status', True), ('http_status', 99), ('http_status', 600), ('http_status', 429.0),
    ('http_status', '429'), ('content_kind', 'application/json; token=SECRET'),
    ('content_kind', None), ('content_kind', ['json']),
    ('message', 'SECRET'), ('cookies', 'SECRET'), ('source_module', 'SECRET'),
])
def test_malformed_or_private_browser_values_never_partially_enter_the_log(field, value):
    details = valid_details()
    details[field] = value
    assert read_api().identity_failure_details(details) is None


@pytest.mark.parametrize('value', [None, [], 'SECRET', True, 1])
def test_non_mapping_browser_values_are_rejected(value):
    assert read_api().identity_failure_details(value) is None


def test_mapping_subclasses_are_not_allowed_to_execute_custom_lookup_code():
    class UntrustedDict(dict):
        def get(self, *args):
            pytest.fail('不应读取自定义映射的属性')
    assert read_api().identity_failure_details(UntrustedDict(valid_details())) is None


def test_safe_exception_preserves_only_validated_evidence_and_owns_its_copy():
    api = read_api()
    original = valid_details()
    error = api.IdentityReadFailure(original)
    original['http_status'] = 403
    assert api.error_details(error) == {'error_kind': 'http_error', 'endpoint': 'profile_user_info',
                                      'request_index': 2, 'read_operation': 'fetch',
                                      'request_elapsed_ms': 328, 'http_status': 429,
                                      'content_kind': 'json'}
    exported = error.details
    exported['http_status'] = True
    assert api.error_details(error)['http_status'] == 429
    assert '429' not in str(error) and 'profile_user_info' not in str(error)


def test_safe_exception_refuses_unknown_fields_without_copying_their_text_into_its_message():
    api = read_api()
    with pytest.raises(ValueError) as caught:
        api.IdentityReadFailure({**valid_details(), 'message': 'SECRET'})
    assert 'SECRET' not in str(caught.value)


def test_identity_timeout_preserves_timeout_semantics_and_the_specific_read_evidence():
    api = read_api()
    details = {'error_kind': 'timeout', 'endpoint': 'pc_user_info_after',
               'request_index': 3, 'read_operation': 'json', 'request_elapsed_ms': 30000,
               'http_status': 200, 'content_kind': 'json'}
    error = api.IdentityReadTimeout(details)
    assert isinstance(error, TimeoutError)
    assert api.error_details(error) == details
    details['http_status'] = True
    assert api.error_details(error)['http_status'] == 200


def test_identity_timeout_cannot_relabel_a_non_timeout_response_as_timeout():
    api = read_api()
    with pytest.raises(ValueError):
        api.IdentityReadTimeout(valid_details())


@pytest.mark.parametrize(('error_name', 'message', 'want'), [
    ('timeout', 'Cookie=SECRET /private/profile', 'timeout'),
    ('playwright_timeout', 'Cookie=SECRET /private/profile', 'timeout'),
    ('playwright', 'Page.evaluate: Execution context was destroyed SECRET', 'page_context_unavailable'),
    ('playwright', 'Page.evaluate: Cannot find context with specified id SECRET', 'page_context_unavailable'),
    ('playwright', 'Target page, context or browser has been closed SECRET', 'page_context_unavailable'),
    ('playwright', 'Browser disconnected SECRET', 'browser_unavailable'),
    ('playwright', 'Page.evaluate: TypeError: Failed to fetch SECRET', 'fetch_error'),
    ('type', 'Failed to fetch SECRET', 'fetch_error'),
    ('playwright', 'Page.evaluate: SyntaxError: Unexpected token SECRET', 'javascript_error'),
    ('runtime', 'SECRET', 'unknown'),
    ('value', 'token SECRET userinfo', 'unknown'),
])
def test_raw_errors_become_fixed_categories_without_retaining_any_message(error_name, message, want):
    from playwright.sync_api import Error, TimeoutError as PlaywrightTimeoutError
    classes = {'timeout': TimeoutError, 'playwright_timeout': PlaywrightTimeoutError,
               'playwright': Error, 'type': TypeError, 'runtime': RuntimeError, 'value': ValueError}
    details = read_api().error_details(classes[error_name](message))
    assert details == {'error_kind': want}
    assert 'SECRET' not in json.dumps(details)


def test_application_trace_reports_code_location_without_path_arguments_or_locals():
    api = read_api()
    namespace = {'__name__': 'yijianjifa.synthetic_reader', 'cookie': 'SECRET'}
    exec(compile('def read_account():\n    raise RuntimeError(cookie)\n',
                 'C:/SECRET/private-profile/account.py', 'exec'), namespace)
    try:
        namespace['read_account']()
    except RuntimeError as error:
        details = api.error_details(error)
    assert details == {'error_kind': 'unknown', 'source_module': 'synthetic_reader',
                       'source_function': 'read_account', 'source_line': 2}
    assert 'SECRET' not in json.dumps(details) and '/' not in json.dumps(details)


def test_non_application_traceback_is_not_exported():
    try:
        raise RuntimeError('SECRET')
    except RuntimeError as error:
        assert read_api().error_details(error) == {'error_kind': 'unknown'}
