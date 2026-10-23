from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import threading
import pytest
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow,PublishHistoryRepository
from yijianjifa.features.publish.executor import SubmissionEvidence
from yijianjifa.app_services.scheduled_publish import ScheduledPublish


def seed(engine,paths):
    folder=paths.data_dir/'publish_resources'/'fixture';folder.mkdir(parents=True)
    file=folder/'test.mp4';file.write_bytes(b'local-only-video')
    digest=hashlib.sha256(file.read_bytes()).hexdigest()
    item=dict(account_id='A',video_id='V',title='仅本地测试',body='正文',tags='',declaration={'label':'无需添加自主声明'},
              location=None,execution_mode='platform_schedule',time=(datetime.now(timezone.utc)+timedelta(days=1)).replace(second=0,microsecond=0).isoformat())
    snapshot=dict(kind='normal_publish',execution_enabled=False,items=[item],issues=[],
        videos=[dict(id='V',path=str(file),content_hash=digest,cover={'kind':'first_frame'})],
        accounts=[dict(id='A',platform_user_id='123',session_fingerprint=hashlib.sha256(b'local-session').hexdigest())])
    raw=json.dumps(snapshot)
    with session_scope(engine) as s:
        s.add(AccountRow(id='A',platform='douyin',platform_user_id='123',nickname='离线账号',session_ref='local-session',login_state='valid'))
        s.add(PublishTaskRow(id='T',platform='douyin',state='paused',snapshot_json=raw))
        s.add(TaskCenterRow(id='T',kind='publish',name='本地待执行',state='paused',snapshot_json=raw));s.flush()
        s.add(TaskItemRow(id='I',task_id='T',item_key='A:V',state='paused',detail_json=json.dumps(item)))
    PublishHistoryRepository(engine).record(digest,'A','queued',task_id='T')
    return snapshot,file


def test_explicit_start_connects_frozen_task_to_transport_and_result(db_engine,app_paths):
    snapshot,file=seed(db_engine,app_paths);calls=[]
    def transport(account,video,form,scheduled_iso,*,before_final,cancel):
        calls.append(form)
        assert Path(video['path']).read_bytes()==b'local-only-video'
        before_final()
        with session_scope(db_engine) as s:assert s.get(TaskItemRow,'I').state=='result_unknown'
        return SubmissionEvidence(True,platform_schedule_confirmation=scheduled_iso)
    service=ScheduledPublish(db_engine,app_paths,transport)
    ready=service.check('T');assert ready['available']
    assert service.start('T',ready['digest'],consent=True)['state']=='running'
    service.worker.join(5)
    assert len(calls)==1 and calls[0]['scheduled_time_text']
    with session_scope(db_engine) as s:
        assert s.get(TaskCenterRow,'T').kind=='normal_publish'
        assert s.get(TaskCenterRow,'T').state==s.get(TaskItemRow,'I').state=='platform_scheduled'
        assert s.scalar(select(PublishHistoryRow)).state=='platform_scheduled'
    assert not service.check('T')['available']
    with pytest.raises(ValueError):service.start('T',ready['digest'],consent=True)
    service.close()


@pytest.mark.parametrize('change',['file','session','mode','tags','location','cover','time','digest','consent'])
def test_invalid_or_changed_submission_never_reaches_transport(db_engine,app_paths,change):
    snapshot,file=seed(db_engine,app_paths);calls=[]
    service=ScheduledPublish(db_engine,app_paths,lambda *a,**k:calls.append(True))
    ready=service.check('T');assert ready['available']
    if change=='file':file.write_bytes(b'changed')
    elif change=='session':
        with session_scope(db_engine) as s:s.get(AccountRow,'A').session_ref='changed'
    elif change not in {'digest','consent'}:
        item=snapshot['items'][0]
        if change=='mode':item['execution_mode']='publish_now'
        if change=='tags':item['tags']='#话题'
        if change=='location':item['location']={'name':'地点'}
        if change=='cover':item['cover']={'kind':'uploaded'}
        if change=='time':item['time']=datetime.now(timezone.utc).isoformat()
        with session_scope(db_engine) as s:
            s.get(PublishTaskRow,'T').snapshot_json=s.get(TaskCenterRow,'T').snapshot_json=json.dumps(snapshot)
            s.get(TaskItemRow,'I').detail_json=json.dumps(item)
    with pytest.raises(ValueError):service.start('T','wrong' if change=='digest' else ready['digest'],consent=change!='consent')
    assert calls==[]
    with session_scope(db_engine) as s:assert s.get(PublishTaskRow,'T').state=='paused'
    service.close()


