"""DB references select immutable session versions; synthetic data only."""
import pytest
from sqlalchemy import event
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore


def test_refresh_changes_reference_and_preserves_previous_version(db_engine,tmp_path):
    service=AccountService(db_engine);store=SessionStore(tmp_path/'profiles')
    account=service.create('douyin','synthetic-id','test')
    first=service.attach_session(account.id,store,b'old synthetic')
    second=service.attach_session(account.id,store,b'new synthetic')
    assert second!=first
    assert store.get(account.id,ref=first)==b'old synthetic'
    assert store.get(account.id,ref=service.get(account.id).session_ref)==b'new synthetic'


def test_database_write_failure_does_not_replace_committed_session(db_engine,tmp_path):
    service=AccountService(db_engine);store=SessionStore(tmp_path/'profiles')
    account=service.create('douyin','synthetic-id','test')
    old=service.attach_session(account.id,store,b'old synthetic')
    def fail(conn,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith('UPDATE ACCOUNTS'):raise RuntimeError('synthetic database failure')
    event.listen(db_engine,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError):service.attach_session(account.id,store,b'new synthetic')
    finally:event.remove(db_engine,'before_cursor_execute',fail)
    assert service.get(account.id).session_ref==old
    assert store.get(account.id,ref=old)==b'old synthetic'


def test_reference_cannot_read_another_account_or_outside_store(tmp_path):
    store=SessionStore(tmp_path/'profiles')
    ref=store.put('A',b'synthetic A')
    for invalid in (ref,'profile:../../secret','/etc/passwd'):
        with pytest.raises(ValueError):store.get('B',ref=invalid)


def test_legacy_reference_remains_readable_after_refresh(db_engine,tmp_path):
    from yijianjifa.features.accounts.service import AccountRow
    from yijianjifa.storage.db import session_scope
    service=AccountService(db_engine);store=SessionStore(tmp_path/'profiles')
    account=service.create('douyin','synthetic-id','test')
    legacy=store.put(account.id,b'legacy synthetic')
    with session_scope(db_engine) as s:s.get(AccountRow,account.id).session_ref=legacy
    new=service.attach_session(account.id,store,b'new synthetic')
    assert new!=legacy
    assert store.get(account.id,ref=legacy)==b'legacy synthetic'
    assert store.get(account.id,ref=service.get(account.id).session_ref)==b'new synthetic'
