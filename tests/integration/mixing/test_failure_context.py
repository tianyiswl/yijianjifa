import json
import subprocess
from pathlib import Path

import pytest

from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.mixing.desktop_service import DesktopMixService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def make_service(db_engine, app_paths, tmp_path, *, keep_copy=False, probe=None):
    source = tmp_path / '河北玻璃钢排水沟素材.mp4'
    source.write_bytes(b'not a valid video')
    asset = MediaStorageService(db_engine, app_paths, kind='video').import_media(
        source, keep_copy=keep_copy, category_ids=[])

    class Renderer:
        def render(self, request):
            raise subprocess.CalledProcessError(1, ['ffmpeg'], stderr=b'No such filter: subtitles')

    return DesktopMixService(db_engine, app_paths, renderer=Renderer(), probe=probe), asset


def receipt(db_engine, task_id):
    with session_scope(db_engine) as session:
        return json.loads(session.get(Setting, f'mix-result:{task_id}:0').value_json)


@pytest.mark.parametrize('keep_copy', [False, True])
def test_bad_source_records_original_material_name_stage_and_tool_reason(
        db_engine, app_paths, tmp_path, keep_copy):
    service, asset = make_service(db_engine, app_paths, tmp_path, keep_copy=keep_copy)
    task = service.submit(quantity=1, use_copy=False)
    service.queue.run_once_until_idle()
    item = receipt(db_engine, task.id)
    diagnostic = item['diagnostic']
    assert diagnostic['material'] == '河北玻璃钢排水沟素材.mp4'
    assert diagnostic['stage'] == '读取素材视频信息'
    assert diagnostic['error_type'] == 'CalledProcessError'
    assert '退出码' in diagnostic['reason']
    assert str(tmp_path) not in diagnostic['reason']
    assert [e['step'] for e in item['execution_log']] == ['mix.started', 'mix.failed']
    assert item['execution_log'][-1]['material'] == '河北玻璃钢排水沟素材.mp4'
    assert service.queue.get(task.id).failure_count == 1


def test_missing_source_names_file_in_persisted_diagnostic(db_engine, app_paths, tmp_path):
    service, asset = make_service(db_engine, app_paths, tmp_path, probe=lambda p: 2000)
    task = service.submit(quantity=1, use_copy=False)
    asset.effective_path.unlink()
    service.queue.run_once_until_idle()
    diagnostic = receipt(db_engine, task.id)['diagnostic']
    assert diagnostic['material'] == '河北玻璃钢排水沟素材.mp4'
    assert diagnostic['stage'] == '检查素材文件'
    assert diagnostic['error_type'] == 'FileNotFoundError'


def test_render_failure_records_involved_materials_and_real_cause(db_engine, app_paths, tmp_path):
    service, asset = make_service(db_engine, app_paths, tmp_path, probe=lambda p: 2000)
    task = service.submit(quantity=1, use_copy=False, total_ms=1000, segment_ms=1000)
    service.queue.run_once_until_idle()
    item = receipt(db_engine, task.id)
    assert item['diagnostic']['stage'] == '合成视频'
    assert item['diagnostic']['materials'] == ['河北玻璃钢排水沟素材.mp4']
    assert 'No such filter: subtitles' in item['diagnostic']['reason']
    assert item['execution_log'][-1]['step'] == 'mix.failed'


def test_cancellation_still_discards_without_registering_a_failure(db_engine):
    from yijianjifa.features.mixing.queue import MixQueue, ItemCancelled

    class Executor:
        def render_item(self, *args):
            raise ItemCancelled()

    queue = MixQueue(db_engine, Executor())
    task = queue.enqueue({'quantity': 1})
    queue.run_once_until_idle()
    assert queue.get(task.id).state == 'cancelled'
    assert queue.get(task.id).failure_count == 0
    with session_scope(db_engine) as session:
        assert session.get(Setting, f'mix-result:{task.id}:0') is None
