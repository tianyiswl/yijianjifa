"""Native session verification guards real account versions, not local badges."""
import threading

import pytest

from yijianjifa.app_services.login_runtime import LoginRuntime
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore


@pytest.fixture
def session_runtime(db_engine, app_paths):
    accounts = AccountService(db_engine)
    account = accounts.create('douyin', '123', 'Synthetic account')
    store = SessionStore(app_paths.data_dir / 'profiles')
    ref = accounts.attach_session(account.id, store, b'synthetic original archive')
    def provider(account_id, cancel):
        raise AssertionError('A read-only check must not open interactive login')
    provider.check_session = lambda account_id, cancel: {'state': 'valid', 'platform_user_id': '123'}
    runtime = LoginRuntime(db_engine, app_paths, provider)
    yield runtime, provider, accounts, account, store, ref
    runtime.close(2)


def check(runtime, account_id, event=None):
    method = getattr(runtime, 'check_session', None)
    assert callable(method), 'LoginRuntime must validate the native online check contract'
    return method(account_id, event or threading.Event())


@pytest.mark.parametrize('error', [OSError, RuntimeError])
def test_final_readonly_cleanup_error_cannot_block_an_already_closed_runtime(session_runtime, error):
    runtime, provider, _, _, _, _ = session_runtime
    calls = []
    def failed_cleanup(timeout):
        calls.append('failed')
        raise error('synthetic final cleanup failure')
    provider.check_session.close = failed_cleanup
    def other_provider(*args):
        raise AssertionError('Closing must not start an authentication window')
    other_provider.check_session = lambda *args: {'state': 'unavailable'}
    other_provider.check_session.close = lambda timeout: calls.append('other')
    runtime._providers['bilibili'] = other_provider
    assert runtime.close(0) is True
    assert set(calls) == {'failed', 'other'}


def test_native_valid_check_updates_only_the_same_account_login_badge(session_runtime):
    runtime, provider, accounts, account, _, ref = session_runtime
    accounts.set_login_state(account.id, 'expired')
    result = check(runtime, account.id)
    assert result == {'state': 'valid', 'platform_user_id': '123'}
    current = accounts.get(account.id)
    assert current.login_state == 'valid' and current.session_ref == ref
    assert current.capabilities == account.capabilities


@pytest.mark.parametrize('state', ['login_required', 'unavailable', 'identity_mismatch'])
def test_only_explicit_login_required_marks_current_session_expired(session_runtime, state):
    runtime, provider, accounts, account, _, _ = session_runtime
    provider.check_session = lambda *args: {'state': state}
    result = check(runtime, account.id)
    assert result['state'] == state
    assert accounts.get(account.id).login_state == ('expired' if state == 'login_required' else 'valid')


def test_missing_saved_session_requests_login_without_claiming_online_expiry(session_runtime):
    runtime, provider, accounts, account, _, _ = session_runtime
    provider.check_session = lambda *args: {'state': 'login_required', 'reason': 'session_missing'}
    assert check(runtime, account.id) == {'state': 'login_required', 'reason': 'session_missing'}
    assert accounts.get(account.id).login_state == 'pending_verification'


@pytest.mark.parametrize('response', [None, True, {'state': 'valid'}, {'state': 'valid', 'platform_user_id': '456'},
    {'state': 'published'}, {'state': 'valid', 'platform_user_id': '123', 'session_blob': b'secret'},
    {'state': 'unavailable', 'reason': 'Cookie=private /private/profile'}])
def test_native_checker_never_accepts_unverified_uid_or_leaks_provider_payload(session_runtime, response):
    runtime, provider, accounts, account, _, _ = session_runtime
    provider.check_session = lambda *args: response
    result = check(runtime, account.id)
    assert result['state'] in {'unavailable', 'identity_mismatch'}
    assert 'private' not in str(result) and 'secret' not in str(result)
    assert accounts.get(account.id).login_state == 'valid'


@pytest.mark.parametrize('response', [{'state': 'valid', 'platform_user_id': '123'}, {'state': 'login_required'}])
def test_relogin_new_session_version_cannot_be_overwritten_by_an_older_check(session_runtime, response):
    runtime, provider, accounts, account, store, _ = session_runtime
    replacement = []
    def checker(account_id, cancel):
        replacement.append(accounts.attach_session(account_id, store, b'synthetic new accepted archive'))
        return response
    provider.check_session = checker
    result = check(runtime, account.id)
    assert result == {'state': 'unavailable', 'reason': 'session_changed'}
    current = accounts.get(account.id)
    assert current.login_state == 'valid' and current.session_ref == replacement[0]


