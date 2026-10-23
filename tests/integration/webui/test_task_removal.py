import pytest
from sqlalchemy import select
from test_scheduled_publish import seed
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.tasks.service import TaskCenterRow,TaskCenterService
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.app_services.task_removal import remove_task
from yijianjifa.app_services.task_center_access import TaskCenterAccess


def test_removing_held_task_cancels_history_but_keeps_resources(db_engine,app_paths):
    _,file=seed(db_engine,app_paths)
    with db_engine.begin() as c:remove_task(c,'T')
    with session_scope(db_engine) as s:
        assert s.get(TaskCenterRow,'T').state=='cancelled'
        assert s.scalar(select(PublishHistoryRow)).state=='cancelled'
        assert s.get(Setting,'task-hidden:T')
    assert file.exists()
    assert TaskCenterAccess(db_engine).list('', 'all','',0,20)['total']==0
    with db_engine.begin() as c:assert remove_task(c,'T')['deleted']


@pytest.mark.parametrize('state',['running','paused_auth'])
def test_unresolved_task_cannot_be_hidden(db_engine,app_paths,state):
    seed(db_engine,app_paths)
    with session_scope(db_engine) as s:s.get(TaskCenterRow,'T').state=state
    with pytest.raises(ValueError):
        with db_engine.begin() as c:remove_task(c,'T')
    with session_scope(db_engine) as s:
        assert s.get(Setting,'task-hidden:T') is None
        assert s.scalar(select(PublishHistoryRow)).state=='queued'


def test_latest_task_is_first_even_after_more_than_one_page(db_engine):
    core=TaskCenterService(db_engine)
    for i in range(25):core.register_task(str(i),kind='publish',name='任务',state='paused',snapshot_json='{}')
    rows=TaskCenterAccess(db_engine).list('','all','',0,20)
    assert rows['total']==25 and rows['items'][0]['id']=='24'


@pytest.mark.parametrize('state',['paused','interrupted'])
def test_pending_make_can_be_deleted_without_losing_outputs_or_resuming(db_engine,state):
    import json
    from yijianjifa.features.mixing.queue import MixQueue,MixTask
    task=MixQueue(db_engine,None).enqueue({'quantity':1})
    with session_scope(db_engine) as s:
        s.get(MixTask,task.id).state=state
        s.add(Setting(key='mix.daily.status:'+task.id,value_json=json.dumps({'auth_phase':'needs_attention'})))
    with db_engine.begin() as c:assert remove_task(c,task.id)['deleted']
    assert TaskCenterAccess(db_engine).list('','all','',0,20)['total']==0
    with session_scope(db_engine) as s:assert s.get(MixTask,task.id).state=='cancelled'

@pytest.mark.parametrize('outcome,allowed',[('removed',True),('not_found',True),('scheduled',True)])
def test_unknown_hide_keeps_original_evidence_and_reservation(db_engine,app_paths,outcome,allowed):
    from yijianjifa.features.tasks.service import TaskItemRow
    from yijianjifa.features.publish.normal_service import PublishTaskRow
    from yijianjifa.app_services.publish_review import PublishReview
    seed(db_engine,app_paths)
    with session_scope(db_engine) as s:
        for model,key in [(TaskCenterRow,'T'),(TaskItemRow,'I'),(PublishTaskRow,'T')]:s.get(model,key).state='result_unknown'
        s.scalar(select(PublishHistoryRow)).state='result_unknown'
    review=PublishReview(db_engine);data=review.read('T')
    review.save('T',data['digest'],outcome,'离线核对依据',True,'test-review')
    if allowed:
        with db_engine.begin() as c:remove_task(c,'T')
    else:
        with pytest.raises(ValueError):
            with db_engine.begin() as c:remove_task(c,'T')
    with session_scope(db_engine) as s:
        assert s.get(TaskCenterRow,'T').state=='result_unknown'
        assert s.scalar(select(PublishHistoryRow)).state=='result_unknown'
        assert s.get(Setting,'publish-review:T')
        assert bool(s.get(Setting,'task-hidden:T')) is allowed


