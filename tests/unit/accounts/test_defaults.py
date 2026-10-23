import pytest

from yijianjifa.features.accounts.service import AccountIdentityMismatch, AccountService


def test_each_platform_can_have_one_default_without_touching_other_platform(db_engine):
    service = AccountService(db_engine)
    d1 = service.create("douyin", "dy-1", "A")
    d2 = service.create("douyin", "dy-2", "B")
    future = service.create("future", "f-1", "F")

    service.set_default("douyin", d2.id)
    service.set_default("future", future.id)

    assert service.default("douyin").id == d2.id
    assert service.default("future").id == future.id
    assert service.get(d1.id).is_default is False


def test_setting_default_rejects_account_from_other_platform(db_engine):
    service = AccountService(db_engine)
    future = service.create("future", "f-1", "F")
    with pytest.raises(ValueError):
        service.set_default("douyin", future.id)


def test_relogin_as_different_platform_identity_blocks_old_account(db_engine):
    service = AccountService(db_engine)
    acct = service.create("douyin", "platform-user-1", "门店A")
    with pytest.raises(AccountIdentityMismatch):
        service.assert_same_identity(acct.id, "platform-user-2")
