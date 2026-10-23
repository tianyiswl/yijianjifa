from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.commerce.distribution import allocate_stores, validate_store_assignments
from yijianjifa.features.commerce.models import CommerceDraft
from yijianjifa.features.commerce.service import CommerceService


def test_single_account_commerce_draft_can_only_confirm_after_store_allocation(db_engine):
    accounts = AccountService(db_engine)
    account = accounts.create("douyin", "accept-commerce", "团购测试号")
    accounts.update_capabilities(
        account.id,
        CapabilitySnapshot(commerce="available", location_scope="local_national"),
    )
    video_ids = ("v1", "v2", "v3")
    pool = ("s1", "s2")
    assignments = allocate_stores(video_ids, pool, {})
    validation = validate_store_assignments(assignments, active_pool=set(pool), manual_overrides={})
    assert validation.ok

    draft = CommerceDraft(
        account_id=account.id,
        selected_video_ids=video_ids,
        store_pool=pool,
        store_assignments={k: v for k, v in assignments.items() if v is not None},
    )
    task_id = CommerceService(db_engine).confirm_draft(draft)
    assert task_id
