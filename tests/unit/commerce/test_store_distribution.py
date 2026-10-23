from yijianjifa.features.commerce.distribution import allocate_stores, validate_store_assignments


def test_many_stores_are_assigned_roughly_evenly():
    plan = allocate_stores(["v1", "v2", "v3", "v4"], ["s1", "s2"], {})

    assert list(plan.values()).count("s1") == 2
    assert list(plan.values()).count("s2") == 2


def test_one_store_is_used_for_all_videos():
    plan = allocate_stores(["v1", "v2", "v3"], ["s1"], {})
    assert plan == {"v1": "s1", "v2": "s1", "v3": "s1"}


def test_manual_override_survives_rebalance():
    plan = allocate_stores(
        ["v1", "v2", "v3", "v4"],
        ["s1", "s2"],
        {"v2": "manual"},
    )

    assert plan["v2"] == "manual"
    assert set(plan) == {"v1", "v2", "v3", "v4"}


def test_removed_manual_store_blocks_instead_of_reassigning():
    result = validate_store_assignments(
        {"v1": "manual-gone"},
        active_pool={"s1", "s2"},
        manual_overrides={"v1": "manual-gone"},
    )

    assert result.blocking_video_ids == {"v1"}


def test_missing_store_assignment_is_blocking():
    result = validate_store_assignments(
        {"v1": None, "v2": "s1"},
        active_pool={"s1"},
        manual_overrides={},
    )
    assert result.blocking_video_ids == {"v1"}
