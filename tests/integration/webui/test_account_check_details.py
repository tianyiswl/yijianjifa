"""Account checks expose finite causes and progress without login or upload."""
import json
import threading
import time

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.login_runtime import LoginRuntime
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore
from test_session_check import check_fixture


def request(api, operation, params, rid='check-read'):
    result = api.dispatch(json.dumps(dict(v=1, request_id=rid, operation=operation, params=params)), trusted_origin=True)
    assert result['ok'], result
    return result['data']


def saved(engine, paths, platform='douyin'):
    service = AccountService(engine)
    account = service.create(platform, 'synthetic-' + platform, 'Synthetic')
    service.attach_session(account.id, SessionStore(paths.data_dir / 'profiles'), b'controlled archive')
    return service, account


def diagnostic(stage='identity_first'):
    return {'v': 1, 'stage': stage, 'last_stage': 'cleanup', 'elapsed_ms': 2400,
            'events': [{'stage': stage, 'state': 'failed', 'elapsed_ms': 2300, 'error_kind': 'timeout'}]}


def test_completed_unknown_keeps_its_finite_cause_and_stage_on_the_current_archive(db_engine, app_paths):
    service, account = saved(db_engine, app_paths)
    def provider(*args): raise AssertionError('No login window')
    provider.check_session = lambda *args: {'state': 'unavailable', 'reason': 'timeout', 'diagnostic': diagnostic()}
    runtime = LoginRuntime(db_engine, app_paths, provider)
    try:
        runtime.check_session(account.id, threading.Event())
        check = service.login_checks([service.get(account.id)])[account.id]
        assert check['reason'] == 'timeout'
        assert check['diagnostic'] == {'stage': 'identity_first', 'elapsed_ms': 2400}
        assert service.get(account.id).login_state == 'valid'
        assert not {'platform_user_id', 'session_ref', 'fingerprint', 'events'} & set(check)
    finally:
        runtime.close(1)


@pytest.mark.parametrize('bad', [{'reason': 'Cookie private UID'},
                               {'diagnostic': {**diagnostic(), 'cookie': 'private'}}])
def test_private_provider_details_are_rejected_before_persistence(db_engine, app_paths, bad):
    service, account = saved(db_engine, app_paths)
    def provider(*args): raise AssertionError('No interactive login')
    provider.check_session = lambda *args: {'state': 'unavailable', **bad}
    runtime = LoginRuntime(db_engine, app_paths, provider)
    try:
        runtime.check_session(account.id, threading.Event())
        current = service.get(account.id)
        check = service.login_checks([current])[account.id]
        assert check['reason'] == 'invalid_provider_result' and current.login_state == 'valid'
        assert 'private' not in json.dumps(check) and 'diagnostic' not in check
    finally:
        runtime.close(1)


def test_running_check_hides_old_unknown_and_cancellation_restores_saved_evidence(db_engine, app_paths):
    service, account = saved(db_engine, app_paths)
    service.observe_login_check(account.id, platform_user_id=account.platform_user_id,
                                session_ref=service.get(account.id).session_ref, state='valid')
    original = service.login_checks([service.get(account.id)])[account.id]
    entered = threading.Event()
    def provider(*args): raise AssertionError('No interactive login')
    def checker(account_id, cancel):
        entered.set(); assert cancel.wait(2)
        return {'state': 'unavailable', 'reason': 'cancelled'}
    provider.check_session = checker
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    try:
        job = request(api, 'account.login.check.start', {'account_ids': [account.id]}, 'start-manual')
        assert entered.wait(1)
        checking = request(api, 'account.get', {'id': account.id})['login_check']
        assert checking['state'] == 'checking' and checking['checked_at']
        request(api, 'account.login.check.cancel', {'id': job['id']}, 'cancel-manual')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = request(api, 'account.login.check.status', {'id': job['id']})
            if status['state'] == 'cancelled': break
            time.sleep(.01)
        assert status['state'] == 'cancelled'
        assert request(api, 'account.get', {'id': account.id})['login_check'] == original
    finally:
        api.close(2)


