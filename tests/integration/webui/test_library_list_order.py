"""Display order is newest first without changing the mixing repositories."""
from datetime import datetime, timedelta
import json

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Category, CategoryLink, CopyRecord, MediaAsset


@pytest.fixture(params=['video', 'music', 'copy'])
def library_rows(request, db_engine, app_paths, tmp_path):
    kind = request.param
    with session_scope(db_engine) as session:
        session.add(Category(id='selected-category', kind=kind, name='Selected'))
        for identity, day in zip('ABCDE', (1, 4, 2, 4, 3)):
            created = datetime(2020, 1, day, 12)
            # Updated times deliberately disagree with creation order.
            values = dict(id=identity, created_at=created, updated_at=datetime(2030, 1, 10) - timedelta(days=day))
            name = ('kept-' if identity in 'ABE' else 'other-') + identity
            if kind == 'copy':
                session.add(CopyRecord(**values, title=name, voice='', body='', tags='', full=''))
            else:
                path = tmp_path / (identity + ('.mp3' if kind == 'music' else '.mp4'))
                path.write_bytes(identity.encode())
                session.add(MediaAsset(**values, kind=kind, display_name=name, original_path=str(path),
                                       content_hash=identity * 64, missing=False))
            if identity in 'BCE':
                session.add(CategoryLink(category_id='selected-category', target_type=kind, target_id=identity))
    api = DesktopApi(db_engine, app_paths)
    def call(**values):
        params = dict(kind=kind, query='', category_ids=[], offset=0, limit=2)
        params.update(values)
        result = api.dispatch(json.dumps(dict(v=1, request_id='ordered-read', operation='library.list', params=params)), trusted_origin=True)
        assert result['ok'], result
        return result['data']
    yield kind, call
    assert api.close(.1)


def test_newest_creation_order_is_global_across_pages_and_stable_for_ties(library_rows, db_engine, app_paths):
    kind, call = library_rows
    def original_order():
        records = CopyRepository(db_engine).list_all() if kind == 'copy' else MediaStorageService(db_engine, app_paths, kind=kind).list_assets()
        return [record.id for record in records]
    expected_original = list('ABCDE' if kind == 'copy' else 'ACEBD')
    assert original_order() == expected_original
    pages = [call(offset=offset) for offset in (0, 2, 4, 6)]
    assert all(page['total'] == 5 for page in pages)
    assert [[item['id'] for item in page['items']] for page in pages] == [list('BD'), list('EC'), ['A'], []]
    assert [item['id'] for item in call()['items']] == list('BD')
    assert original_order() == expected_original


def test_filters_are_applied_before_global_newest_pagination(library_rows):
    _, call = library_rows
    assert [item['id'] for item in call(query='kept', offset=1)['items']] == list('EA')
    categories = dict(category_ids=['selected-category'])
    assert [item['id'] for item in call(**categories)['items']] == list('BE')
    assert [item['id'] for item in call(**categories, offset=2)['items']] == ['C']
    assert call(query='no-match')['items'] == []
    assert call(category_ids=['__none__'])['total'] == 2
    assert [item['id'] for item in call(category_ids=['__none__'])['items']] == list('DA')


@pytest.mark.parametrize('kind', ['video', 'music', 'copy'])
def test_empty_library_still_returns_an_empty_page(kind, db_engine, app_paths):
    api = DesktopApi(db_engine, app_paths)
    try:
        params = dict(kind=kind, query='', category_ids=[], offset=0, limit=20)
        result = api.dispatch(json.dumps(dict(v=1, request_id='empty-read', operation='library.list', params=params)), trusted_origin=True)
        assert result['ok'] and result['data'] == {'items': [], 'total': 0}
    finally:
        assert api.close(.1)
