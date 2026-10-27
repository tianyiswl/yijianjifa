"""One native, read-only startup pass; no platform interaction or publication."""
import threading

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore


def startup_type():
    from yijianjifa.app_services import desktop_api
    cls = getattr(desktop_api, 'StartupLoginChecks', None)
    assert callable(cls), 'Desktop startup needs a bounded, once-only account-check worker'
    return cls


def test_startup_worker_checks_supported_accounts_once_in_one_background_thread(db_engine, app_paths):
    calls = []; finished = threading.Event(); main_thread = threading.get_ident()
    accounts = AccountService(db_engine)
    for platform in ('douyin', 'channels', 'bilibili', 'unknown'):
        accounts.create(platform, platform + '-uid', platform)
    class Runtime:
        def available(self, platform): return platform in {'douyin', 'channels', 'bilibili'}
        def check_session(self, account_id, cancel, *, background=False):
            assert background is True and not cancel.is_set()
            calls.append((account_id, threading.get_ident()))
            if len(calls) == 3: finished.set()
            return {'state': 'valid'}
    worker = startup_type()(db_engine, Runtime())
    assert worker.start() is True
    assert finished.wait(2)
    assert worker.start() is False
    assert worker.close(2) is True
    assert len(calls) == 3 and len({thread for _, thread in calls}) == 1
    assert calls[0][1] != main_thread
    assert worker.start() is False


def test_startup_close_cancels_current_check_and_never_starts_following_accounts(db_engine, app_paths):
    entered = threading.Event(); cancelled = threading.Event(); calls = []
    accounts = AccountService(db_engine)
    accounts.create('douyin', 'a', 'First'); accounts.create('douyin', 'b', 'Second')
    class Runtime:
        def available(self, platform): return True
        def check_session(self, account_id, cancel, *, background=False):
            calls.append(account_id); entered.set()
            if cancel.wait(2): cancelled.set()
            return {'state': 'unavailable', 'reason': 'cancelled'}
    worker = startup_type()(db_engine, Runtime())
    worker.start(); assert entered.wait(1)
    assert worker.close(2) is True and cancelled.is_set()
    assert len(calls) == 1 and worker.start() is False


def test_desktop_local_checks_start_one_readonly_pass_and_close_without_new_checks(db_engine, app_paths):
    accounts = AccountService(db_engine)
    account = accounts.create('douyin', '123', 'Saved synthetic account')
    accounts.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'synthetic archive')
    checked = threading.Event(); calls = []
    def provider(*args): raise AssertionError('Startup must not open interactive login')
    def checker(account_id, cancel):
        calls.append(account_id); checked.set()
        return {'state': 'valid', 'platform_user_id': '123'}
    provider.check_session = checker
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    try:
        assert calls == []
        api.start_local_checks(); assert checked.wait(2)
        api.start_local_checks()
    finally:
        assert api.close(2)
    assert calls == [account.id]
    api.start_local_checks()
    assert calls == [account.id]


@pytest.mark.parametrize('backend', [False, True])
def test_interactive_login_cancels_only_startup_check_and_continues_after_cleanup(db_engine, app_paths, backend):
    accounts = AccountService(db_engine)
    account = accounts.create('douyin', '123', 'Synthetic account')
    accounts.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'saved synthetic archive')
    entered = threading.Event(); cleanup = threading.Event(); login_entered = threading.Event()
    def provider(account_id, cancel):
        assert cleanup.is_set(), 'Interactive login must wait for safe startup-browser cleanup'
        login_entered.set()
        return {'platform_user_id': '123', 'nickname': 'Verified account', 'session_blob': b'verified new archive'}
    def checker(account_id, cancel):
        entered.set(); assert cancel.wait(2)
        cleanup.set()
        return {'state': 'unavailable', 'reason': 'cancelled'}
    provider.check_session = checker
    provider.open_backend = provider
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    try:
        api.start_local_checks(); assert entered.wait(1)
        job = api._login_runtime.start('login-over-startup', account.id, backend=backend)
        assert login_entered.wait(2)
        api._login_runtime.thread.join(2)
        assert api._login_runtime.status(job['id'])['state'] == 'completed'
        assert accounts.get(account.id).login_state == 'valid'
    finally:
        assert api.close(2)


