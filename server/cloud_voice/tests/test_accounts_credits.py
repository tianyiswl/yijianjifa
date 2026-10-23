from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from yijianjifa_cloud_voice.auth import AuthService
from yijianjifa_cloud_voice.credits import CreditService
from yijianjifa_cloud_voice.errors import CloudError
from yijianjifa_cloud_voice.store import Store


@pytest.fixture
def services(tmp_path):
    store = Store(tmp_path / 'cloud.db')
    return store, AuthService(store), CreditService(store)


def test_registration_login_password_storage_and_refresh_replay(services):
    store, auth, _ = services
    session = auth.register('test_user', 'safe password 123')
    assert auth.authenticate(session['access_token']) == session['account_id']
    with store.connect() as conn:
        row = conn.execute('SELECT * FROM accounts').fetchone()
        assert row['password_hash'] != 'safe password 123'
        assert 'safe password 123' not in row['password_hash']
    login = auth.login('test_user', 'safe password 123')
    rotated = auth.refresh(login['refresh_token'])
    with pytest.raises(CloudError, match='登录'):
        auth.refresh(login['refresh_token'])
    # Replaying an already rotated refresh revokes the token family.
    with pytest.raises(CloudError):
        auth.authenticate(rotated['access_token'])
    assert auth.authenticate(session['access_token']) == session['account_id']


def test_recovery_rotates_credentials_and_revokes_old_sessions(services):
    _, auth, _ = services
    session = auth.register('recover_user', 'safe password 123')
    credential = auth.export_recovery(session['account_id'], 'safe password 123')
    recovered = auth.recover(credential['account_id'], credential['recovery_secret'], 'a new password 456')
    with pytest.raises(CloudError):
        auth.authenticate(session['access_token'])
    with pytest.raises(CloudError):
        auth.recover(credential['account_id'], credential['recovery_secret'], 'another password 7')
    assert auth.authenticate(recovered['access_token']) == session['account_id']
    assert auth.login('recover_user', 'a new password 456')['account_id'] == session['account_id']


def test_one_code_only_credits_one_concurrent_account(services):
    store, auth, credits = services
    ids = [auth.register(f'user_{i}', 'safe password 123')['account_id'] for i in range(2)]
    issued = credits.issue(1, order_ref='isolated-test-order')[0]
    def redeem(account_id):
        try:
            return credits.redeem(account_id, issued['code'], 'request-key-0001')
        except CloudError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, ids))
    assert sum(isinstance(result, dict) for result in results) == 1
    with store.connect() as conn:
        assert conn.execute('SELECT SUM(credits) FROM accounts').fetchone()[0] == 10000
        assert conn.execute('SELECT COUNT(*) FROM ledger').fetchone()[0] == 1
        code = conn.execute('SELECT * FROM redemption_codes').fetchone()
        assert issued['code'] not in tuple(code)


def test_redemption_idempotency_payload_conflict_and_fixed_pack(services):
    _, auth, credits = services
    account = auth.register('credit_user', 'safe password 123')['account_id']
    a, b = credits.issue(2, order_ref='isolated-test-order')
    receipt = credits.redeem(account, a['code'], 'persistent-key-0001')
    assert receipt['added_words'] == 10000
    assert receipt['price_fen'] == 1990
    replay=credits.redeem(account, a['code'], 'persistent-key-0001')
    assert receipt['replayed'] is False
    assert replay==dict(receipt,replayed=True)
    with pytest.raises(CloudError) as conflict:
        credits.redeem(account, b['code'], 'persistent-key-0001')
    assert conflict.value.code == 'idempotency_conflict'
    assert credits.balance(account)['available_words'] == 10000
    credits.revoke(b['id'])
    with pytest.raises(CloudError):
        credits.redeem(account, b['code'], 'persistent-key-0002')
    assert credits.usage(account)['total'] == 1


def test_password_change_and_logout_all(services):
    _, auth, _ = services
    session = auth.register('password_user', 'safe password 123')
    auth.change_password(session['account_id'], 'safe password 123', 'updated password 456')
    with pytest.raises(CloudError):
        auth.authenticate(session['access_token'])
    current = auth.login('password_user', 'updated password 456')
    auth.logout_all(current['account_id'], 'updated password 456')
    with pytest.raises(CloudError):
        auth.authenticate(current['access_token'])


