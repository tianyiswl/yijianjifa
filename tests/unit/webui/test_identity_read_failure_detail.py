"""用完全拦截的真实浏览器验证账号读取诊断，不访问真实平台。"""
import json

import pytest
from playwright.sync_api import Error, sync_playwright

from yijianjifa.webui.chromium_multi_publish import NORMAL_READ_IDENTITY
from yijianjifa.webui.chromium_publish import evaluate_script


@pytest.fixture(scope='module')
def isolated_browser():
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        yield browser
        browser.close()


def run_identity(browser, *, failed_index=None, status=200, body=None, content_type='application/json', abort=False):
    calls = []
    with browser.new_context(service_workers='block') as context:
        def serve(route):
            path = route.request.url.split('?', 1)[0]
            if '/aweme/v1/creator/' not in path:
                route.fulfill(status=200, content_type='text/html', body='<html><body>本地合成页面</body></html>')
                return
            index = len(calls) + 1
            calls.append(path)
            if index == failed_index:
                if abort:
                    route.abort('connectionreset')
                else:
                    route.fulfill(status=status, content_type=content_type, body=body or 'SECRETSONLYALPHA')
                return
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                'uid': '111', 'status_code': 0, 'user_profile': {'nick_name': '合成账号'}}))
        context.route('**/*', serve)
        page = context.new_page()
        page.goto('https://creator.douyin.com/creator-micro/content/manage')
        try:
            result = evaluate_script(page, NORMAL_READ_IDENTITY + 'return identity;', {})
        except Error:
            pytest.fail('账号读取只抛出通用Error，缺少安全的结构化失败原因。')
        return result, calls


@pytest.mark.parametrize('request_index', [1, 2, 3])
@pytest.mark.parametrize('status', [401, 403, 429, 503])
def test_real_http_failure_reports_exact_request_and_status_without_response_body(isolated_browser, request_index, status):
    result, calls = run_identity(isolated_browser, failed_index=request_index, status=status, content_type='text/html')
    assert set(result) == {'p011_identity_failure'}
    detail = result['p011_identity_failure']
    assert detail['error_kind'] == 'http_error'
    assert detail['request_index'] == request_index and detail['http_status'] == status
    assert detail['endpoint'] == ['pc_user_info_before', 'profile_user_info', 'pc_user_info_after'][request_index - 1]
    assert detail['content_kind'] == 'html' and detail['read_operation'] == 'fetch'
    assert type(detail['request_elapsed_ms']) is int and 0 <= detail['request_elapsed_ms'] <= 30000
    assert len(calls) == request_index
    assert all(secret not in json.dumps(result) for secret in ['SECRETSONLYALPHA', 'uid', 'nick_name', 'creator.douyin.com'])
    assert '111' not in [value for value in detail.values() if type(value) is str]


@pytest.mark.parametrize('content_type,expected', [('application/json', 'json'), ('text/html', 'html')])
def test_real_invalid_json_is_distinct_from_http_failure(isolated_browser, content_type, expected):
    result, calls = run_identity(isolated_browser, failed_index=2, body='SECRETSONLYALPHA', content_type=content_type)
    detail = result['p011_identity_failure']
    assert detail['error_kind'] == 'json_error' and detail['http_status'] == 200
    assert detail['read_operation'] == 'json' and detail['request_index'] == 2
    assert detail['content_kind'] == expected and len(calls) == 2
    assert 'SECRETSONLYALPHA' not in json.dumps(result)


def test_real_failed_fetch_does_not_invent_http_status_or_retry(isolated_browser):
    result, calls = run_identity(isolated_browser, failed_index=3, abort=True)
    detail = result['p011_identity_failure']
    assert detail['error_kind'] == 'fetch_error' and detail['request_index'] == 3
    assert detail['read_operation'] == 'fetch' and 'http_status' not in detail
    assert len(calls) == 3


def test_success_keeps_three_identity_reads_and_original_return_shape(isolated_browser):
    result, calls = run_identity(isolated_browser)
    assert result == {'uid': '111', 'uid_after': '111', 'status': 0, 'profile_status': 0,
                      'after_status': 0, 'nickname': '合成账号'}
    assert len(calls) == 3
