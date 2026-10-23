import json
import pytest
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
from yijianjifa.app_services.publish_review import PublishReview


def seed(engine,state='result_unknown'):
    with session_scope(engine) as s:
        s.add(TaskCenterRow(id='T',kind='normal_publish',name='测试任务',state=state,snapshot_json='{}'));s.flush()
        s.add(TaskItemRow(id='I',task_id='T',item_key='A:V',state=state,detail_json='{}'))
        s.add(Setting(key='publish.final.untouched',value_json='{"proof":true}'))
    return PublishReview(engine)


def test_append_idempotent_and_preserves_machine_evidence(db_engine):
    service=seed(db_engine);before=service.read('T');params=('T',before['digest'],'removed','后台确认测试作品已删除',True,'request1')
    first=service.save(*params);assert service.save(*params)==first
    read=service.read('T');assert len(read['entries'])==1 and read['digest']!=before['digest']
    with session_scope(db_engine) as s:
        assert s.get(TaskCenterRow,'T').state=='result_unknown'
        assert s.get(TaskItemRow,'I').state=='result_unknown'
        assert s.get(Setting,'publish.final.untouched').value_json=='{"proof":true}'
    with pytest.raises(ValueError):service.save('T',before['digest'],'published','不同观察',True,'request2')
    service.save('T',read['digest'],'not_found','继续核对',True,'request2')
    assert len(PublishReview(db_engine).read('T')['entries'])==2


@pytest.mark.parametrize('state',['running','paused','pre_submit_failed','cancelled'])
def test_not_available_for_unsubmitted_or_active_task(db_engine,state):
    service=seed(db_engine,state);value=service.read('T');assert not value['available']
    with pytest.raises(ValueError):service.save('T',value['digest'],'removed','无依据',True,'r')


@pytest.mark.parametrize('outcome,note,consent',[('removed','',True),('bad','说明',True),('removed','说明',False),([], '说明',True),('removed','x'*1001,True)])
def test_requires_explicit_complete_observation(db_engine,outcome,note,consent):
    service=seed(db_engine)
    with pytest.raises(ValueError):service.save('T',service.read('T')['digest'],outcome,note,consent,'r')
    assert service.read('T')['entries']==[]


def test_lost_reply_cannot_reuse_request_for_different_result(db_engine):
    service=seed(db_engine);digest=service.read('T')['digest']
    service.save('T',digest,'scheduled','后台时间一致',True,'r')
    with pytest.raises(ValueError):service.save('T',digest,'removed','已删除',True,'r')


def removed_attempt(engine,tmp_path):
    from yijianjifa.features.publish.history import PublishHistoryRepository
    from yijianjifa.features.finished.service import FinishedVideoService
    p=tmp_path/'test.mp4';p.write_bytes(b'unique fixture')
    v=FinishedVideoService(engine).register_generated(p,duration_ms=3000,title='测试片')
    service=seed(engine)
    with session_scope(engine) as s:
        s.get(TaskCenterRow,'T').snapshot_json=json.dumps({'items':[{'account_id':'A','video_id':v.id}],
            'videos':[{'id':v.id,'content_hash':v.content_hash}]})
    h=PublishHistoryRepository(engine);h.record(v.content_hash,'A','result_unknown',task_id='T')
    service.save('T',service.read('T')['digest'],'removed','对应作品已删除',True,'review')
    return service,h,v


def test_rearrange_is_explicit_once_persistent_and_preserves_old_history(db_engine,tmp_path):
    from yijianjifa.features.publish.history import DuplicatePublishBlocked,PublishHistoryRow
    from yijianjifa.app_services.publish_planning import PublishPlanning
    service,h,v=removed_attempt(db_engine,tmp_path)
    with pytest.raises(DuplicatePublishBlocked):h.assert_can_create(v.content_hash,'A',allow_repeat_success=False)
    digest=service.read('T')['digest'];assert service.read('T')['rearrange_available']
    with pytest.raises(ValueError):service.allow_rearrange('T',digest,False,'grant')
    grant=service.allow_rearrange('T',digest,True,'grant')
    assert service.allow_rearrange('T',digest,True,'grant')==grant
    assert not service.read('T')['rearrange_available']
    h.assert_can_create(v.content_hash,'A',allow_repeat_success=False)
    assert v.id in [i['id'] for i in PublishPlanning(db_engine).candidates(query='',offset=0,limit=20)['items']]
    with pytest.raises(ValueError):service.allow_rearrange('T',digest,True,'another-grant')
    h.record(v.content_hash,'A','queued',task_id='NEW')
    assert PublishReview(db_engine).read('T')['rearrange']['consumed_by']=='NEW'
    with pytest.raises(DuplicatePublishBlocked):h.assert_can_create(v.content_hash,'A',allow_repeat_success=False)
    with session_scope(db_engine) as s:
        assert s.scalar(select(PublishHistoryRow).where(PublishHistoryRow.task_id=='T')).state=='result_unknown'
        assert s.get(TaskCenterRow,'T').state=='result_unknown'
        assert s.get(TaskItemRow,'I').state=='result_unknown'
        assert s.get(Setting,'publish.final.untouched').value_json=='{"proof":true}'
    assert service.allow_rearrange('T',digest,True,'grant')['consumed_by']=='NEW'


