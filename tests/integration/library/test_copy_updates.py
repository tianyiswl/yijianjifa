from yijianjifa.features.library.copy_service import CategoryRepository
from yijianjifa.features.library.excel_import import EXPECTED_COLUMNS, apply_import, classify_rows


def _row(copy_id, voice):
    return dict(zip(EXPECTED_COLUMNS, [copy_id, "A", voice, "B", "x", ""]))


def test_applying_approved_update_preserves_categories(copy_repo, db_engine):
    categories = CategoryRepository(db_engine)
    category = categories.create("copy", "产品A")
    copy_repo.create(id="W001", title="A", voice="old", body="B", tags="x", full="")
    copy_repo.set_categories("W001", [category.id])

    preview = classify_rows(copy_repo, [_row("W001", "new")])
    result = apply_import(copy_repo, preview, approved_update_ids={"W001"})

    assert result.updated_ids == ["W001"]
    assert copy_repo.get("W001").voice == "new"
    assert copy_repo.category_ids("W001") == {category.id}


def test_unapproved_update_is_not_applied(copy_repo):
    copy_repo.create(id="W001", title="A", voice="old", body="B", tags="x", full="")
    preview = classify_rows(copy_repo, [_row("W001", "new")])
    result = apply_import(copy_repo, preview, approved_update_ids=set())
    assert result.updated_ids == []
    assert copy_repo.get("W001").voice == "old"