def test_mutation_during_upload_blocks_final_action(db_engine,app_paths):
    _,file=seed(db_engine,app_paths)
    def transport(account,video,form,when,*,before_final,cancel):
        file.write_bytes(b'changed while preparing');before_final()
        pytest.fail('final must not be reached')
    service=ScheduledPublish(db_engine,app_paths,transport)
    service.start('T',service.check('T')['digest'],consent=True);service.worker.join(5)
    with session_scope(db_engine) as s:assert s.get(TaskItemRow,'I').state=='pre_submit_failed'
    service.close()


def test_failure_after_final_marker_is_unknown_not_retryable(db_engine,app_paths):
    seed(db_engine,app_paths)
    def transport(account,video,form,when,*,before_final,cancel):
        before_final();raise TimeoutError()
    service=ScheduledPublish(db_engine,app_paths,transport)
    service.start('T',service.check('T')['digest'],consent=True);service.worker.join(5)
    with session_scope(db_engine) as s:assert s.get(TaskItemRow,'I').state=='result_unknown'
    assert not service.check('T')['available'];service.close()


def test_api_without_transport_cannot_enable_task(db_engine,app_paths):
    from yijianjifa.app_services.desktop_api import DesktopApi
    seed(db_engine,app_paths);api=DesktopApi(db_engine,app_paths)
    def call(op,p):return api.dispatch(json.dumps(dict(v=1,request_id='local',operation=op,params=p)),trusted_origin=True)
    assert call('publish.execution.availability',{})['data']=={'available':False}
    assert not call('publish.execution.start',dict(id='T',digest='x',consent=True))['ok']
    with session_scope(db_engine) as s:assert s.get(PublishTaskRow,'T').state=='paused'
    api.close()


def batch_seed(engine,paths):
    snapshot,file=seed(engine,paths)
    snapshot['items'][0]['declaration']=None
    other=file.with_name('second.mp4');other.write_bytes(b'second-video')
    video={**snapshot['videos'][0],'id':'V2','path':str(other),'content_hash':hashlib.sha256(other.read_bytes()).hexdigest()}
    item={**snapshot['items'][0],'video_id':'V2','title':'第二条','time':(datetime.fromisoformat(snapshot['items'][0]['time'])+timedelta(hours=1)).isoformat()}
    snapshot['videos'].append(video);snapshot['items'].append(item)
    with session_scope(engine) as s:
        s.get(PublishTaskRow,'T').snapshot_json=s.get(TaskCenterRow,'T').snapshot_json=json.dumps(snapshot)
        s.get(TaskItemRow,'I').detail_json=json.dumps(snapshot['items'][0])
        s.add(TaskItemRow(id='I2',task_id='T',item_key='A:V2',state='paused',detail_json=json.dumps(item)))
    PublishHistoryRepository(engine).record(video['content_hash'],'A','queued',task_id='T')
    return snapshot


@pytest.mark.parametrize('failure',[None,'before','after'])
def test_batch_sequential_success_or_stop_without_retry(db_engine,app_paths,failure):
    snapshot=batch_seed(db_engine,app_paths);calls=[]
    def transport(account,video,form,when,*,before_final,cancel):
        calls.append(video['id']);assert form['declaration_label']==''
        if failure=='before':raise RuntimeError('prepare failed')
        before_final()
        if failure=='after':raise TimeoutError('unknown')
        return SubmissionEvidence(True,platform_schedule_confirmation=when)
    service=ScheduledPublish(db_engine,app_paths,transport)
    ready=service.check('T');assert ready['available'],ready
    service.start('T',ready['digest'],consent=True);service.worker.join(5)
    assert not service.worker.is_alive()
    with session_scope(db_engine) as s:
        if failure:
            assert calls==['V'] and s.get(TaskItemRow,'I2').state=='paused'
            assert s.get(TaskItemRow,'I').state==('result_unknown' if failure=='after' else 'pre_submit_failed')
        else:
            assert calls==['V','V2']
            assert s.get(TaskCenterRow,'T').state==s.get(TaskItemRow,'I2').state=='platform_scheduled'
    assert not service.check('T')['available'];service.close()


def test_batch_validates_all_items_before_any_upload(db_engine,app_paths):
    snapshot=batch_seed(db_engine,app_paths)
    Path(snapshot['videos'][1]['path']).write_bytes(b'changed')
    calls=[];service=ScheduledPublish(db_engine,app_paths,lambda *a,**kw:calls.append(True))
    assert not service.check('T')['available']
    with pytest.raises(ValueError):service.start('T','wrong',consent=True)
    assert not calls;service.close()


