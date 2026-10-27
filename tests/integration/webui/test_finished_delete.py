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


def test_active_serial_parent_keeps_its_generated_file_until_batch_cancelled(db_engine,tmp_path):
    from yijianjifa.features.mixing.queue import MixQueue
    queue=MixQueue(db_engine,None)
    task=queue.enqueue({'quantity':1,'output_directory':str(tmp_path)})
    source=tmp_path/task.id/'finished.mp4';source.parent.mkdir();source.write_bytes(b'generated')
    video=FinishedVideoService(db_engine).register_generated(source,duration_ms=1000)
    with pytest.raises(RuntimeError,match='本批'):FinishedDeletion(db_engine).delete(video.id)
    assert source.exists()
    queue.cancel_remaining(task.id)
    assert FinishedDeletion(db_engine).delete(video.id)['deleted']


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


@pytest.mark.parametrize('outcome',['platform_scheduled','platform_accepted','result_unknown','platform_rejected'])
def test_hidden_v2_history_keeps_its_copy_but_does_not_lock_finished_source(db_engine,app_paths,tmp_path,outcome):
    from datetime import datetime,timedelta,timezone
    from sqlalchemy import select
    from yijianjifa.app_services.multi_publish import MultiPublishService
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.publish.coordinator import PublishCoordinator,AdapterOutcome,AdapterValidation
    from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
    from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow,PublishResourceRow
    from yijianjifa.features.publish.history import PublishHistoryRow,PublishHistoryRepository
    from yijianjifa.storage.db import session_scope
    class Adapter:
        platform_id='douyin'
        verified_capabilities={'ordinary','publish_now','platform_schedule'}
        def validate_target(self,target,payload,execution):return AdapterValidation(target['platform_user_id'],True)
        def submit(self,payload,*,context,before_final):
            before_final()
            return AdapterOutcome(outcome,scheduled_at=payload['scheduled_at'],receipt_reference={'work_id':'synthetic'})
        def reconcile(self,payload,*,context):return AdapterOutcome(outcome)
    source=tmp_path/'generated.mp4';source.write_bytes(b'generated video')
    item=FinishedVideoService(db_engine).register_generated(source,duration_ms=1000)
    account=AccountService(db_engine).create('douyin','isolated','测试')
    service=MultiPublishService(db_engine,app_paths,catalog=PlatformCatalog(transports={'douyin':Adapter()}))
    prepared=service.prepare(request_id='hidden-history',video_ids=[item.id],targets=[{'account_id':account.id,'platform':'douyin'}],common_fields={'title':'测试'},execution_mode='platform_schedule',scheduled_times={item.id:(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()})
    receipt=service.save(prepared['prepared_id'],request_id='hidden-history')
    PublishCoordinator(service).run_once(receipt['id'])
    with session_scope(db_engine) as s:
        row=s.scalar(select(PublishDeliveryRow));delivery_id=row.id;history_id=row.history_id
        evidence=s.scalar(select(DeliveryAttemptRow)).result_json
        assert s.get(PublishHistoryRow,history_id).state==outcome
        copied=Path(s.scalar(select(PublishResourceRow)).path)
    duplicate_block=PublishHistoryRepository(db_engine).has_quick_block(item.content_hash)
    # A visible task still protects its source; deleting the local task only
    # detaches the original. Its owned copy and duplicate history stay intact.
    assert FinishedDeletion(db_engine).preview([item.id])['items'][0]['allowed'] is False
    service.cancel_delivery(delivery_id)
    assert FinishedDeletion(db_engine).preview([item.id])['items'][0]['allowed'] is True
    service.hide_delivery(delivery_id)
    copied.write_bytes(b'broken copy')
    assert FinishedDeletion(db_engine).preview([item.id])['items'][0]['allowed'] is False
    copied.write_bytes(b'generated video')
    assert FinishedDeletion(db_engine).preview([item.id])['items'][0]['allowed'] is True
    assert FinishedDeletion(db_engine).delete(item.id)['deleted']
    assert not source.exists() and copied.read_bytes()==b'generated video'
    with session_scope(db_engine) as s:
        assert s.get(PublishHistoryRow,history_id).state==outcome
        assert s.scalar(select(DeliveryAttemptRow)).result_json==evidence
    assert PublishHistoryRepository(db_engine).has_quick_block(item.content_hash)==duplicate_block
