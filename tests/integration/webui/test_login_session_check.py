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


def test_account_list_has_no_online_check_until_the_native_checker_runs(session_runtime):
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, _, _ = session_runtime
    access = AccountAccess(accounts.engine)
    assert access.get(account.id)['login_check'] is None
    rows = access.list(query='', platform='', group='', state='', offset=0, limit=20)
    assert rows['items'][0]['login_check'] is None


def test_completed_native_check_records_safe_time_and_state_without_rechecking_on_read(session_runtime):
    import json
    from datetime import datetime, timezone
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, _, ref = session_runtime
    calls = []
    def online_check(account_id, cancel):
        calls.append(account_id)
        return {'state': 'valid', 'platform_user_id': '123'}
    provider.check_session = online_check
    before = datetime.now(timezone.utc)
    check(runtime, account.id)
    health = AccountAccess(accounts.engine).get(account.id)['login_check']
    assert set(health) == {'checked_at', 'state'} and health['state'] == 'valid'
    assert before <= datetime.fromisoformat(health['checked_at']) <= datetime.now(timezone.utc)
    rows = AccountAccess(accounts.engine).list(query='', platform='', group='', state='', offset=0, limit=20)
    assert rows['items'][0]['login_check'] == health and calls == [account.id]
    assert ref not in json.dumps(health) and 'platform_user_id' not in health


@pytest.mark.parametrize(('result', 'login_state'), [
    ({'state': 'login_required'}, 'expired'),
    ({'state': 'unavailable', 'reason': 'timeout'}, 'valid'),
    ({'state': 'identity_mismatch', 'platform_user_id': 'different-private-identity'}, 'valid'),
])
def test_health_records_checked_outcome_without_treating_network_or_identity_failure_as_expiry(
        session_runtime, result, login_state):
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, _, _ = session_runtime
    provider.check_session = lambda *args: dict(result)
    check(runtime, account.id)
    public = AccountAccess(accounts.engine).get(account.id)
    assert public['login_check']['state'] == result['state']
    assert public['login_state'] == login_state
    assert 'different-private-identity' not in str(public)


def test_new_login_archive_cannot_inherit_previous_online_check(session_runtime):
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, store, _ = session_runtime
    access = AccountAccess(accounts.engine)
    check(runtime, account.id)
    assert access.get(account.id)['login_check']['state'] == 'valid'
    accounts.attach_session(account.id, store, b'new synthetic login archive')
    assert access.get(account.id)['login_check'] is None
    check(runtime, account.id)
    assert access.get(account.id)['login_check']['state'] == 'valid'


def test_older_inflight_check_cannot_record_a_result_for_a_new_login_archive(session_runtime):
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, store, _ = session_runtime
    def online_check(account_id, cancel):
        accounts.attach_session(account_id, store, b'replacement while old check is running')
        return {'state': 'login_required'}
    provider.check_session = online_check
    assert check(runtime, account.id)['reason'] == 'session_changed'
    assert AccountAccess(accounts.engine).get(account.id)['login_check'] is None
    assert accounts.get(account.id).login_state == 'valid'


@pytest.mark.parametrize('reason', ['account_busy', 'cancelled', 'login_in_progress', 'app_closing'])
def test_skipped_or_cancelled_check_does_not_replace_last_completed_check(session_runtime, reason):
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, _, _ = session_runtime
    access = AccountAccess(accounts.engine)
    check(runtime, account.id)
    previous = access.get(account.id)['login_check']
    provider.check_session = lambda *args: {'state': 'unavailable', 'reason': reason}
    check(runtime, account.id)
    assert access.get(account.id)['login_check'] == previous
    assert accounts.get(account.id).login_state == 'valid'


def test_cancellation_after_online_observation_does_not_store_health_or_expire_session(session_runtime):
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, _, _ = session_runtime
    cancel = threading.Event()
    def online_check(account_id, event):
        event.set()
        return {'state': 'login_required'}
    provider.check_session = online_check
    assert check(runtime, account.id, cancel)['reason'] == 'cancelled'
    assert AccountAccess(accounts.engine).get(account.id)['login_check'] is None
    assert accounts.get(account.id).login_state == 'valid'


@pytest.mark.parametrize('platform', ['douyin', 'channels', 'kuaishou', 'bilibili'])
def test_online_health_metadata_is_shared_by_all_supported_account_platforms(db_engine, app_paths, platform):
    from yijianjifa.app_services.account_access import AccountAccess
    accounts = AccountService(db_engine)
    uid = 'synthetic-private-' + platform
    account = accounts.create(platform, uid, 'Synthetic account')
    store = SessionStore(app_paths.data_dir / 'profiles')
    accounts.attach_session(account.id, store, b'synthetic accepted archive')
    def provider(*args):
        raise AssertionError('Reading health must not open login')
    provider.check_session = lambda *args: {'state': 'valid', 'platform_user_id': uid}
    runtime = LoginRuntime(db_engine, app_paths, providers={platform: provider})
    try:
        check(runtime, account.id)
        public = AccountAccess(db_engine).get(account.id)
        assert public['login_check']['state'] == 'valid'
        assert uid not in str(public) and 'session_ref' not in str(public)
    finally:
        runtime.close(1)