def test_topic_capable_transport_receives_frozen_topics_without_dropping_them(db_engine,app_paths):
    snapshot,_=seed(db_engine,app_paths);item=snapshot['items'][0];item['tags']='#功能测试 #剪辑测试'
    with session_scope(db_engine) as s:
        s.get(PublishTaskRow,'T').snapshot_json=s.get(TaskCenterRow,'T').snapshot_json=json.dumps(snapshot)
        s.get(TaskItemRow,'I').detail_json=json.dumps(item)
    calls=[]
    def transport(account,video,form,when,*,before_final,cancel):
        calls.append(form);before_final();return SubmissionEvidence(True,platform_schedule_confirmation=when)
    transport.supports_topics=True
    service=ScheduledPublish(db_engine,app_paths,transport)
    ready=service.check('T');assert ready['available']
    service.start('T',ready['digest'],consent=True);service.worker.join(5)
    assert calls[0]['topic_names']==['功能测试','剪辑测试']
    assert calls[0]['body_text']=='正文\n#功能测试 #剪辑测试'
    assert calls[0]['plain_body_text']=='正文'
    service.close()


def test_explicit_retry_keeps_same_task_and_archives_proven_unsubmitted_attempt(db_engine,app_paths):
    from yijianjifa.storage.models import Setting
    seed(db_engine,app_paths)
    failed=ScheduledPublish(db_engine,app_paths,lambda *a,**k:SubmissionEvidence(False,error_code='submission_window_closed'))
    failed.start('T',failed.check('T')['digest'],consent=True);failed.worker.join(5);failed.close()
    calls=[]
    def success(a,v,f,when,*,before_final,cancel):
        calls.append(True);before_final();return SubmissionEvidence(True,platform_schedule_confirmation=when)
    retry=ScheduledPublish(db_engine,app_paths,success)
    with pytest.raises(ValueError):retry.reset_proven_unsubmitted('T',consent=False)
    ready=retry.reset_proven_unsubmitted('T',consent=True);assert ready['available']
    with session_scope(db_engine) as s:
        archives=list(s.scalars(select(Setting).where(Setting.key.like('publish.retry.%'))))
        assert len(archives)==1 and json.loads(archives[0].value_json)['result']['evidence']['final_action_triggered'] is False
    retry.start('T',ready['digest'],consent=True);retry.worker.join(5)
    assert calls==[True]
    with pytest.raises(ValueError):retry.reset_proven_unsubmitted('T',consent=True)
    retry.close()


@pytest.mark.parametrize('change',['final_marker','missing_result','different_digest','claimed_final'])
def test_retry_rejects_ambiguous_or_changed_evidence(db_engine,app_paths,change):
    from yijianjifa.storage.models import Setting
    from yijianjifa.features.publish.submission_journal import SubmissionJournal
    seed(db_engine,app_paths)
    failed=ScheduledPublish(db_engine,app_paths,lambda *a,**k:SubmissionEvidence(False,error_code='stopped'))
    failed.start('T',failed.check('T')['digest'],consent=True);failed.worker.join(5)
    key=SubmissionJournal._key(json.dumps(['T','I']))
    with session_scope(db_engine) as s:
        result=s.get(Setting,key.replace('publish.final.','publish.result.',1))
        if change=='final_marker':s.add(Setting(key=key,value_json='{}'))
        elif change=='missing_result':s.delete(result)
        else:
            value=json.loads(result.value_json)
            if change=='different_digest':value['request_digest']='wrong'
            else:value['evidence']['final_action_triggered']=True
            result.value_json=json.dumps(value)
    with pytest.raises(ValueError):failed.reset_proven_unsubmitted('T',consent=True)
    with session_scope(db_engine) as s:assert s.get(TaskItemRow,'I').state=='pre_submit_failed'
    failed.close()


def test_location_supported_transport_receives_frozen_selection(db_engine,app_paths):
    snapshot,_=seed(db_engine,app_paths)
    snapshot['items'][0]['location']={'name':'三千海','address':'本地测试地址'}
    raw=json.dumps(snapshot)
    with session_scope(db_engine) as s:
        s.get(PublishTaskRow,'T').snapshot_json=s.get(TaskCenterRow,'T').snapshot_json=raw
        s.get(TaskItemRow,'I').detail_json=json.dumps(snapshot['items'][0])
    calls=[]
    def transport(account,video,form,scheduled_iso,*,before_final,cancel):
        calls.append(form['location_selection'])
        before_final()
        return SubmissionEvidence(True,platform_schedule_confirmation=scheduled_iso)
    transport.supports_location=True
    service=ScheduledPublish(db_engine,app_paths,transport)
    ready=service.check('T');assert ready['available']
    service.start('T',ready['digest'],consent=True);service.worker.join(5)
    assert calls==[{'name':'三千海','address':'本地测试地址'}]
    service.close()


