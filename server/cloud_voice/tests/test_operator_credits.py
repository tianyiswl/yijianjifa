"""Operator code handling uses isolated databases and synthetic orders only."""

import base64
from concurrent.futures import ThreadPoolExecutor
import secrets
import sqlite3

import pytest

from yijianjifa_cloud_voice.auth import AuthService
from yijianjifa_cloud_voice.credits import CreditService, _code_hash
from yijianjifa_cloud_voice.errors import CloudError
from yijianjifa_cloud_voice.store import Store


@pytest.fixture
def operator(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / 'operator.db', clock=lambda: now[0])
    key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    return store, CreditService(store, operator_code_key=key), key, now


def test_same_order_replays_original_codes_even_after_restart(operator):
    store, credits, key, _ = operator
    first = credits.issue(2, order_ref=' QA-ORDER-1 ')
    replay = CreditService(Store(store.path), operator_code_key=key).issue(2, order_ref='QA-ORDER-1')
    assert [row['code'] for row in replay] == [row['code'] for row in first]
    assert {row['id'] for row in replay} == {row['id'] for row in first}
    assert all(row['replayed'] is False for row in first)
    assert all(row['replayed'] is True for row in replay)
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 2


def test_same_order_cannot_change_count_or_issue_concurrently(operator):
    store, credits, _, _ = operator
    with ThreadPoolExecutor(max_workers=4) as pool:
        issued = list(pool.map(lambda _: credits.issue(1, order_ref='QA-CONCURRENT'), range(4)))
    assert len({rows[0]['id'] for rows in issued}) == 1
    assert sum(rows[0]['replayed'] is False for rows in issued) == 1
    with pytest.raises(CloudError) as error:
        credits.issue(2, order_ref='QA-CONCURRENT')
    assert error.value.code == 'order_count_conflict'
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 1


def test_raw_code_is_encrypted_at_rest_and_not_exposed_in_lists(operator):
    store, credits, _, _ = operator
    issued = credits.issue(1, order_ref='QA-PRIVATE')[0]
    with store.connect() as conn:
        row = dict(conn.execute('SELECT * FROM redemption_codes').fetchone())
        assert row['encrypted_code']
        assert issued['code'] not in str(row)
        assert issued['code'].replace('-', '') not in str(row)
    public = credits.list_codes()
    assert public['total'] == 1
    assert 'code' not in public['items'][0]
    assert 'encrypted_code' not in public['items'][0]
    assert 'code_hash' not in public['items'][0]
    assert issued['code'] not in str(public)
    assert credits.reveal_code(issued['id'])['code'] == issued['code']


def test_wrong_key_and_swapped_ciphertext_never_reveal_a_code(operator):
    store, credits, _, _ = operator
    a, b = credits.issue(2, order_ref='QA-CIPHERTEXT')
    wrong = CreditService(store, operator_code_key=base64.urlsafe_b64encode(secrets.token_bytes(32)))
    with pytest.raises(CloudError) as error:
        wrong.reveal_code(a['id'])
    assert error.value.code == 'code_copy_unavailable'
    with store.transaction() as conn:
        cipher = conn.execute('SELECT encrypted_code FROM redemption_codes WHERE id=?', (b['id'],)).fetchone()[0]
        conn.execute('UPDATE redemption_codes SET encrypted_code=? WHERE id=?', (cipher, a['id']))
    with pytest.raises(CloudError) as error:
        credits.reveal_code(a['id'])
    assert error.value.code == 'code_copy_unavailable'


def test_legacy_schema_migrates_without_reissuing_unrecoverable_codes(tmp_path):
    path = tmp_path / 'legacy.db'
    raw = 'ABCDE-' * 7 + 'ABCDE'
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE redemption_codes (
            id TEXT PRIMARY KEY,code_hash TEXT NOT NULL UNIQUE,suffix TEXT NOT NULL,
            batch_id TEXT NOT NULL,order_ref TEXT NOT NULL,price_fen INTEGER NOT NULL,
            words INTEGER NOT NULL,created_at REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,
            redeemed_by TEXT,redeemed_at REAL)''')
        conn.execute('''INSERT INTO redemption_codes VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                     ('old-code', _code_hash(raw), 'ABCDE', 'old-batch', 'QA-OLD', 1990, 10000, 123, 0, None, None))
    key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    store = Store(path)
    credits = CreditService(store, operator_code_key=key)
    assert credits.list_codes()['items'][0]['can_copy'] is False
    with pytest.raises(CloudError) as error:
        credits.issue(1, order_ref='QA-OLD')
    assert error.value.code == 'legacy_codes_unavailable'
    with pytest.raises(CloudError):
        credits.reveal_code('old-code')
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 1
    assert credits.revoke('old-code')['revoked'] is True


