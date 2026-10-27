import pytest

from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.copy_service import CategoryRepository, CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService


@pytest.mark.parametrize('kind', ['copy', 'video', 'music', 'finished'])
def test_single_category_replaces_and_rejects_multiple_without_losing_original(db_engine, app_paths, tmp_path, kind):
    categories = CategoryRepository(db_engine)
    first = categories.create(kind, 'first')
    second = categories.create(kind, 'second')
    wrong = categories.create('finished' if kind != 'finished' else 'video', 'wrong')
    if kind == 'copy':
        service = CopyRepository(db_engine)
        item = service.create(id='copy', title='title', voice='', body='', tags='', full='')
    else:
        source = tmp_path / ('sample.mp3' if kind == 'music' else 'sample.mp4')
        source.write_bytes(b'fixture')
        if kind == 'finished':
            service = FinishedVideoService(db_engine)
            item = service.import_external(source, duration_ms=1000)
        else:
            service = MediaStorageService(db_engine, app_paths, kind=kind)
            item = service.import_media(source, keep_copy=False, category_ids=[])
    service.set_categories(item.id, [first.id])
    with pytest.raises(ValueError, match='只能选择一个分类'):
        service.set_categories(item.id, [first.id, second.id])
    assert service.category_ids(item.id) == {first.id}
    with pytest.raises(ValueError):
        service.set_categories(item.id, [wrong.id])
    assert service.category_ids(item.id) == {first.id}
    service.set_categories(item.id, [second.id])
    assert service.category_ids(item.id) == {second.id}
    service.set_categories(item.id, [])
    assert service.category_ids(item.id) == set()


@pytest.mark.parametrize('kind', ['video', 'music'])
def test_duplicate_media_import_keeps_existing_unless_a_category_is_chosen(db_engine, app_paths, tmp_path, kind):
    categories = CategoryRepository(db_engine)
    first, second = (categories.create(kind, name) for name in ['first', 'second'])
    source = tmp_path / ('sample.mp3' if kind == 'music' else 'sample.mp4')
    source.write_bytes(b'fixture')
    service = MediaStorageService(db_engine, app_paths, kind=kind)
    item = service.import_media(source, keep_copy=False, category_ids=[first.id])
    assert service.import_media(source, keep_copy=False, category_ids=[]).id == item.id
    assert service.category_ids(item.id) == {first.id}
    service.import_media(source, keep_copy=False, category_ids=[second.id])
    assert service.category_ids(item.id) == {second.id}
    service.append_categories(item.id, [first.id])
    assert service.category_ids(item.id) == {first.id}
    with pytest.raises(ValueError, match='只能选择一个分类'):
        service.import_media(source, keep_copy=True, category_ids=[first.id, second.id])
    assert service.category_ids(item.id) == {first.id}


def test_music_accepts_legacy_audio_category_without_allowing_multiple(db_engine, app_paths, tmp_path):
    categories = CategoryRepository(db_engine)
    legacy = categories.create('audio', 'legacy music')
    current = categories.create('music', 'current music')
    source = tmp_path / 'legacy.mp3'
    source.write_bytes(b'fixture')
    service = MediaStorageService(db_engine, app_paths, kind='music')

    item = service.import_media(source, keep_copy=False, category_ids=[legacy.id])
    assert service.category_ids(item.id) == {legacy.id}
    service.set_categories(item.id, [current.id])
    service.set_categories(item.id, [legacy.id])
    assert service.category_ids(item.id) == {legacy.id}
    with pytest.raises(ValueError, match='只能选择一个分类'):
        service.set_categories(item.id, [legacy.id, current.id])
    assert service.category_ids(item.id) == {legacy.id}