def test_user_can_cancel_login_while_waiting_for_startup_cleanup(db_engine, app_paths):
    accounts = AccountService(db_engine)
    account = accounts.create('douyin', '123', 'Synthetic account')
    accounts.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'saved synthetic archive')
    entered = threading.Event(); release = threading.Event(); provider_calls = []
    def provider(*args):
        provider_calls.append('login')
        raise AssertionError('Cancelled waiting login must never open its browser')
    def checker(account_id, cancel):
        entered.set(); assert release.wait(2)
        return {'state': 'unavailable', 'reason': 'cancelled'}
    provider.check_session = checker
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    try:
        api.start_local_checks(); assert entered.wait(1)
        job = api._login_runtime.start('cancel-waiting-login', account.id)
        assert api._login_runtime.cancel(job['id'])['state'] == 'cancelled'
        api._login_runtime.thread.join(1)
        assert not api._login_runtime.thread.is_alive() and provider_calls == []
        release.set(); assert api.close(2)
        assert provider_calls == [] and accounts.get(account.id).login_state == 'valid'
    finally:
        release.set(); api.close(2)


def test_startup_skips_a_busy_account_and_checks_other_accounts_without_changing_busy_health(db_engine, app_paths):
    from yijianjifa.app_services.account_access import AccountAccess
    from yijianjifa.webui.chromium_publish import account_lock
    accounts = AccountService(db_engine)
    store = SessionStore(app_paths.data_dir / 'profiles')
    busy = accounts.create('douyin', '123', 'A busy account')
    free = accounts.create('douyin', '456', 'B free account')
    for account in (busy, free): accounts.attach_session(account.id, store, b'synthetic saved archive')
    skipped = threading.Event(); checked = threading.Event()
    def provider(*args): raise AssertionError('Startup never opens login')
    def checker(account_id, cancel):
        lock = account_lock(account_id)
        if not lock.acquire(blocking=False):
            skipped.set()
            return {'state': 'unavailable', 'reason': 'account_busy'}
        try:
            assert account_id == free.id
            checked.set()
            return {'state': 'valid', 'platform_user_id': '456'}
        finally:
            lock.release()
    provider.check_session = checker
    lock = account_lock(busy.id); assert lock.acquire(blocking=False)
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    try:
        api.start_local_checks(); assert skipped.wait(1) and checked.wait(1)
        api._startup_login_checks._thread.join(2)
        access = AccountAccess(db_engine)
        assert access.get(busy.id)['login_check'] is None
        assert access.get(busy.id)['login_state'] == 'valid'
        assert access.get(free.id)['login_check']['state'] == 'valid'
    finally:
        lock.release(); assert api.close(2)


@pytest.mark.parametrize('action', ['continue', 'cancel', 'close'])
def test_foreground_publish_check_replaces_startup_check_only_after_safe_cleanup(db_engine, app_paths, action):
    from yijianjifa.app_services.account_access import AccountAccess
    accounts = AccountService(db_engine)
    account = accounts.create('douyin', '123', 'Synthetic account')
    accounts.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'synthetic saved archive')
    entered = threading.Event(); allow_cleanup = threading.Event(); cleaned = threading.Event()
    startup_cancelled = threading.Event(); foreground_cancel = threading.Event()
    calls = []; result = []
    def provider(*args): raise AssertionError('A publish check must never open interactive login')
    def checker(account_id, cancel):
        calls.append(account_id)
        if len(calls) == 1:
            entered.set()
            if cancel.wait(2): startup_cancelled.set()
            assert allow_cleanup.wait(2)
            cleaned.set()
            return {'state': 'unavailable', 'reason': 'cancelled'}
        assert cleaned.is_set(), 'The foreground check must wait for startup-browser cleanup'
        return {'state': 'valid', 'platform_user_id': '123'}
    provider.check_session = checker
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    foreground = threading.Thread(target=lambda: result.append(
        api._login_runtime.check_session(account.id, foreground_cancel)))
    try:
        api.start_local_checks(); assert entered.wait(1)
        foreground.start()
        assert startup_cancelled.wait(1), 'Publishing should preempt its optional startup check, not fail as busy'
        if action == 'cancel':
            foreground_cancel.set()
        elif action == 'close':
            assert api._login_runtime.close(.01) is False
        else:
            allow_cleanup.set()
        foreground.join(1)
        assert not foreground.is_alive()
        if action == 'continue':
            assert result == [{'state': 'valid', 'platform_user_id': '123'}]
            assert calls == [account.id, account.id]
            assert AccountAccess(db_engine).get(account.id)['login_check']['state'] == 'valid'
        else:
            assert result == [{'state': 'unavailable', 'reason': 'cancelled' if action == 'cancel' else 'app_closing'}]
            assert calls == [account.id]
            assert AccountAccess(db_engine).get(account.id)['login_check'] is None
        assert accounts.get(account.id).login_state == 'valid'
    finally:
        allow_cleanup.set()
        if foreground.ident is not None: foreground.join(2)
        assert api.close(2)
