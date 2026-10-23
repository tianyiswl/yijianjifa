from yijianjifa.features.library.excel_import import EXPECTED_COLUMNS, classify_rows


def row(copy_id="", title="A", voice="v", body="B", tags="x", full=""):
    return dict(zip(EXPECTED_COLUMNS, [copy_id, title, voice, body, tags, full]))


def test_existing_id_changed_content_is_update_not_auto_overwrite(copy_repo):
    copy_repo.create(id="W001", title="A", voice="old", body="B", tags="x", full="")
    preview = classify_rows(copy_repo, [row("W001", voice="new")])
    assert [x.id for x in preview.updates] == ["W001"]
    assert copy_repo.get("W001").voice == "old"


def test_error_rows_do_not_block_valid_rows(copy_repo):
    preview = classify_rows(
        copy_repo,
        [
            row("W100", title="conflict A"),
            row("W100", title="conflict B"),
            row("", title="valid new"),
        ],
    )
    assert len(preview.errors) == 2
    assert len(preview.new) == 1
    assert preview.new[0].title == "valid new"


def test_blank_id_duplicate_uses_complete_content_not_title_only(copy_repo):
    copy_repo.create(id="W001", title="same", voice="v1", body="B", tags="x", full="")
    preview = classify_rows(copy_repo, [row("", title="same", voice="v2")])
    assert len(preview.new) == 1
    assert not preview.duplicate


def test_exact_same_existing_id_is_duplicate(copy_repo):
    copy_repo.create(id="W001", title="A", voice="v", body="B", tags="x", full="")
    preview = classify_rows(copy_repo, [row("W001")])
    assert [x.id for x in preview.duplicate] == ["W001"]
    assert not preview.updates
