from __future__ import annotations

from yijianjifa.features.accounts.service import AccountService


def verify_relogin_identity(account_service: AccountService, account_id: str, observed_platform_user_id: str) -> None:
    account_service.assert_same_identity(account_id, observed_platform_user_id)
