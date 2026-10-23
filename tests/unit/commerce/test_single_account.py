import pytest
from pydantic import ValidationError


def test_commerce_draft_accepts_one_account_not_list():
    from yijianjifa.features.commerce.models import CommerceDraft

    draft = CommerceDraft(account_id="A01")
    assert draft.account_id == "A01"
    with pytest.raises(ValidationError):
        CommerceDraft(account_id=["A01", "A02"])


def test_commerce_service_preselects_default_only_when_commerce_available(db_engine):
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.commerce.service import CommerceService

    accounts = AccountService(db_engine)
    account = accounts.create("douyin", "u1", "A")
    accounts.set_default("douyin", account.id)
    service = CommerceService(db_engine)

    assert service.initial_account_id("douyin") is None
    accounts.update_capabilities(account.id, CapabilitySnapshot(commerce="available"))
    assert service.initial_account_id("douyin") == account.id


def test_confirming_commerce_task_blocks_when_any_selected_video_has_no_store(db_engine):
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.commerce.models import CommerceDraft
    from yijianjifa.features.commerce.service import CommerceService

    accounts = AccountService(db_engine)
    account = accounts.create("douyin", "u-store", "店号")
    accounts.update_capabilities(account.id, CapabilitySnapshot(commerce="available", location_scope="local"))
    draft = CommerceDraft(
        account_id=account.id,
        selected_video_ids=("v1",),
        store_pool=("s1",),
    )

    with pytest.raises(ValueError, match="store"):
        CommerceService(db_engine).confirm_draft(draft)


def test_confirmed_commerce_snapshot_has_one_scalar_account_and_valid_store(db_engine):
    import json
    from sqlalchemy.orm import Session

    from yijianjifa.features.accounts.models import CapabilitySnapshot
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.commerce.models import CommerceDraft
    from yijianjifa.features.commerce.service import CommerceService, CommerceTaskRow

    accounts = AccountService(db_engine)
    account = accounts.create("douyin", "u-confirm", "店号")
    accounts.update_capabilities(account.id, CapabilitySnapshot(commerce="available", location_scope="local"))
    draft = CommerceDraft(
        account_id=account.id,
        selected_video_ids=("v1",),
        store_pool=("s1",),
        store_assignments={"v1": "s1"},
    )

    task_id = CommerceService(db_engine).confirm_draft(draft)
    with Session(db_engine) as session:
        row = session.get(CommerceTaskRow, task_id)
        payload = json.loads(row.snapshot_json)

    assert payload["account_id"] == account.id
    assert isinstance(payload["account_id"], str)
    assert payload["store_assignments"] == {"v1": "s1"}