@pytest.mark.parametrize('change', [
    {'v': True}, {'state': 'published'}, {'state': ['valid']},
    {'checked_at': 'not a date'}, {'checked_at': '2026-10-23T10:00:00'},
    {'checked_at': '0001-01-01T00:00:00+14:00'},
    {'fingerprint': 'older-session'}, {'session_ref': 'private-ref'},
])
def test_damaged_or_unbound_health_metadata_is_ignored_without_exposing_values(session_runtime, change):
    import json
    from sqlalchemy import select
    from yijianjifa.app_services.account_access import AccountAccess
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    runtime, provider, accounts, account, _, _ = session_runtime
    check(runtime, account.id)
    with session_scope(accounts.engine) as session:
        saved = session.scalars(select(Setting).where(Setting.key.startswith('account-login-check:'))).one()
        saved.value_json = json.dumps(json.loads(saved.value_json) | change)
    public = AccountAccess(accounts.engine).get(account.id)
    assert public['login_check'] is None
    assert public['login_state'] == 'valid' and 'private-ref' not in str(public)


def test_shared_observer_rejects_older_observations_without_changing_newer_state(session_runtime):
    from datetime import datetime, timedelta, timezone
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, _, ref = session_runtime
    observe = getattr(accounts, 'observe_login_check', None)
    assert callable(observe), 'All publishing paths need one same-archive observation transaction'
    now = datetime.now(timezone.utc)
    assert observe(account.id, platform_user_id='123', session_ref=ref,
                   state='login_required', observed_at=now)
    latest = AccountAccess(accounts.engine).get(account.id)
    assert latest['login_state'] == 'expired'
    assert latest['login_check']['checked_at'] == now.isoformat()
    assert observe(account.id, platform_user_id='123', session_ref=ref,
                   state='valid', observed_at=now - timedelta(seconds=1)) is False
    assert AccountAccess(accounts.engine).get(account.id) == latest


@pytest.mark.parametrize('change', ['uid', 'archive', 'removed'])
def test_shared_observer_never_updates_a_changed_or_deleted_account(session_runtime, change):
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, store, ref = session_runtime
    observe = getattr(accounts, 'observe_login_check', None)
    assert callable(observe), 'Shared observer must bind the accepted UID and archive'
    if change == 'archive':
        accounts.attach_session(account.id, store, b'new accepted archive')
    elif change == 'removed':
        accounts.remove(account.id)
    assert observe(account.id, platform_user_id='different' if change == 'uid' else '123',
                   session_ref=ref, state='login_required') is False
    if change != 'removed':
        saved = AccountAccess(accounts.engine).get(account.id)
        assert saved['login_state'] == 'valid' and saved['login_check'] is None


def test_interactive_relogin_records_the_new_verified_archive_as_valid(session_runtime):
    from yijianjifa.app_services.account_access import AccountAccess
    runtime, provider, accounts, account, _, old_ref = session_runtime
    provider = lambda *args: {'platform_user_id': '123', 'nickname': 'Verified account',
                             'session_blob': b'new verified interactive archive'}
    runtime._providers['douyin'] = provider
    job = runtime.start('interactive-health', account.id)
    runtime.thread.join(2)
    assert runtime.status(job['id'])['state'] == 'completed'
    assert accounts.get(account.id).session_ref != old_ref
    saved = AccountAccess(accounts.engine).get(account.id)
    assert saved['login_state'] == 'valid' and saved['login_check']['state'] == 'valid'


@pytest.mark.parametrize('change', [{'v': True}, {'v': 9}, {'state': 'published'}, {'extra': 'invalid'}])
def test_damaged_future_metadata_cannot_prevent_later_real_observations(session_runtime, change):
    import json
    from sqlalchemy import select
    from yijianjifa.app_services.account_access import AccountAccess
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    runtime, provider, accounts, account, _, _ = session_runtime
    check(runtime, account.id)
    with session_scope(accounts.engine) as session:
        row = session.scalars(select(Setting).where(Setting.key.startswith('account-login-check:'))).one()
        value = json.loads(row.value_json) | {'checked_at': '2099-01-01T00:00:00+00:00'} | change
        row.value_json = json.dumps(value)
    assert check(runtime, account.id) == {'state': 'valid', 'platform_user_id': '123'}
    saved = AccountAccess(accounts.engine).get(account.id)['login_check']
    assert saved['state'] == 'valid' and not saved['checked_at'].startswith('2099')


def test_superseded_same_archive_check_uses_newer_evidence_without_claiming_archive_changed(session_runtime):
    from datetime import datetime, timezone
    runtime, provider, accounts, account, _, ref = session_runtime
    def checker(account_id, cancel):
        assert accounts.observe_login_check(account_id, platform_user_id='123', session_ref=ref,
                                            state='login_required', observed_at=datetime.now(timezone.utc))
        return {'state': 'valid', 'platform_user_id': '123'}
    provider.check_session = checker
    result = check(runtime, account.id)
    assert result == {'state': 'login_required'}
    assert accounts.get(account.id).login_state == 'expired'