def test_selected_accounts_use_four_platform_readonly_checkers_once_and_never_upload(db_engine, app_paths):
    calls = []; providers = {}; accounts = []
    for platform in ('douyin', 'kuaishou', 'channels', 'bilibili'):
        _, account = saved(db_engine, app_paths, platform); accounts.append(account)
        def provider(*args): raise AssertionError('No interactive login or capability upload')
        def checker(account_id, cancel, account=account):
            calls.append(account.platform)
            return {'state': 'valid', 'platform_user_id': account.platform_user_id}
        provider.check_session = checker; providers[platform] = provider
    api = DesktopApi(db_engine, app_paths, login_providers=providers)
    try:
        params = {'account_ids': [account.id for account in accounts]}
        job = request(api, 'account.login.check.start', params, 'four-checks')
        assert request(api, 'account.login.check.start', params, 'four-checks')['id'] == job['id']
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = request(api, 'account.login.check.status', {'id': job['id']})
            if status['state'] == 'completed': break
            time.sleep(.01)
        assert status == {'id': job['id'], 'state': 'completed', 'completed': 4, 'total': 4}
        assert calls == ['douyin', 'kuaishou', 'channels', 'bilibili']
    finally:
        api.close(2)


def test_explicit_login_safely_cancels_optional_manual_check_without_checking_more_accounts(db_engine, app_paths):
    service, account = saved(db_engine, app_paths)
    _, second = saved(db_engine, app_paths, 'channels')
    entered = threading.Event(); cleaned = threading.Event(); checked = []
    def provider(account_id, cancel):
        assert cleaned.is_set()
        return {'platform_user_id': account.platform_user_id, 'nickname': 'Synthetic', 'session_blob': b'new verified archive'}
    def checker(account_id, cancel):
        checked.append(account_id); entered.set(); assert cancel.wait(2)
        cleaned.set()
        return {'state': 'unavailable', 'reason': 'cancelled'}
    provider.check_session = checker
    api = DesktopApi(db_engine, app_paths, login_providers={'douyin': provider, 'channels': provider})
    try:
        job = request(api, 'account.login.check.start', {'account_ids': [account.id, second.id]}, 'manual-before-login')
        assert entered.wait(1)
        login = api._login_runtime.start('explicit-login', account.id)
        api._login_runtime.thread.join(2)
        assert api._login_runtime.status(login['id'])['state'] == 'completed'
        assert request(api, 'account.login.check.status', {'id': job['id']})['state'] == 'cancelled'
        assert checked == [account.id]
    finally:
        api.close(2)


def test_checking_accounts_are_pending_in_the_same_filter_used_by_the_list(db_engine, app_paths):
    from yijianjifa.app_services.account_access import AccountAccess
    service, account = saved(db_engine, app_paths)
    runtime = type('Runtime', (), {'check_activity': lambda self, _: {'state': 'checking', 'checked_at': '2026-10-25T00:00:00+00:00'}})()
    access = AccountAccess(db_engine, runtime)
    assert access.list('', '', '', 'pending', 0, 20)['total'] == 1
    assert access.list('', '', '', 'ok', 0, 20)['total'] == 0


@pytest.mark.parametrize('check_fixture', ['bilibili'], indirect=True)
def test_readonly_bili_check_returns_from_known_public_landing_before_identity(check_fixture, monkeypatch):
    f = check_fixture
    navigate = f.page.goto; visits = []
    def redirect(url, **options):
        visits.append(url)
        navigate(url, **options)
        f.surface['origin'] = 'https://www.bilibili.com' if len(visits) == 1 else 'https://member.bilibili.com'
    monkeypatch.setattr(f.page, 'goto', redirect)
    result = f.provider.check_session(f.account.id, f.cancel)
    assert result['state'] == 'valid'
    assert len(visits) == 3 and f.page.reads == 2
    assert f.service.get(f.account.id).session_ref == f.ref


@pytest.mark.parametrize('check_fixture', ['bilibili'], indirect=True)
@pytest.mark.parametrize('origin', ['https://untrusted.invalid', 'https://www.bilibili.com'])
def test_landing_recovery_is_bounded_and_never_accepts_the_landing_identity(check_fixture, monkeypatch, origin):
    f = check_fixture
    f.surface['origin'] = origin
    result = f.provider.check_session(f.account.id, f.cancel)
    assert result['state'] == 'unavailable' and f.page.reads == 0
    assert len([event for event in f.events if event[0] == 'visit']) == (4 if 'www.bilibili.com' in origin else 1)
    assert f.context.closed and f.service.get(f.account.id).session_ref == f.ref
