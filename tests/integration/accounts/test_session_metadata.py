from sqlalchemy import text

from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore


def test_database_contains_session_reference_not_secret_payload(db_engine, tmp_path):
    service = AccountService(db_engine)
    acct = service.create("douyin", "dy-platform-id", "门店A")
    store = SessionStore(tmp_path / "profiles")
    secret = b"secret-cookie-state"

    ref = service.attach_session(acct.id, store, secret)

    assert b"secret-cookie-state" == store.get(acct.id, ref=service.get(acct.id).session_ref)
    assert "secret-cookie-state" not in ref
    with db_engine.connect() as connection:
        row = connection.execute(text("select session_ref from accounts where id=:id"), {"id": acct.id}).one()
    assert "secret-cookie-state" not in row.session_ref
    assert row.session_ref == ref


def test_session_file_uses_account_isolated_directory_and_delete(db_engine, tmp_path):
    service = AccountService(db_engine)
    acct = service.create("douyin", "dy-platform-id", "门店A")
    store = SessionStore(tmp_path / "profiles")
    service.attach_session(acct.id, store, b"secret")
    assert store.get(acct.id, ref=service.get(acct.id).session_ref) == b"secret"
    store.delete(acct.id)
    assert store.get(acct.id, ref=service.get(acct.id).session_ref) is None
