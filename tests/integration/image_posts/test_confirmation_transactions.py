"""Receipt and image import commit together; retry does not create hidden works."""
from openpyxl import Workbook
from PySide6.QtGui import QImage
from sqlalchemy import event

from yijianjifa.app_services.image_post_commands import ImagePostCommands
from yijianjifa.features.image_posts.files import METADATA_COLUMNS
from yijianjifa.storage.models import Setting


def source(path):
    image = QImage(30, 40, QImage.Format.Format_RGB32)
    image.fill(0xff0088ff)
    assert image.save(str(path))
    return path


def command(op, rid, token):
    return {'v': 1, 'request_id': rid, 'operation': op, 'params': {'token': token}}


def test_failed_command_receipt_rolls_back_and_same_token_retries_once(db_engine, app_paths, tmp_path):
    commands = ImagePostCommands(db_engine, app_paths)
    staged = commands.files.stage_import([source(tmp_path / 'one.png')])
    request = command('image_posts.import.confirm', 'receipt_failure_then_retry', staged['token'])
    def fail(_mapper, _connection, row):
        if row.key == 'image_posts.command:receipt_failure_then_retry':
            raise OSError('synthetic receipt failure')
    event.listen(Setting, 'before_insert', fail)
    try:
        assert not commands.dispatch(request)['ok']
    finally:
        event.remove(Setting, 'before_insert', fail)
    assert commands.core.list()[1] == 0
    assert not list(commands.core.root.rglob('*.jpg'))
    accepted = commands.dispatch(request)
    assert accepted['ok'] and accepted['data']['imported'] == 1
    assert commands.dispatch(request) == accepted
    assert commands.core.list()[1] == 1


def test_successful_token_recovers_after_restart_with_different_request_id(db_engine, app_paths, tmp_path):
    commands = ImagePostCommands(db_engine, app_paths)
    staged = commands.files.stage_import([source(tmp_path / 'one.png')])
    accepted = commands.dispatch(command('image_posts.import.confirm', 'first_confirmation', staged['token']))
    assert accepted['ok'] and accepted['data']['imported'] == 1
    restarted = ImagePostCommands(db_engine, app_paths)
    restored = restarted.dispatch(command('image_posts.import.confirm', 'retry_after_restart', staged['token']))
    assert restored['ok'] and restored['data'] == accepted['data']
    assert restarted.core.list()[1] == 1


def test_failed_metadata_receipt_rolls_back_and_preserves_retry(db_engine, app_paths, tmp_path):
    commands = ImagePostCommands(db_engine, app_paths)
    post = commands.core.import_group([source(tmp_path / 'one.png')], display_name='产品A', title='原文案')
    workbook = Workbook()
    workbook.active.append(list(METADATA_COLUMNS))
    workbook.active.append(['产品A', '新文案', '正文', '#产品'])
    xlsx = tmp_path / 'metadata.xlsx'
    workbook.save(xlsx)
    staged = commands.files.stage_metadata([xlsx])
    request = command('image_posts.metadata.confirm', 'metadata_receipt_failure', staged['token'])
    def fail(_mapper, _connection, row):
        if row.key == 'image_posts.command:metadata_receipt_failure':
            raise OSError('synthetic receipt failure')
    event.listen(Setting, 'before_insert', fail)
    try:
        assert not commands.dispatch(request)['ok']
    finally:
        event.remove(Setting, 'before_insert', fail)
    assert commands.core.get(post.id).title == '原文案'
    accepted = commands.dispatch(request)
    assert accepted['ok'] and accepted['data']['updated'] == 1
    assert commands.core.get(post.id).title == '新文案'
    restarted = ImagePostCommands(db_engine, app_paths)
    restored = restarted.dispatch(command('image_posts.metadata.confirm', 'metadata_recovered', staged['token']))
    assert restored['data'] == accepted['data']