def test_check_blocks_duplicate_or_login_and_close_cancels_active_owned_check(session_runtime):
    runtime, provider, accounts, account, _, _ = session_runtime
    entered = threading.Event(); release = threading.Event(); cancel = threading.Event(); result = []
    def checker(account_id, event):
        entered.set()
        assert release.wait(2)
        return {'state': 'login_required'}
    provider.check_session = checker
    worker = threading.Thread(target=lambda: result.append(check(runtime, account.id, cancel)))
    worker.start()
    assert entered.wait(1)
    try:
        assert check(runtime, account.id) == {'state': 'unavailable', 'reason': 'account_busy'}
        with pytest.raises(ValueError): runtime.start('login-during-check', account.id)
        assert runtime.close(.01) is False and cancel.is_set()
        release.set(); worker.join(2)
        assert result == [{'state': 'unavailable', 'reason': 'cancelled'}]
        assert accounts.get(account.id).login_state == 'valid'
        assert runtime.close(.1) is True
    finally:
        release.set(); worker.join(2)


def test_missing_checker_and_network_exception_keep_existing_state(session_runtime):
    runtime, provider, accounts, account, _, _ = session_runtime
    del provider.check_session
    assert check(runtime, account.id) == {'state': 'unavailable', 'reason': 'provider_unavailable'}
    def fail(*args): raise TimeoutError('private cookie/profile')
    provider.check_session = fail
    result = check(runtime, account.id)
    assert result['state'] == 'unavailable' and 'private' not in str(result)
    assert accounts.get(account.id).login_state == 'valid'


def diagnostic(stage='identity_second'):
    return {'v': 1, 'stage': stage, 'last_stage': 'cleanup', 'elapsed_ms': 25000,
            'events': [{'stage': 'browser_launch', 'state': 'completed', 'elapsed_ms': 100},
                       {'stage': stage, 'state': 'failed', 'elapsed_ms': 24900, 'error_kind': 'timeout'}]}


def test_native_checker_preserves_only_valid_finite_diagnostics(session_runtime):
    runtime, provider, accounts, account, _, _ = session_runtime
    result = {'state': 'valid', 'platform_user_id': '123', 'diagnostic': diagnostic()}
    result['diagnostic']['events'][-1] = {'stage': 'identity_second', 'state': 'completed', 'elapsed_ms': 24900}
    provider.check_session = lambda *args: result
    checked = check(runtime, account.id)
    assert checked == result
    result['diagnostic']['events'][0]['elapsed_ms'] = 50
    assert checked['diagnostic']['events'][0]['elapsed_ms'] == 100
    assert accounts.get(account.id).login_state == 'valid'


@pytest.mark.parametrize('invalid', [None, {}, {'v': True},
    {**diagnostic(), 'url': 'https://private.invalid/?session=SECRET'},
    {**diagnostic(), 'stage': 'PRIVATE_STAGE'},
    {**diagnostic(), 'elapsed_ms': True},
    {**diagnostic(), 'elapsed_ms': 120001},
    {**diagnostic(), 'events': [{'stage': 'surface', 'state': 'completed', 'elapsed_ms': 1, 'body': 'SECRET'}]},
    {**diagnostic(), 'events': [{'stage': 'surface', 'state': 'completed', 'elapsed_ms': 1, 'error_kind': 'timeout'}]},
    {**diagnostic(), 'events': [{'stage': 'surface', 'state': 'started', 'elapsed_ms': 2},
                              {'stage': 'surface', 'state': 'completed', 'elapsed_ms': 1}]}])
def test_invalid_present_diagnostic_cannot_obtain_valid_authority_or_update_badge(session_runtime, invalid):
    runtime, provider, accounts, account, _, _ = session_runtime
    accounts.set_login_state(account.id, 'expired')
    provider.check_session = lambda *args: {'state': 'valid', 'platform_user_id': '123', 'diagnostic': invalid}
    assert check(runtime, account.id) == {'state': 'unavailable', 'reason': 'invalid_provider_result'}
    assert accounts.get(account.id).login_state == 'expired'
