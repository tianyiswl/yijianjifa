"""Independent review cases for user-visible import and bridge failures."""
import json
import os

from PySide6.QtGui import QImage
from sqlalchemy import event

from yijianjifa.app_services.image_post_commands import ImagePostCommands
from yijianjifa.storage.models import Setting


def test_import_confirmation_failure_does_not_hide_an_already_committed_import(db_engine, app_paths, tmp_path):
    source = tmp_path / 'one.png'
    image = QImage(30, 40, QImage.Format.Format_RGB32)
    image.fill(0xff0088ff)
    assert image.save(str(source))
    commands = ImagePostCommands(db_engine, app_paths)
    staged = commands.files.stage_import([source])
    request = {'v':1,'request_id':'lost_import_receipt','operation':'image_posts.import.confirm',
        'params':{'token':staged['token']}}
    def fail_receipt(_mapper, _connection, target):
        if target.key == 'image_posts.command:lost_import_receipt':
            raise OSError('synthetic receipt storage failure')
    event.listen(Setting, 'before_insert', fail_receipt)
    try:
        failed = commands.dispatch(request)
    finally:
        event.remove(Setting, 'before_insert', fail_receipt)
    assert not failed['ok']
    # A rejected operation must not leave an unacknowledged imported work.
    assert commands.core.list()[1] == 0


def test_image_details_fit_the_existing_bridge_response_budget(db_engine, app_paths, tmp_path):
    source = tmp_path / 'photo.png'
    # No third-party photos: deterministic shape, synthetic random pixels.
    raw = os.urandom(420 * 420 * 3)
    image = QImage(raw, 420, 420, 420 * 3, QImage.Format.Format_RGB888).copy()
    assert image.save(str(source))
    commands = ImagePostCommands(db_engine, app_paths)
    post = commands.core.import_group([source] * 10, title='十张自有图片')
    response = commands.dispatch({'v':1,'request_id':'large_image_detail','operation':'image_posts.get',
        'params':{'id':post.id}})
    assert response['ok']
    actual_bytes = len(json.dumps(response, ensure_ascii=False).encode())
    assert actual_bytes <= 2 * 1024 * 1024, actual_bytes
