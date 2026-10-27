"""Account contention is not a failed login or proof of an expired session."""
import json
import threading

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.publish.account_wait import AccountBusy
from yijianjifa.webui import chromium_publish


def dispatch(api, operation, params):
    return api.dispatch(json.dumps(dict(v=1, request_id='fixture-login-busy',
        operation=operation, params=params)), trusted_origin=True)


def test_same_account_original_page_contention_has_a_specific_safe_failure(db_engine, app_paths, monkeypatch):
    account = AccountService(db_engine).create('douyin', '123', '合成账号')
    AccountService(db_engine).set_login_state(account.id, 'valid')
    monkeypatch.setattr(chromium_publish, 'browser_options', lambda _mode: {})
    launches = []
    def launch():
        launches.append(True)
        pytest.fail('a busy original profile must never open another browser')
    monkeypatch.setattr('playwright.sync_api.sync_playwright', launch)
    provider = chromium_publish.make_chromium_login(db_engine, app_paths)
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    lock = chromium_publish.account_lock(account.id)
    assert lock.acquire(False)
    try:
        initial = AccountService(db_engine).get(account.id)
        result = dispatch(api, 'account.login.start', {'account_id': account.id})
        assert result['ok']
        api._login_runtime.thread.join(2)
        status = dispatch(api, 'account.login.status', {'id': result['data']['id']})['data']
        assert status['state'] == 'failed'
        assert status['code'] == 'login_account_busy'
        current = AccountService(db_engine).get(account.id)
        assert (current.login_state, current.session_ref) == (initial.login_state, initial.session_ref)
        assert lock.locked() and launches == []
    finally:
        lock.release()
        api.close(2)


def test_busy_exception_is_typed_and_keeps_original_lock(db_engine, app_paths, monkeypatch):
    account = AccountService(db_engine).create('douyin', '123', '合成账号')
    monkeypatch.setattr(chromium_publish, 'browser_options', lambda _mode: {})
    provider = chromium_publish.make_chromium_login(db_engine, app_paths)
    lock = chromium_publish.account_lock(account.id)
    assert lock.acquire(False)
    try:
        with pytest.raises(AccountBusy):
            provider(account.id, threading.Event())
        assert lock.locked()
    finally:
        lock.release()


def test_untyped_provider_exception_cannot_impersonate_account_contention(db_engine, app_paths):
    def provider(_account_id, _cancel):
        raise ValueError('account busy Cookie=private-token')
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    try:
        result = dispatch(api, 'account.login.start', {'account_id': None})
        api._login_runtime.thread.join(2)
        status = dispatch(api, 'account.login.status', {'id': result['data']['id']})['data']
        assert status['state'] == 'failed' and status['code'] == 'login_provider_failed'
        assert 'private-token' not in repr(status)
    finally:
        api.close(2)


@pytest.mark.parametrize('platform', ['channels', 'bilibili', 'kuaishou'])
@pytest.mark.parametrize('operation', ['account.login.start', 'account.backend.start'])
def test_creator_login_and_backend_share_typed_contention_without_opening_a_second_browser(
        db_engine, app_paths, monkeypatch, platform, operation):
    from yijianjifa.webui.creator_providers import make_creator_login_providers
    account = AccountService(db_engine).create(platform, '123', '合成账号')
    AccountService(db_engine).set_login_state(account.id, 'valid')
    monkeypatch.setattr(chromium_publish, 'browser_options', lambda _mode: {})
    monkeypatch.setattr('playwright.sync_api.sync_playwright',
                        lambda: pytest.fail('busy profile must not create a second browser'))
    providers = make_creator_login_providers(db_engine, app_paths)
    api = DesktopApi(db_engine, app_paths, login_providers={platform: providers[platform]})
    lock = chromium_publish.account_lock(account.id)
    assert lock.acquire(False)
    try:
        before = AccountService(db_engine).get(account.id)
        started = dispatch(api, operation, {'account_id': account.id})
        assert started['ok']
        api._login_runtime.thread.join(2)
        status = dispatch(api, 'account.login.status', {'id': started['data']['id']})['data']
        assert status['state'] == 'failed' and status['code'] == 'login_account_busy'
        after = AccountService(db_engine).get(account.id)
        assert (after.login_state, after.session_ref) == (before.login_state, before.session_ref)
        assert lock.locked()
    finally:
        lock.release()
        api.close(2)