def test_failed_batch_delete_releases_unsubmitted_items_and_preserves_journal(db_engine,app_paths):
    import json
    from pathlib import Path
    from yijianjifa.app_services.scheduled_publish import ScheduledPublish
    from yijianjifa.features.publish.executor import SubmissionEvidence
    from yijianjifa.features.tasks.service import TaskItemRow
    from yijianjifa.features.publish.normal_service import PublishTaskRow
    snapshot,_=seed(db_engine,app_paths)
    extra={**snapshot['items'][0],'video_id':'V2'}
    snapshot['items'].append(extra)
    second=Path(snapshot['videos'][0]['path']).with_name('second.mp4');second.write_bytes(b'second-offline-video')
    import hashlib
    digest=hashlib.sha256(second.read_bytes()).hexdigest()
    snapshot['videos'].append({**snapshot['videos'][0],'id':'V2','path':str(second),'content_hash':digest})
    with session_scope(db_engine) as s:
        raw=json.dumps(snapshot)
        for model in (PublishTaskRow,TaskCenterRow):s.get(model,'T').snapshot_json=raw
        s.add(TaskItemRow(id='I2',task_id='T',item_key='A:V2',state='paused',detail_json=json.dumps(extra)))
        s.add(PublishHistoryRow(id='H2',task_id='T',video_hash=digest,account_id='A',attempt=1,state='queued'))
    service=ScheduledPublish(db_engine,app_paths,lambda *a,**kw:SubmissionEvidence(False,error_code='prepare_failed'))
    service.start('T',service.check('T')['digest'],consent=True);service.worker.join(5)
    with session_scope(db_engine) as s:
        assert s.get(TaskCenterRow,'T').state=='pre_submit_failed'
        journals={r.key:r.value_json for r in s.scalars(select(Setting).where(Setting.key.like('publish.%')))}
    with db_engine.begin() as c:remove_task(c,'T')
    with session_scope(db_engine) as s:
        assert {r.state for r in s.scalars(select(PublishHistoryRow))}=={'cancelled'}
        assert {r.state for r in s.scalars(select(TaskItemRow))}=={'cancelled'}
        assert all(s.get(Setting,k).value_json==v for k,v in journals.items())
    service.close()


def test_partial_success_delete_preserves_submitted_work_and_releases_failed_remainder(db_engine,app_paths):
    import json,hashlib
    from pathlib import Path
    from test_scheduled_publish import batch_seed
    from yijianjifa.features.tasks.service import TaskItemRow
    from yijianjifa.features.publish.normal_service import PublishTaskRow
    from yijianjifa.features.publish.executor import SubmissionEvidence
    from yijianjifa.app_services.scheduled_publish import ScheduledPublish
    snapshot=batch_seed(db_engine,app_paths)
    file=Path(snapshot['videos'][0]['path']).with_name('third.mp4');file.write_bytes(b'third-unsubmitted')
    digest=hashlib.sha256(file.read_bytes()).hexdigest()
    video={**snapshot['videos'][1],'id':'V3','path':str(file),'content_hash':digest}
    item={**snapshot['items'][1],'video_id':'V3','title':'第三条'}
    snapshot['videos'].append(video);snapshot['items'].append(item)
    with session_scope(db_engine) as s:
        raw=json.dumps(snapshot)
        for model in (PublishTaskRow,TaskCenterRow):s.get(model,'T').snapshot_json=raw
        s.add(TaskItemRow(id='I3',task_id='T',item_key='A:V3',state='paused',detail_json=json.dumps(item)))
        s.add(PublishHistoryRow(id='H3',task_id='T',video_hash=digest,account_id='A',attempt=1,state='queued'))
    calls=[]
    def transport(account,video,form,when,*,before_final,cancel):
        calls.append(video['id'])
        if video['id']=='V':
            before_final();return SubmissionEvidence(True,platform_schedule_confirmation=when)
        return SubmissionEvidence(False,error_code='prepare_failed')
    service=ScheduledPublish(db_engine,app_paths,transport)
    service.start('T',service.check('T')['digest'],consent=True);service.worker.join(5)
    assert calls==['V','V2']
    with session_scope(db_engine) as s:
        successful=s.scalar(select(PublishHistoryRow).where(PublishHistoryRow.state=='platform_scheduled'))
        successful_id=successful.id
        evidence={r.key:r.value_json for r in s.scalars(select(Setting).where(Setting.key.like('publish.%')))}
    with db_engine.begin() as c:assert remove_task(c,'T')['deleted']
    with session_scope(db_engine) as s:
        assert s.get(TaskItemRow,'I').state=='platform_scheduled'
        assert s.get(PublishHistoryRow,successful_id).state=='platform_scheduled'
        assert s.get(TaskItemRow,'I2').state==s.get(TaskItemRow,'I3').state=='cancelled'
        assert s.get(PublishHistoryRow,'H3').state=='cancelled'
        assert all(s.get(Setting,k).value_json==v for k,v in evidence.items())
        assert s.get(Setting,'task-hidden:T')
    assert TaskCenterAccess(db_engine).list('','all','',0,20)['total']==0
    assert file.exists()
    service.close()