def test_without_operator_key_cli_issue_still_works_but_never_reissues_order(tmp_path):
    store = Store(tmp_path / 'no-key.db')
    credits = CreditService(store)
    issued = credits.issue(1, order_ref='QA-CLI')[0]
    assert issued['code']
    with pytest.raises(CloudError):
        credits.issue(1, order_ref='QA-CLI')
    assert credits.code_status(issued['id'])['can_copy'] is False
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 1


def test_delivery_first_time_is_preserved_and_revoked_code_cannot_be_delivered(operator):
    _, credits, _, now = operator
    issued = credits.issue(1, order_ref='QA-DELIVER')[0]
    assert credits.code_status(issued['id'])['status'] == 'unused'
    delivered = credits.mark_delivered(issued['id'])
    assert delivered['delivered_at'] == 1000.0
    assert delivered['status'] == 'delivered'
    now[0] = 2000
    assert credits.mark_delivered(issued['id'])['delivered_at'] == 1000.0
    credits.revoke(issued['id'])
    assert credits.code_status(issued['id'])['status'] == 'revoked'
    for operation in (credits.mark_delivered, credits.reveal_code):
        with pytest.raises(CloudError) as error:
            operation(issued['id'])
        assert error.value.code == 'code_revoked'


def test_redeemed_code_shows_account_but_cannot_be_copied_or_reissued(operator):
    store, credits, _, _ = operator
    account = AuthService(store).register('qa_buyer', 'password123')['account_id']
    issued = credits.issue(1, order_ref='QA-REDEEM')[0]
    credits.redeem(account, issued['code'], 'qa-redemption-001')
    status = credits.code_status(issued['id'])
    assert status['status'] == 'redeemed'
    assert status['redeemed_username'] == 'qa_buyer'
    assert status['can_copy'] is False
    assert credits.list_codes(search='qa_buyer', status='redeemed')['total'] == 1
    for operation in (credits.reveal_code, credits.mark_delivered):
        with pytest.raises(CloudError) as error:
            operation(issued['id'])
        assert error.value.code == 'already_redeemed'
    with pytest.raises(CloudError):
        credits.issue(1, order_ref='QA-REDEEM')
    assert credits.balance(account)['available_words'] == 10000


def test_code_listing_is_paginated_filtered_and_search_escapes_wildcards(operator):
    _, credits, _, now = operator
    first = credits.issue(1, order_ref='QA-_literal%')[0]
    now[0] = 2000
    second = credits.issue(1, order_ref='QA-OTHER')[0]
    credits.mark_delivered(first['id'])
    page = credits.list_codes(page=0, page_size=1)
    assert page['items'][0]['id'] == second['id']
    assert page['total'] == 2
    assert page['page_size'] == 1
    assert credits.list_codes(page=1, page_size=1)['items'][0]['id'] == first['id']
    assert credits.list_codes(search='_literal%', status='delivered')['total'] == 1
    assert credits.list_codes(search='%')['total'] == 1
    assert credits.list_codes(search=first['code'].split('-')[-1])['total'] == 1
    assert credits.list_codes(status='unused')['total'] == 1
    assert credits.list_codes(page=3)['items'] == []


@pytest.mark.parametrize('arguments', [
    {'page': True}, {'page': -1}, {'page': 100001}, {'page_size': True},
    {'page_size': 0}, {'page_size': 101}, {'search': None}, {'search': 'a'*129},
    {'status': 'unknown'}, {'status': None},
])
def test_list_rejects_unbounded_or_ambiguous_inputs(operator, arguments):
    _, credits, _, _ = operator
    with pytest.raises(CloudError):
        credits.list_codes(**arguments)


def test_unknown_code_operator_operations_return_not_found(operator):
    _, credits, _, _ = operator
    for operation in (credits.code_status, credits.reveal_code, credits.mark_delivered):
        with pytest.raises(CloudError) as error:
            operation('missing-code')
        assert error.value.code == 'code_not_found'


def test_purchase_id_serializes_concurrent_retries_and_allows_same_buyer_next_time(operator):
    store,credits,key,_=operator
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(lambda _:credits.issue(1,order_ref='微信昵称',purchase_id='a'*32),range(4)))
    assert len({r[0]['id'] for r in rows})==1
    restored=CreditService(Store(store.path),operator_code_key=key)
    assert restored.issue(1,order_ref='微信昵称',purchase_id='a'*32)[0]['id']==rows[0][0]['id']
    assert restored.issue(1,order_ref='微信昵称',purchase_id='b'*32)[0]['id']!=rows[0][0]['id']