def test_transaction_rolls_back_and_enforces_nonnegative_balance(services):
    store, auth, _ = services
    account = auth.register('balance_user', 'safe password 123')['account_id']
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as conn:
            conn.execute('UPDATE accounts SET reserved=1 WHERE id=?', (account,))
    assert CreditService(store).balance(account)['available_words'] == 0


def test_cli_only_writes_raw_codes_to_new_private_file(tmp_path,capsys):
    from yijianjifa_cloud_voice.admin import main
    output=tmp_path/'private-codes.json'
    argv=['--database',str(tmp_path/'admin.db'),'issue','--order-ref','isolated-cli-test',
          '--output',str(output),'--confirm-payment-received']
    assert main(argv)==0
    import json
    issued=json.loads(output.read_text())
    assert output.stat().st_mode&0o777==0o600
    assert issued['codes'][0]['code'] not in capsys.readouterr().out
    original=output.read_bytes()
    assert main(argv)==1
    assert output.read_bytes()==original
    conn=Store(tmp_path/'admin.db').connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0]==1
    finally:
        conn.close()


def test_api_password_and_names_are_bounded(services):
    _,auth,_=services
    for username,password in [('abc','safe password 123'),('invalid name','safe password 123'),
                              ('good_user','short')]:
        with pytest.raises(CloudError):
            auth.register(username,password)


@pytest.mark.parametrize('operation', ['register', 'change_password', 'recover'])
@pytest.mark.parametrize('length,accepted', [(5,False), (6,True), (128,True), (129,False)])
def test_new_password_has_same_six_to_128_boundary_for_all_account_flows(services, operation, length, accepted):
    store, auth, _ = services
    username, old_password = 'password_bounds', 'existing-long-password-' * 4
    password = 'x' * length
    session = None if operation == 'register' else auth.register(username, old_password)
    credential = auth.export_recovery(session['account_id'], old_password) if operation == 'recover' else None
    def apply_password():
        if operation == 'register':
            return auth.register(username, password)
        if operation == 'change_password':
            return auth.change_password(session['account_id'], old_password, password)
        return auth.recover(credential['account_id'], credential['recovery_secret'], password)
    if not accepted:
        with pytest.raises(CloudError, match='6至128位') as error:
            apply_password()
        assert error.value.code == 'invalid_password'
        if session:
            assert auth.login(username, old_password)['account_id'] == session['account_id']
        return
    result = apply_password()
    account_id = result['account_id'] if operation != 'change_password' else session['account_id']
    assert auth.login(username, password)['account_id'] == account_id
    with store.connect() as connection:
        stored = connection.execute('SELECT password_hash FROM accounts WHERE id=?', (account_id,)).fetchone()[0]
    # The shorter minimum must not reduce password derivation strength.
    assert stored.startswith('pbkdf2_sha256$600000$')


def test_existing_long_password_still_logs_in_after_minimum_change(services):
    _, auth, _ = services
    old_password = 'original-account-password-with-letters-0123456789' * 2
    registered = auth.register('existing_account', old_password)
    assert auth.login('existing_account', old_password)['account_id'] == registered['account_id']
    with pytest.raises(CloudError) as error:
        auth.login('existing_account', old_password + 'wrong')
    assert error.value.code == 'invalid_credentials'


@pytest.mark.parametrize('length,accepted', [(6,True), (128,True), (129,False)])
def test_registration_password_unicode_boundary_counts_non_bmp_codepoints(services, length, accepted):
    _, auth, _ = services
    # Each non-BMP character occupies two JavaScript UTF-16 code units. Frontend
    # Array.from and Python len must both count one Unicode code point here.
    password = '\U0001f600' * length
    if not accepted:
        with pytest.raises(CloudError) as error:
            auth.register('unicode_password', password)
        assert error.value.code == 'invalid_password'
        return
    account = auth.register('unicode_password', password)
    assert auth.login('unicode_password', password)['account_id'] == account['account_id']
