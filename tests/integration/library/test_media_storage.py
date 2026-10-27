def test_deleting_original_mode_record_never_unlinks_original(tmp_path, media_service):
    original = tmp_path / "clip.mp4"
    original.write_bytes(b"video")
    asset = media_service.import_media(original, keep_copy=False, category_ids=[])
    media_service.delete_media_record(asset.id)
    assert original.exists()


def test_deleting_managed_copy_unlinks_only_managed_file(tmp_path, media_service):
    original = tmp_path / "clip.mp4"
    original.write_bytes(b"video")
    asset = media_service.import_media(original, keep_copy=True, category_ids=[])
    managed = asset.managed_path
    assert managed is not None and managed.exists()
    media_service.delete_media_record(asset.id)
    assert original.exists()
    assert not managed.exists()


def test_same_content_reimport_reuses_record_without_relocating_source(tmp_path, media_service):
    first_path = tmp_path / "a.mp4"
    first_path.write_bytes(b"same")
    first = media_service.import_media(first_path, keep_copy=False, category_ids=[])
    renamed = tmp_path / "renamed.mp4"
    renamed.write_bytes(b"same")
    second = media_service.import_media(renamed, keep_copy=False, category_ids=[])
    assert second.id == first.id
    assert second.content_hash == first.content_hash
    # Final spec 04.2: V1 does not relocate existing records on reimport.
    assert second.original_path == first_path


def test_import_folder_recurses_and_ignores_unsupported_files(tmp_path, media_service):
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a.mp4").write_bytes(b"a")
    (nested / "b.mov").write_bytes(b"b")
    (nested / "ignore.txt").write_text("x")

    imported = media_service.import_folder(root, recursive=True, keep_copy=False, category_ids=[])
    assert {item.display_name for item in imported} == {"a.mp4", "b.mov"}


def test_reimport_same_media_moves_to_chosen_category_without_duplication(tmp_path, media_service, db_engine):
    from yijianjifa.features.library.copy_service import CategoryRepository

    categories = CategoryRepository(db_engine)
    c1 = categories.create("video", "产品A")
    c2 = categories.create("video", "产品展示")
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"same-category-asset")

    first = media_service.import_media(source, keep_copy=False, category_ids=[c1.id])
    second = media_service.import_media(source, keep_copy=False, category_ids=[c2.id])

    assert first.id == second.id
    assert media_service.category_ids(first.id) == {c2.id}


def test_deleting_category_does_not_delete_media(tmp_path, media_service, db_engine):
    from yijianjifa.features.library.copy_service import CategoryRepository

    categories = CategoryRepository(db_engine)
    category = categories.create("video", "临时分类")
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"category-delete")
    asset = media_service.import_media(source, keep_copy=False, category_ids=[category.id])

    categories.delete(category.id)

    assert media_service.get(asset.id) is not None
    assert media_service.category_ids(asset.id) == set()


def test_filtered_assets_match_any_selected_category_without_duplicates(tmp_path, media_service, db_engine):
    from yijianjifa.features.library.copy_service import CategoryRepository

    categories = CategoryRepository(db_engine)
    c1 = categories.create("video", "A")
    c2 = categories.create("video", "B")
    source = tmp_path / "first.mp4"
    source.write_bytes(b"first")
    first = media_service.import_media(source, keep_copy=False, category_ids=[c1.id])
    second_source = tmp_path / "second.mp4"
    second_source.write_bytes(b"second")
    second = media_service.import_media(second_source, keep_copy=False, category_ids=[c2.id])

    rows = media_service.list_assets(category_ids=[c1.id, c2.id, c1.id])
    assert {row.id for row in rows} == {first.id, second.id}
    assert len(rows) == 2