@pytest.mark.parametrize('boundary',['check','start','final'])
def test_v2_history_created_after_legacy_preparation_blocks_legacy_execution(db_engine,app_paths,boundary):
    snapshot,_=seed(db_engine,app_paths)
    content_hash=snapshot['videos'][0]['content_hash']
    calls=[]
    def transport(account,video,form,when,*,before_final,cancel):
        calls.append('upload')
        PublishHistoryRepository(db_engine).record(content_hash,'A','waiting')
        before_final()
        calls.append('final')
        return SubmissionEvidence(True,platform_schedule_confirmation=when)
    service=ScheduledPublish(db_engine,app_paths,transport)
    ready=service.check('T');assert ready['available']
    if boundary!='final':PublishHistoryRepository(db_engine).record(content_hash,'A','waiting')
    if boundary=='check':assert service.check('T')['available'] is False
    elif boundary=='start':
        with pytest.raises(ValueError,match='target history'):service.start('T',ready['digest'],consent=True)
        assert service.worker is None
    else:
        service.start('T',ready['digest'],consent=True);service.worker.join(5)
        assert calls==['upload']
        from yijianjifa.storage.models import Setting
        with session_scope(db_engine) as s:
            assert not list(s.scalars(select(Setting).where(Setting.key.like('publish.final.%'))))
            assert s.get(TaskItemRow,'I').state=='pre_submit_failed'
    service.close()


@pytest.mark.parametrize('ending',['success','failure','cancel','cleanup_failure','cancelled_queued'])
def test_confirmed_tasks_queue_and_flush_before_next_task(db_engine,app_paths,ending):
    import copy
    snapshot,file=seed(db_engine,app_paths)
    second=copy.deepcopy(snapshot);other=file.with_name('second.mp4');other.write_bytes(b'other video')
    second['items'][0]['video_id']='V2';second['videos'][0].update(id='V2',path=str(other),content_hash=hashlib.sha256(other.read_bytes()).hexdigest())
    raw=json.dumps(second)
    with session_scope(db_engine) as s:
        s.add(PublishTaskRow(id='T2',platform='douyin',state='paused',snapshot_json=raw))
        s.add(TaskCenterRow(id='T2',kind='publish',name='第二条',state='paused',snapshot_json=raw));s.flush()
        s.add(TaskItemRow(id='I2',task_id='T2',item_key='A:V2',state='paused',detail_json=json.dumps(second['items'][0])))
    PublishHistoryRepository(db_engine).record(second['videos'][0]['content_hash'],'A','queued',task_id='T2')
    entered=threading.Event();release=threading.Event();events=[]
    def transport(account,video,form,when,*,before_final,cancel):
        events.append(video['id'])
        if video['id']=='V':entered.set();assert release.wait(5)
        if video['id']=='V' and ending in {'failure','cancel'}:return SubmissionEvidence(False,error_code='submission_prepare_failed')
        before_final();return SubmissionEvidence(True,platform_schedule_confirmation=when)
    def flush():
        events.append('flush')
        if ending=='cleanup_failure':raise ValueError('browser close failed')
    def persist():
        events.append('persist')
        with session_scope(db_engine) as s:s.get(AccountRow,'A').session_ref='refreshed-after-queue'
    transport.release_sessions=flush
    transport.flush_sessions=persist
    service=ScheduledPublish(db_engine,app_paths,transport)
    try:
        service.start('T',service.check('T')['digest'],consent=True);assert entered.wait(2)
        service.start('T2',service.check('T2')['digest'],consent=True)
        assert events==['V']
        with pytest.raises(ValueError):service.start('T2','invalid-repeat',consent=True)
        if ending=='cancelled_queued':
            with session_scope(db_engine) as s:
                for model,key in [(PublishTaskRow,'T2'),(TaskCenterRow,'T2'),(TaskItemRow,'I2')]:s.get(model,key).state='cancelled'
        if ending in {'cancel','cancelled_queued'}:service.close(0)
        release.set();service.worker.join(5)
        assert not service.worker.is_alive()
        assert events==(['V','flush'] if ending=='cleanup_failure' else ['V','flush','persist'] if ending in {'cancel','cancelled_queued'} else ['V','flush','V2','flush','persist'])
        with session_scope(db_engine) as s:
            row=s.get(TaskCenterRow,'T2')
            assert row.state==('cancelled' if ending=='cancelled_queued' else 'paused' if ending in {'cancel','cleanup_failure'} else 'platform_scheduled')
            if ending in {'cancel','cleanup_failure'}:assert json.loads(row.snapshot_json)['execution_enabled'] is False
    finally:release.set();service.close(5)
