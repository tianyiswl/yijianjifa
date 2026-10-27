import json

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.library.copy_service import CategoryRepository
from yijianjifa.features.library.media_storage import MediaStorageService


def call(api, operation, params, request_id):
    return api.dispatch(json.dumps(dict(v=1, request_id=request_id,
                                       operation=operation, params=params)), trusted_origin=True)


def prepare_media(engine, paths, copy_repo, directory, category_id):
    video = directory / 'source.mp4'
    video.write_bytes(b'video fixture; submit does not render')
    MediaStorageService(engine, paths, kind='video').import_media(
        video, keep_copy=False, category_ids=[])
    copy_repo.create(id='C1', title='文案', voice='', body='', tags='', full='')
    track = directory / 'music.wav'
    track.write_bytes(b'music fixture; submit freezes its identity')
    return MediaStorageService(engine, paths, kind='music').import_media(
        track, keep_copy=False, category_ids=[category_id] if category_id else [])


def test_webui_created_music_category_is_valid_for_mix_submission(db_engine, app_paths, copy_repo, tmp_path):
    api = DesktopApi(db_engine, app_paths)
    try:
        created = call(api, 'category.create', {'kind': 'music', 'name': '正式入口音乐分类'}, 'category')
        assert created['ok'], created
        category = created['data']
        assert category['kind'] == 'music'
        track = prepare_media(db_engine, app_paths, copy_repo, tmp_path, category['id'])
        result = call(api, 'mix.submit', {'quantity': 2, 'background_music': {
            'mode': 'random', 'category_ids': [category['id']]}}, 'submit')
        assert result['ok'], result
        snapshot = api._mix.queue.get(result['data']['id']).snapshot
        assert snapshot['music_choices'] == [track.id, track.id]
        assert [item['asset_id'] for item in snapshot['music_sources']] == [track.id]
        assert snapshot['plan']['background_music']['category_ids'] == [category['id']]
        assert CategoryRepository(db_engine).list('audio') == []
    finally:
        api.close(.1)


@pytest.mark.parametrize('kind', ['video', 'copy', 'missing'])
def test_music_scope_never_accepts_unrelated_or_missing_categories(db_engine, app_paths, copy_repo, tmp_path, kind):
    api = DesktopApi(db_engine, app_paths)
    try:
        category_id = 'missing' if kind == 'missing' else CategoryRepository(db_engine).create(kind, '非音乐分类').id
        prepare_media(db_engine, app_paths, copy_repo, tmp_path, None)
        with pytest.raises(ValueError, match='音乐分类已失效'):
            api._mix.submit(quantity=1, background_music={'mode': 'random', 'category_ids': [category_id]})
        assert api._mix.tasks() == []
    finally:
        api.close(.1)
