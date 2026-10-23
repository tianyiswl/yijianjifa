from yijianjifa.features.mixing.output_names import output_filename
"""Real SIGKILL boundaries between media, success transaction and queue counters.

Set P011_CRASH_EVIDENCE_DIR to retain isolated databases, files and JSON readbacks.
No native UI or platform provider is used.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest
from sqlalchemy import select

from yijianjifa.features.finished.service import FinishedVideo
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.mixing.desktop_service import DesktopMixService
from yijianjifa.features.mixing.ffmpeg_renderer import FFmpegRenderer
from yijianjifa.os_integration.paths import AppPaths
from yijianjifa.os_integration.resources import ffmpeg_path
from yijianjifa.storage.db import create_engine_for, migrate_to_head, session_scope
from yijianjifa.storage.models import HistoricalIdentity, Setting, UsageProgress


CHILD = r'''
import json, signal, sys
from pathlib import Path
from contextlib import contextmanager
from yijianjifa.features.mixing import desktop_service as module
from yijianjifa.os_integration.paths import AppPaths
from yijianjifa.storage.db import create_engine_for

root, task_id, boundary = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
data = root / 'data'
paths = AppPaths(data, data/'cache', data/'logs', data/'managed_media')
engine = create_engine_for(data/'app.db')
service = module.DesktopMixService(engine, paths)

def stop_at_boundary():
    (root/'boundary.json').write_text(json.dumps({'boundary': boundary, 'task_id': task_id}))
    while True:
        signal.pause()

if boundary == 'prepared_before_render':
    original_render = service.executor.renderer.render
    def render(request):
        stop_at_boundary()
        return original_render(request)
    service.executor.renderer.render = render
elif boundary == 'render_before_ready_commit':
    original_save = service.executor._save_pending
    def save(task_id, index, pending):
        if pending['phase'] == 'ready':
            stop_at_boundary()
        return original_save(task_id, index, pending)
    service.executor._save_pending = save
elif boundary == 'file_before_success_commit':
    # Pending/ready records intentionally use earlier transactions. Stop only
    # when the complete formal file is about to enter the success transaction.
    original_commit = service.executor._commit_success
    def commit(*args, **kwargs):
        stop_at_boundary()
        return original_commit(*args, **kwargs)
    service.executor._commit_success = commit
else:
    original_item = service.executor.render_item
    def item(*args, **kwargs):
        value = original_item(*args, **kwargs)
        if sys.argv[4] == 'cancel':
            service.queue.cancel_remaining(task_id)
        stop_at_boundary()
        return value
    service.executor.render_item = item
service.queue.run_once_until_idle()
'''


def _state(service, task_id):
    with session_scope(service.engine) as session:
        finished = list(session.scalars(select(FinishedVideo)))
        settings = list(session.scalars(select(Setting).where(
            Setting.key.like(f'mix-%:{task_id}:%'))))
        progress = list(session.scalars(select(UsageProgress)))
        history = list(session.scalars(select(HistoricalIdentity).where(
            HistoricalIdentity.identity_kind == 'mix:composition')))
        return {
            'task': asdict(service.queue.get(task_id)),
            'finished': [{'id': r.id, 'path': r.stored_path, 'sha256': r.content_hash,
                          'file_sha256': sha256_file(Path(r.stored_path)), 'copy_id': r.copy_id}
                         for r in finished],
            'receipts': {r.key: json.loads(r.value_json) for r in settings},
            'progress': [{'scope': r.scope, 'copy_id': r.copy_id,
                          'cycle': r.cycle, 'order': r.used_order} for r in progress],
            'history': [r.content_hash for r in history],
        }


def _crash_and_recover(tmp_path, boundary, *, quantity=2, cancel=False):
    evidence = os.environ.get('P011_CRASH_EVIDENCE_DIR')
    root = (Path(evidence) / f'{boundary}-{uuid.uuid4().hex[:8]}') if evidence else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    data = root / 'data'
    paths = AppPaths(data, data/'cache', data/'logs', data/'managed_media')
    for path in asdict(paths).values():
        path.mkdir(parents=True, exist_ok=True)
    engine = create_engine_for(data/'app.db')
    migrate_to_head(engine)
    source = root/'source.mp4'
    subprocess.run([str(ffmpeg_path()), '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=s=128x128:r=25:d=2', '-c:v', 'libx264', str(source)],
                   check=True, capture_output=True)
    MediaStorageService(engine, paths, kind='video').import_media(
        source, keep_copy=False, category_ids=[])
    copies = CopyRepository(engine)
    for identity in ('A', 'B'):
        copies.create(id=identity, title=f'冻结-{identity}', voice='', body='', tags='', full='')
    service = DesktopMixService(engine, paths)
    task = service.submit(quantity=quantity, total_ms=600, segment_ms=600,
                          resolution='720p', frame_fit='crop_fill', keep_original_audio=False)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[3]/'src'))
    process = subprocess.Popen([sys.executable, '-c', CHILD, str(root), task.id, boundary,
                                'cancel' if cancel else 'continue'],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not (root/'boundary.json').exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail('media worker did not reach the selected transaction boundary')
            time.sleep(.02)
        assert process.poll() is None, process.communicate(timeout=5)[1].decode()
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    output = Path(task.snapshot['output_directory'])/task.id/output_filename(task.snapshot, 0)
    before = _state(service, task.id)
    pending = before['receipts'].get(f'mix-pending:{task.id}:0')
    formal_existed = output.exists()
    before_path = output if output.exists() else Path(pending['attempt']) if pending else output
    original_hash = sha256_file(before_path) if before_path.exists() else None
    service.hold_after_restart()
    held = _state(service, task.id)
    service.hold_after_restart()
    assert _state(service, task.id) == held, 'repeated startup reconciliation must be idempotent'

    class ObservedRenderer(FFmpegRenderer):
        def __init__(self):
            self.calls = []
        def render(self, request):
            self.calls.append(str(request.output_path))
            return super().render(request)

    renderer = ObservedRenderer()
    service.executor.renderer = renderer
    assert not renderer.calls
    if held['task']['state'] in {'paused', 'interrupted'}:
        service.queue.resume(task.id)
        service.queue.run_once_until_idle()
    after = _state(service, task.id)
    report = {
        'boundary': boundary, 'cancel_requested': cancel, 'worker_exit': process.returncode,
        'worker_stderr': stderr.decode(), 'source_sha256': sha256_file(source),
        'formal_existed_before_restart': formal_existed,
        'artifact_before_restart_path': str(before_path),
        'formal_before_restart_sha256': original_hash,
        'formal_after_resume_sha256': sha256_file(output),
        'retry_render_paths': renderer.calls, 'before_restart': before,
        'after_hold': held, 'after_explicit_resume': after,
    }
    (root/'readback.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    engine.dispose()
    return report


@pytest.mark.skipif(os.name != 'posix', reason='POSIX SIGKILL crash boundary')
@pytest.mark.parametrize(('quantity', 'cancel'), [(2, False), (1, False), (2, True)])
def test_committed_success_is_visible_after_restart_without_reexecuting(tmp_path, quantity, cancel):
    report = _crash_and_recover(tmp_path, 'success_before_queue_commit',
                                quantity=quantity, cancel=cancel)
    before, held, after = (report[key] for key in
                           ('before_restart', 'after_hold', 'after_explicit_resume'))
    assert len(before['finished']) == len(before['history']) == len(before['progress']) == 1
    assert before['task']['completed_count'] == 0
    assert held['task']['auto_resume'] is False
    expected_count = 1 if cancel else quantity
    assert len(report['retry_render_paths']) == expected_count - 1
    assert after['task']['success_count'] == expected_count
    assert len(after['finished']) == len(after['history']) == len(after['progress']) == expected_count
    assert report['formal_before_restart_sha256'] == report['formal_after_resume_sha256']
    assert held['task']['completed_count'] == held['task']['success_count'] == 1
    assert held['receipts'][f"mix-result:{held['task']['id']}:0"]['state'] == 'completed'
    if cancel:
        assert held['task']['state'] == 'cancelled'
    elif quantity == 1:
        assert held['task']['state'] == 'completed'
    else:
        assert held['task']['state'] in {'paused', 'interrupted'}


@pytest.mark.skipif(os.name != 'posix', reason='POSIX SIGKILL crash boundary')
def test_verified_output_before_success_commit_is_adopted_without_rendering_again(tmp_path):
    report = _crash_and_recover(tmp_path, 'file_before_success_commit')
    before, held, after = (report[key] for key in
                           ('before_restart', 'after_hold', 'after_explicit_resume'))
    assert not before['finished'] and not before['history'] and not before['progress']
    assert held['task']['auto_resume'] is False
    assert after['task']['success_count'] == 2
    assert len(after['finished']) == len(after['history']) == len(after['progress']) == 2
    assert report['formal_before_restart_sha256'] == report['formal_after_resume_sha256']
    assert len(report['retry_render_paths']) == 1, 'verified first output was rendered a second time'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX SIGKILL crash boundary')
@pytest.mark.parametrize('boundary', ['prepared_before_render', 'render_before_ready_commit'])
def test_pending_render_crash_boundaries_hold_until_explicit_resume(tmp_path, boundary):
    report = _crash_and_recover(tmp_path, boundary)
    held, after = report['after_hold'], report['after_explicit_resume']
    assert held['task']['completed_count'] == 0 and held['task']['auto_resume'] is False
    assert not held['finished'] and not held['history'] and not held['progress']
    expected = 2 if boundary == 'prepared_before_render' else 1
    assert len(report['retry_render_paths']) == expected
    assert after['task']['success_count'] == 2
    assert len(after['finished']) == len(after['history']) == len(after['progress']) == 2
    assert not any(k.startswith('mix-pending:') for k in after['receipts'])
    if boundary == 'render_before_ready_commit':
        assert report['formal_before_restart_sha256'] == report['formal_after_resume_sha256']


def test_success_transaction_failure_preserves_output_and_replays_without_new_render(
    db_engine, app_paths, tmp_path, monkeypatch
):
    from test_desktop_service import service_for
    service, renderer, _, _ = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    original = service.executor._mark_used
    monkeypatch.setattr(service.executor, '_mark_used', lambda *args: (_ for _ in ()).throw(
        RuntimeError('injected database transaction failure')))
    service.queue.run_once_until_idle()
    held = _state(service, task.id)
    assert held['task']['state'] == 'interrupted' and held['task']['completed_count'] == 0
    assert not held['finished'] and not held['history'] and not held['progress']
    pending = held['receipts'][f'mix-pending:{task.id}:0']
    assert sha256_file(Path(pending['output'])) == pending['content_hash']
    monkeypatch.setattr(service.executor, '_mark_used', original)
    service.queue.resume(task.id)
    service.queue.run_once_until_idle()
    after = _state(service, task.id)
    assert len(renderer.requests) == 1
    assert after['task']['success_count'] == 1
    assert len(after['finished']) == len(after['history']) == len(after['progress']) == 1


def test_unowned_formal_file_is_not_overwritten_or_adopted(db_engine, app_paths, tmp_path):
    from test_desktop_service import service_for
    service, renderer, _, _ = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    output = Path(task.snapshot['output_directory']) / task.id / output_filename(task.snapshot, 0)
    output.parent.mkdir(parents=True)
    output.write_bytes(b'unrelated existing file')
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).state == 'interrupted'
    assert output.read_bytes() == b'unrelated existing file' and not renderer.requests
    readback = _state(service, task.id)
    assert not readback['finished'] and not readback['history'] and not readback['progress']


def test_cancel_pending_registration_removes_only_owned_unregistered_file(
    db_engine, app_paths, tmp_path, monkeypatch
):
    from test_desktop_service import service_for
    service, renderer, _, _ = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    monkeypatch.setattr(service.executor, '_commit_success', lambda *args: (_ for _ in ()).throw(
        RuntimeError('injected database transaction failure')))
    service.queue.run_once_until_idle()
    before = _state(service, task.id)
    pending = before['receipts'][f'mix-pending:{task.id}:0']
    output = Path(pending['output'])
    assert output.is_file()
    unrelated = output.parent/'other.mp4'
    unrelated.write_bytes(b'keep unrelated file')
    service.queue.cancel_remaining(task.id)
    assert service.queue.get(task.id).state == 'cancelled'
    assert not output.exists() and unrelated.read_bytes() == b'keep unrelated file'
    assert f'mix-pending:{task.id}:0' not in _state(service, task.id)['receipts']
    assert len(renderer.requests) == 1


def test_renderer_failure_discards_pending_attempt_without_advancing_usage(
    db_engine, app_paths, tmp_path, monkeypatch
):
    from test_desktop_service import service_for
    service, renderer, _, _ = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    def fail(request):
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b'incomplete media')
        raise ValueError('injected render failure')
    monkeypatch.setattr(renderer, 'render', fail)
    service.queue.run_once_until_idle()
    after = _state(service, task.id)
    assert after['task']['failure_count'] == 1
    assert not after['finished'] and not after['history'] and not after['progress']
    assert not any(k.startswith('mix-pending:') for k in after['receipts'])
    assert not list((Path(task.snapshot['output_directory'])/task.id).glob('*.mp4'))


def test_invalid_pending_composition_is_neither_adopted_nor_deleted_on_cancel(
    db_engine, app_paths, tmp_path, monkeypatch
):
    from test_desktop_service import service_for
    service, renderer, _, _ = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    monkeypatch.setattr(service.executor, '_commit_success', lambda *args: (_ for _ in ()).throw(
        RuntimeError('injected database transaction failure')))
    service.queue.run_once_until_idle()
    key = f'mix-pending:{task.id}:0'
    with session_scope(db_engine) as session:
        row = session.get(Setting, key)
        pending = json.loads(row.value_json)
        pending['fingerprint'] = '0' * 64
        row.value_json = json.dumps(pending)
    output = Path(pending['output'])
    original_hash = sha256_file(output)
    service.queue.resume(task.id)
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).state == 'interrupted'
    assert len(renderer.requests) == 1
    service.queue.cancel_remaining(task.id)
    service.hold_after_restart()
    assert sha256_file(output) == original_hash
    readback = _state(service, task.id)
    assert readback['task']['state'] == 'cancelled' and readback['task']['last_error']
    assert key in readback['receipts']
    assert not readback['finished'] and not readback['progress']


def test_startup_does_not_trust_receipt_for_an_unrelated_finished_file(
    db_engine, app_paths, tmp_path
):
    from test_desktop_service import service_for
    from yijianjifa.features.finished.service import FinishedVideoService
    service, renderer, _, _ = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    unrelated = tmp_path/'unrelated.mp4'
    unrelated.write_bytes(b'existing unrelated output')
    finished = FinishedVideoService(db_engine).register_generated(unrelated, duration_ms=1000)
    with session_scope(db_engine) as session:
        session.add(Setting(key=f'mix-success:{task.id}:0', value_json=json.dumps(
            {'finished_id': finished.id, 'path': str(unrelated)})))
    service.hold_after_restart()
    record = service.queue.get(task.id)
    assert record.state == 'paused' and record.completed_count == 0
    assert not service.item_results(task.id) and not renderer.requests
    assert unrelated.read_bytes() == b'existing unrelated output'


@pytest.mark.parametrize('stage', ['cancel', 'startup'])
def test_cancelled_cleanup_permission_failure_preserves_files_and_allows_startup(
    db_engine, app_paths, tmp_path, monkeypatch, stage
):
    from test_desktop_service import service_for
    from yijianjifa.features.mixing.queue import MixTask
    service, renderer, _, _ = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    original_commit = service.executor._commit_success
    monkeypatch.setattr(service.executor, '_commit_success', lambda *args: (_ for _ in ()).throw(
        RuntimeError('injected database transaction failure')))
    service.queue.run_once_until_idle()
    monkeypatch.setattr(service.executor, '_commit_success', original_commit)
    key = f'mix-pending:{task.id}:0'
    pending = _state(service, task.id)['receipts'][key]
    output = Path(pending['output'])
    original_hash = sha256_file(output)
    original_unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path == output:
            raise PermissionError('injected output permission failure')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', denied)
    if stage == 'startup':
        with session_scope(db_engine) as session:
            session.get(MixTask, task.id).state = 'cancelled'
    else:
        service.queue.cancel_remaining(task.id)
    other = service.submit(quantity=1)
    service.hold_after_restart()
    readback = _state(service, task.id)
    assert readback['task']['state'] == 'cancelled' and readback['task']['last_error']
    assert key in readback['receipts']
    assert sha256_file(output) == original_hash
    assert not readback['finished'] and not readback['progress']
    assert service.queue.get(other.id).state == 'paused'
    service.queue.resume(other.id)
    service.queue.run_once_until_idle()
    assert service.queue.get(other.id).success_count == 1
    assert sha256_file(output) == original_hash
    monkeypatch.setattr(Path, 'unlink', original_unlink)
    service.hold_after_restart()
    assert not output.exists()
    assert key not in _state(service, task.id)['receipts']
    assert len(renderer.requests) == 2


def test_cancelled_cleanup_does_not_suppress_database_errors(
    db_engine, app_paths, tmp_path, monkeypatch
):
    from sqlalchemy.exc import OperationalError
    from test_desktop_service import service_for
    service, _, _, _ = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    monkeypatch.setattr(service.executor, '_commit_success', lambda *args: (_ for _ in ()).throw(
        RuntimeError('injected success transaction failure')))
    service.queue.run_once_until_idle()
    key = f'mix-pending:{task.id}:0'
    pending = _state(service, task.id)['receipts'][key]
    monkeypatch.setattr(service.executor, '_discard_pending', lambda *args: (_ for _ in ()).throw(
        OperationalError('DELETE FROM settings', {}, RuntimeError('database unavailable'))))
    with pytest.raises(OperationalError):
        service.queue.cancel_remaining(task.id)
    assert service.queue.get(task.id).state == 'cancelled'
    assert key in _state(service, task.id)['receipts']
    assert Path(pending['output']).is_file()


def test_connection_bound_cancel_never_cleans_files_before_outer_commit(db_engine):
    from yijianjifa.features.mixing.queue import MixQueue
    calls = []
    class Executor:
        def discard_cancelled(self, task_id):
            calls.append(task_id)
    executor = Executor()
    queue = MixQueue(db_engine, executor)
    task = queue.enqueue({'quantity': 1})
    with pytest.raises(RuntimeError, match='rollback outer receipt'):
        with db_engine.begin() as connection:
            MixQueue(connection, executor).cancel_remaining(task.id)
            raise RuntimeError('rollback outer receipt')
    assert queue.get(task.id).state == 'waiting'
    assert calls == [], 'filesystem cleanup must wait for the owning command transaction to commit'
