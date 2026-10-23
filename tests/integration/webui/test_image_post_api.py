import json
from pathlib import Path

from PySide6.QtGui import QImage

from yijianjifa.app_services.desktop_api import DesktopApi


def call(api, operation, params, request_id='image_api_read'):
    return api.dispatch(json.dumps({'v': 1, 'request_id': request_id, 'operation': operation, 'params': params}), trusted_origin=True)


def test_image_post_list_is_available_without_changing_video_list(db_engine, app_paths):
    api = DesktopApi(db_engine, app_paths)
    try:
        result = call(api, 'image_posts.list', {'query': '', 'offset': 0, 'limit': 20})
        assert result['ok'], result
        assert result['data'] == {'items': [], 'total': 0}
        videos = call(api, 'finished.list', {'query': '', 'origin': 'all', 'offset': 0, 'limit': 20})
        assert videos['ok'] and videos['data']['total'] == 0
    finally:
        api.close(.1)


def test_image_post_api_never_accepts_js_file_paths(db_engine, app_paths):
    api = DesktopApi(db_engine, app_paths)
    try:
        for operation, params in [('image_posts.get', {'id': '../secret'}),
                                  ('image_posts.import.confirm', {'token': 'x', 'paths': ['/tmp/private.png']}),
                                  ('image_posts.list', {'query': '', 'offset': -1, 'limit': 20})]:
            assert not call(api, operation, params)['ok']
    finally:
        api.close(.1)


def test_image_post_dto_exposes_thumbnails_not_private_paths(db_engine, app_paths, tmp_path):
    from yijianjifa.features.image_posts.service import ImagePostService
    image = QImage(32, 48, QImage.Format.Format_RGB32)
    image.fill(0xff435ff5)
    source = tmp_path / '私有路径.png'
    assert image.save(str(source))
    post = ImagePostService(db_engine, app_paths).import_group([source], title='公开标题')
    api = DesktopApi(db_engine, app_paths)
    try:
        result = call(api, 'image_posts.get', {'id': post.id})
        assert result['ok'], result
        data = result['data']
        assert data['title'] == '公开标题' and data['image_count'] == 1
        assert data['images'][0]['thumbnail_url'].startswith('data:image/jpeg;base64,')
        assert str(tmp_path) not in json.dumps(result)
        saved = call(api, 'image_posts.save', {'id': post.id, 'display_name': '图文一', 'title': '新标题', 'body': '正文', 'tags': '#测试', 'image_ids': [post.images[0].id]}, 'save_image_post')
        assert saved['ok'], saved
        assert call(api, 'image_posts.save', {'id': post.id, 'display_name': '图文一', 'title': '新标题', 'body': '正文', 'tags': '#测试', 'image_ids': [post.images[0].id]}, 'save_image_post') == saved
        unnamed = call(api, 'image_posts.save', {'id': post.id, 'display_name': ' ', 'title': '仅改标题', 'body': '', 'tags': '', 'image_ids': [post.images[0].id]}, 'save_without_name')
        assert unnamed['ok'] and unnamed['data']['display_name'] == '图文一'
    finally:
        api.close(.1)