def test_new_review_invalidates_permission_and_stale_confirmation(db_engine,tmp_path):
    from yijianjifa.features.publish.history import DuplicatePublishBlocked
    service,h,v=removed_attempt(db_engine,tmp_path);digest=service.read('T')['digest']
    service.allow_rearrange('T',digest,True,'grant')
    service.save('T',digest,'not_found','重新核对中',True,'review2')
    with pytest.raises(DuplicatePublishBlocked):h.assert_can_create(v.content_hash,'A',allow_repeat_success=False)
    assert not service.read('T')['rearrange_available']
    with pytest.raises(ValueError):service.allow_rearrange('T',digest,True,'grant2')
    with pytest.raises(ValueError):service.allow_rearrange('T',service.read('T')['digest'],True,'grant2')


def test_permission_does_not_ignore_another_attempt_or_account(db_engine,tmp_path):
    from yijianjifa.features.publish.history import DuplicatePublishBlocked
    service,h,v=removed_attempt(db_engine,tmp_path)
    service.allow_rearrange('T',service.read('T')['digest'],True,'grant')
    h.record(v.content_hash,'B','result_unknown',task_id='OTHER')
    with pytest.raises(DuplicatePublishBlocked):h.assert_can_create(v.content_hash,'B',allow_repeat_success=False)
    h.assert_can_create(v.content_hash,'A',allow_repeat_success=False)
    h.record(v.content_hash,'A','result_unknown',task_id='OTHER2')
    with pytest.raises(DuplicatePublishBlocked):h.assert_can_create(v.content_hash,'A',allow_repeat_success=False)


def test_permission_consumption_rolls_back_with_task_creation(db_engine,tmp_path):
    from yijianjifa.features.publish.history import PublishHistoryRepository
    service,h,v=removed_attempt(db_engine,tmp_path)
    service.allow_rearrange('T',service.read('T')['digest'],True,'grant')
    with db_engine.connect() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE')
        PublishHistoryRepository(c).record(v.content_hash,'A','queued',task_id='FAILED')
        c.rollback()
    assert service.read('T')['rearrange']['consumed_by'] is None
    h.assert_can_create(v.content_hash,'A',allow_repeat_success=False)


def test_incomplete_history_cannot_grant_permission(db_engine):
    service=seed(db_engine)
    service.save('T',service.read('T')['digest'],'removed','已删除',True,'review')
    assert not service.read('T')['rearrange_available']
    with pytest.raises(ValueError):service.allow_rearrange('T',service.read('T')['digest'],True,'grant')


def test_rearrange_real_preparation_confirmation_consumes_atomically(db_engine,app_paths,tmp_path):
    from test_publish_preparation import setup
    from yijianjifa.app_services.publish_confirmation import PublishConfirmation
    from yijianjifa.features.publish.history import PublishHistoryRepository
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    service=seed(db_engine)
    with session_scope(db_engine) as s:
        s.get(TaskCenterRow,'T').snapshot_json=json.dumps({'items':[{'account_id':a.id,'video_id':v.id}],
            'videos':[{'id':v.id,'content_hash':v.content_hash}]})
    PublishHistoryRepository(db_engine).record(v.content_hash,a.id,'result_unknown',task_id='T')
    service.save('T',service.read('T')['digest'],'removed','原作品确认已删除',True,'review')
    service.allow_rearrange('T',service.read('T')['digest'],True,'grant')
    token=prep.prepare(p,now=now)['token'];assert token
    confirmation=PublishConfirmation(db_engine,app_paths,prep)
    result=confirmation.confirm(token,now=now)
    assert result['state']=='paused' and result['submitted'] is False
    assert service.read('T')['rearrange']['consumed_by']==result['id']
    assert confirmation.confirm(token,now=now)==result
    assert not prep.prepare(p,now=now).get('token')
    prep.close()


def test_api_grant_requires_trusted_origin_and_exact_fields(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.desktop_api import DesktopApi
    service,h,v=removed_attempt(db_engine,tmp_path)
    api=DesktopApi(db_engine,app_paths)
    payload={'v':1,'request_id':'grant-api','operation':'publish.review.allow_rearrange',
             'params':{'id':'T','digest':service.read('T')['digest'],'consent':True}}
    try:
        assert not api.dispatch(json.dumps(payload),trusted_origin=False)['ok']
        bad={**payload,'params':{**payload['params'],'execute':True}}
        assert not api.dispatch(json.dumps(bad),trusted_origin=True)['ok']
        result=api.dispatch(json.dumps(payload),trusted_origin=True)
        assert result['ok'],result
        assert api.dispatch(json.dumps(payload),trusted_origin=True)==result
        assert service.read('T')['rearrange']['consumed_by'] is None
    finally:api.close(.1)
