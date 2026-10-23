from yijianjifa.core.selection import FilteredSelection


def test_select_all_uses_full_filtered_id_set_not_visible_page():
    selection = FilteredSelection()
    selection.select_all([f"id-{i}" for i in range(120)])
    assert selection.count == 120
    selection.clear_all([f"id-{i}" for i in range(20, 40)])
    assert selection.count == 100


def test_selected_ids_are_stable_ids_not_page_indexes():
    selection = FilteredSelection()
    selection.select("asset-100")
    selection.select("asset-002")
    assert selection.selected_ids == {"asset-100", "asset-002"}
    selection.clear("asset-100")
    assert selection.selected_ids == {"asset-002"}


def test_right_click_unselected_item_targets_only_that_item():
    selection = FilteredSelection()
    selection.select_all(["A", "B"])
    assert selection.context_targets("C") == {"C"}
    assert selection.selected_ids == {"A", "B"}


def test_right_click_selected_item_targets_whole_selected_set():
    selection = FilteredSelection()
    selection.select_all(["A", "B"])
    assert selection.context_targets("B") == {"A", "B"}
