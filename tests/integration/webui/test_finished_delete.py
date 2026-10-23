import json
import pytest
from pathlib import Path
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.tasks.service import TaskCenterService
from yijianjifa.app_services.finished_delete import FinishedDeletion


def test_delete_respects_real_snapshot_without_manual_lock(db_engine,tmp_path):
    file=tmp_path/'video.mp4';file.write_bytes(b'video')
    core=FinishedVideoService(db_engine);item=core.register_generated(file,duration_ms=1000)
    TaskCenterService(db_engine).register_task('pending',kind='publish',name='待确认',state='result_unknown',snapshot_json=json.dumps({'video_id':item.id}))
    with pytest.raises(RuntimeError):FinishedDeletion(db_engine).delete(item.id)
    assert file.is_file() and core.get(item.id)


def test_generated_delete_and_external_record_only_are_idempotent(db_engine,tmp_path):
    generated=tmp_path/'自制.mp4';generated.write_bytes(b'generated')
    external=tmp_path/'外部.mp4';external.write_bytes(b'external')
    core=FinishedVideoService(db_engine);g=core.register_generated(generated,duration_ms=1000);e=core.import_external(external,duration_ms=1000)
    service=FinishedDeletion(db_engine)
    result=service.delete(g.id)
    assert result['deleted'] and not generated.exists() and core.get(g.id) is None
    assert FinishedDeletion(db_engine).delete(g.id)==result
    assert service.delete(e.id)['deleted'] and external.read_bytes()==b'external'


def test_database_failure_restores_file_and_row(db_engine,tmp_path):
    file=tmp_path/'video.mp4';file.write_bytes(b'video')
    core=FinishedVideoService(db_engine);item=core.register_generated(file,duration_ms=1000)
    with db_engine.begin() as c:c.exec_driver_sql("CREATE TRIGGER deny_finished_delete BEFORE DELETE ON finished_videos BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(Exception):FinishedDeletion(db_engine).delete(item.id)
    assert file.read_bytes()==b'video' and core.get(item.id)


def test_recovery_restores_precommit_file_and_finishes_committed_cleanup(db_engine,tmp_path):
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    core=FinishedVideoService(db_engine)
    for state in ('prepared','committed'):
        original=tmp_path/(state+'.mp4');original.write_bytes(b'video')
        item=core.register_generated(original,duration_ms=1000)
        hidden=tmp_path/('.p011-delete-'+item.id);original.rename(hidden)
        with session_scope(db_engine) as s:
            if state=='committed':
                from yijianjifa.features.finished.service import FinishedVideo
                s.delete(s.get(FinishedVideo,item.id))
            s.add(Setting(key='finished-delete:'+item.id,value_json=json.dumps({'id':item.id,'state':state,'original':str(original),'staged':str(hidden),'owned':True})))
    errors=FinishedDeletion(db_engine).recover()
    assert errors==[]
    assert (tmp_path/'prepared.mp4').read_bytes()==b'video'
    assert not (tmp_path/'committed.mp4').exists()
    assert not list(tmp_path.glob('.p011-delete-*'))


def test_changed_generated_file_is_not_deleted(db_engine,tmp_path):
    file=tmp_path/'video.mp4';file.write_bytes(b'original')
    core=FinishedVideoService(db_engine);item=core.register_generated(file,duration_ms=1000)
    file.write_bytes(b'replaced outside application')
    with pytest.raises(RuntimeError):FinishedDeletion(db_engine).delete(item.id)
    assert core.get(item.id) and file.read_bytes()==b'replaced outside application'


@pytest.mark.parametrize('state,blocked',[('result_unknown',True),('published',False)])
def test_publish_history_protects_pending_and_survives_deletion(db_engine,tmp_path,state,blocked):
    from yijianjifa.features.publish.history import PublishHistoryRepository
    file=tmp_path/'external.mp4';file.write_bytes(b'video')
    core=FinishedVideoService(db_engine);item=core.import_external(file,duration_ms=1000)
    history=PublishHistoryRepository(db_engine);history.record(item.content_hash,'isolated-account',state)
    if blocked:
        with pytest.raises(RuntimeError):FinishedDeletion(db_engine).delete(item.id)
        assert core.get(item.id)
    else:
        assert FinishedDeletion(db_engine).delete(item.id)['deleted']
    assert history.has_quick_block(item.content_hash) and file.exists()
