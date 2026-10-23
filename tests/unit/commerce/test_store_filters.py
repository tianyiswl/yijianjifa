from yijianjifa.features.commerce.stores import StoreCandidate, StoreFilter, StoreViewModel, filter_stores


def store(store_id: str, *, commission: str, region: str = "上海", name: str | None = None):
    return StoreCandidate(
        id=store_id,
        name=name or f"门店{store_id}",
        region=region,
        scope="local",
        commission=commission,
    )


def test_confirmed_commission_filter_excludes_unknown_and_none():
    rows = [
        store("A", commission="confirmed"),
        store("B", commission="unknown"),
        store("C", commission="none"),
    ]

    result = filter_stores(rows, StoreFilter(commission="confirmed_only"))

    assert [x.id for x in result] == ["A"]


def test_store_select_all_uses_full_filtered_result_not_current_page():
    rows = [store(f"s{i}", commission="confirmed", region="上海") for i in range(75)]
    rows += [store("beijing", commission="confirmed", region="北京")]
    vm = StoreViewModel(rows)
    vm.apply_filter(StoreFilter(region="上海"))

    vm.select_all()

    assert vm.selected_count == 75
    assert "beijing" not in vm.selected_ids


def test_clearing_filtered_scope_preserves_selection_outside_current_filter():
    rows = [
        store("sh", commission="confirmed", region="上海"),
        store("bj", commission="confirmed", region="北京"),
    ]
    vm = StoreViewModel(rows)
    vm.apply_filter(StoreFilter(region="上海"))
    vm.select_all()
    vm.apply_filter(StoreFilter(region="北京"))
    vm.select_all()
    vm.clear_all()

    assert vm.selected_ids == {"sh"}
