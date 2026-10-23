import pytest

from yijianjifa.features.accounts.service import AccountIdentityMismatch, AccountService
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.features.publish.platforms.douyin.browser_session import build_browser_session_config
from yijianjifa.features.publish.platforms.douyin.identity import verify_relogin_identity


def test_browser_session_uses_account_isolated_profile_directory(tmp_path):
    store = SessionStore(tmp_path / "profiles")
    a = build_browser_session_config(store, "A1")
    b = build_browser_session_config(store, "A2")
    assert a.user_data_dir != b.user_data_dir
    assert a.user_data_dir == store.profile_dir("A1")
    assert a.headless is False


def test_douyin_relogin_identity_wrapper_blocks_wrong_platform_user(db_engine):
    service = AccountService(db_engine)
    account = service.create("douyin", "dy-user-1", "账号A")
    with pytest.raises(AccountIdentityMismatch):
        verify_relogin_identity(service, account.id, "dy-user-2")
