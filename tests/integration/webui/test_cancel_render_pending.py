"""Exercise cancellation through the real receipt transaction and post-commit path."""
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.mixing.ffmpeg_renderer import RenderResult


def call(api, task_id, request_id='cancel-pending'):
    return api.dispatch(json.dumps({'v': 1, 'request_id': request_id,
                                   'operation': 'task.cancel', 'params': {'id': task_id}}),
                        trusted_origin=True)


@pytest.fixture
def interrupted_render(db_engine, app_paths, tmp_path, monkeypatch):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'input fixture')
    MediaStorageService(db_engine, app_paths, kind='video').import_media(
        source, keep_copy=False, category_ids=[])
    CopyRepository(db_engine).create(id='C1', title='frozen title', voice='', body='', tags='', full='')
    api = DesktopApi(db_engine, app_paths)
    api._mix.probe = lambda path: 2000

    class Renderer:
        def render(self, request):
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(b'completed but not registered')
            return RenderResult(request.output_path, sum(s.duration_ms for s in request.segments), 720, 1280)

    api._mix.executor.renderer = Renderer()
    task = api._mix.submit(quantity=1)

    def fail_commit(*args, **kwargs):
        raise RuntimeError('injected success transaction failure')

    monkeypatch.setattr(api._mix.executor, '_commit_success', fail_commit)
    api._mix.queue.run_once_until_idle()
    assert api._mix.queue.get(task.id).state == 'interrupted'
    key = f'mix-pending:{task.id}:0'
    with db_engine.connect() as connection:
        pending = json.loads(connection.execute(text('SELECT value_json FROM settings WHERE key=:key'),
                                                {'key': key}).scalar_one())
    output = Path(pending['output'])
    assert output.read_bytes() == b'completed but not registered'
    unrelated = output.parent/'unrelated.mp4'
    unrelated.write_bytes(b'keep existing output')
    try:
        yield api, task, key, output, unrelated
    finally:
        api.close(.2)


def pending_exists(engine, key):
    with engine.connect() as connection:
        return connection.execute(text('SELECT count(*) FROM settings WHERE key=:key'),
                                  {'key': key}).scalar_one() == 1


def assert_no_success(engine):
    with engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM finished_videos').scalar_one() == 0
        assert connection.exec_driver_sql('SELECT count(*) FROM usage_progress').scalar_one() == 0
        assert connection.exec_driver_sql("SELECT count(*) FROM historical_identities WHERE identity_kind='mix:composition'").scalar_one() == 0


def test_api_cancel_commits_then_cleans_pending_and_replays_idempotently(db_engine, interrupted_render):
    api, task, key, output, unrelated = interrupted_render
    result = call(api, task.id)
    assert result['ok'] and result['data']['state'] == 'cancelled', result
    assert api._mix.queue.get(task.id).state == 'cancelled'
    assert not output.exists(), 'API cancellation returned success without cleaning its committed pending output'
    assert not pending_exists(db_engine, key)
    assert unrelated.read_bytes() == b'keep existing output'
    assert call(api, task.id) == result
    with db_engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM web_command_receipts').scalar_one() == 1
    assert_no_success(db_engine)


def test_cancel_receipt_rollback_keeps_pending_and_bytes(db_engine, interrupted_render):
    api, task, key, output, unrelated = interrupted_render
    with db_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_cancel_receipt BEFORE INSERT ON web_command_receipts BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END")
    for initial_state in ('interrupted', 'cancelled'):
        with db_engine.begin() as connection:
            connection.execute(text('UPDATE mix_tasks SET state=:state WHERE id=:id'),
                               {'id': task.id, 'state': initial_state})
        result = call(api, task.id)
        assert not result['ok']
        assert api._mix.queue.get(task.id).state == initial_state
        assert output.read_bytes() == b'completed but not registered'
        assert pending_exists(db_engine, key)
        assert unrelated.read_bytes() == b'keep existing output'
    with db_engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM web_command_receipts').scalar_one() == 0
    assert_no_success(db_engine)


def test_receipt_replay_retries_post_commit_cleanup_after_database_failure(
    db_engine, interrupted_render, monkeypatch
):
    api, task, key, output, unrelated = interrupted_render
    cleanup = api._mix.executor.discard_cancelled

    def unavailable(*args):
        raise OperationalError('cleanup pending', {}, RuntimeError('database unavailable'))

    monkeypatch.setattr(api._mix.executor, 'discard_cancelled', unavailable)
    result = call(api, task.id)
    assert not result['ok'], 'post-commit database failure must not be reported as cleanup success'
    assert result['error']['code'] == 'cancel_cleanup_unknown'
    assert result['error']['retryable'] and '取消已保存' in result['error']['message']
    assert api._mix.queue.get(task.id).state == 'cancelled'
    assert output.is_file() and pending_exists(db_engine, key)
    with db_engine.connect() as connection:
        receipt = json.loads(connection.exec_driver_sql('SELECT response_json FROM web_command_receipts').scalar_one())
    assert receipt['ok'] and receipt['data']['state'] == 'cancelled'
    monkeypatch.setattr(api._mix.executor, 'discard_cancelled', cleanup)
    assert call(api, task.id) == receipt
    assert not output.exists() and not pending_exists(db_engine, key)
    assert unrelated.read_bytes() == b'keep existing output'
    assert_no_success(db_engine)


def test_permission_pending_cleanup_retries_only_for_matching_cancel_receipt(
    db_engine, interrupted_render, monkeypatch
):
    api, task, key, output, unrelated = interrupted_render
    original_unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == output:
            raise PermissionError('injected output permission failure')
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', denied)
    first = call(api, task.id)
    assert first['ok']
    assert api._mix.queue.get(task.id).last_error
    assert output.is_file() and pending_exists(db_engine, key)
    monkeypatch.setattr(Path, 'unlink', original_unlink)
    other = api._mix.submit(quantity=1)
    conflict = call(api, other.id)
    assert conflict['error']['code'] == 'request_conflict'
    assert api._mix.queue.get(other.id).state == 'waiting'
    assert output.is_file() and pending_exists(db_engine, key)
    assert call(api, task.id) == first
    assert not output.exists() and not pending_exists(db_engine, key)
    assert unrelated.read_bytes() == b'keep existing output'
    assert_no_success(db_engine)
