"""逐条每日制作的独立恢复与停止竞争检查；只有渲染和平台IO使用合成替身。"""
import json
import threading

import pytest
from sqlalchemy import func,select

from test_daily_serial_flow import serial,status,receive_child,call
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_runtime import authorized_targets
from yijianjifa.features.library.usage_progress import UsageProgress
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.notifications.service import NotificationRow
from yijianjifa.features.publish.deliveries import PublishBatchReceiptRow,PublishDeliveryRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def completion_count(engine,parent):
    with session_scope(engine) as session:
        return session.scalar(select(func.count()).select_from(NotificationRow).where(
            NotificationRow.task_id==parent,NotificationRow.status=='mix_completed'))


def child_count(engine,parent):
    with session_scope(engine) as session:
        return session.scalar(select(func.count()).select_from(PublishBatchReceiptRow).where(
            PublishBatchReceiptRow.request_id.like('daily-'+parent+'-i%')))


def usage_count(engine):
    with session_scope(engine) as session:
        return session.scalar(select(func.count()).select_from(UsageProgress).where(UsageProgress.scope=='mixing'))


def complete_previous(serial,index):
    api,parent,renderer,*_=serial
    api._mix.queue.run_once_until_idle()
    assert renderer.calls==index+1
    child=status(serial)['publish_task_ids'][-1]
    receive_child(serial,child)
    api._daily.reconcile(parent)
    return child


def reopen(serial,db_engine,app_paths,monkeypatch,daily_auth_sync):
    api,parent,renderer,events,adapters,accounts=serial
    assert api.close(3)
    reopened=DesktopApi(db_engine,app_paths,publish_adapters=adapters)
    daily_auth_sync(reopened)
    monkeypatch.setattr(reopened._multi_publish,'wake',lambda:None)
    reopened._mix.executor.renderer=renderer
    reopened._mix.probe=lambda path:100000
    return reopened,parent,renderer,events,adapters,accounts


def test_reopen_revokes_waiting_child_and_explicit_continue_reuses_video_and_child(serial,db_engine,app_paths,monkeypatch,daily_auth_sync):
    api,parent,renderer,events,*_=serial
    api._mix.queue.run_once_until_idle()
    child=status(serial)['publish_task_ids'][0]
    assert renderer.calls==renderer.tts_calls==usage_count(db_engine)==1
    fresh=reopen(serial,db_engine,app_paths,monkeypatch,daily_auth_sync)
    reopened=fresh[0]
    try:
        with session_scope(db_engine) as session:
            assert authorized_targets(session,child)=={}
            before=[(row.key,row.value_json) for row in session.scalars(select(Setting))]
        for _ in range(3):
            call(reopened,'mix.daily.status',{'id':parent})
            reopened._daily.recheck_serial()
        with session_scope(db_engine) as session:
            assert authorized_targets(session,child)=={}
            assert [(row.key,row.value_json) for row in session.scalars(select(Setting))]==before
        assert renderer.calls==renderer.tts_calls==1 and child_count(db_engine,parent)==1
        response=call(reopened,'task.resume',{'id':parent},'explicit-reopen-continue')
        assert response['ok'],response
        reopened._mix.queue.run_once_until_idle()
        assert renderer.calls==renderer.tts_calls==1
        assert status(fresh)['publish_task_ids']==[child]
        receive_child(fresh,child)
        reopened._daily.reconcile(parent)
        reopened._mix.queue.run_once_until_idle()
        assert renderer.calls==renderer.tts_calls==2 and usage_count(db_engine)==2
        assert child_count(db_engine,parent)==2
    finally:assert reopened.close(3)


def test_last_render_waits_at_quantity_until_third_account_and_notifies_once(serial,db_engine):
    api,parent,renderer,*_=serial
    complete_previous(serial,0);complete_previous(serial,1)
    api._mix.queue.run_once_until_idle()
    final=api._mix.queue.get(parent)
    assert final.current_index==final.quantity==final.completed_count==3
    assert final.state=='waiting_publish' and completion_count(db_engine,parent)==0
    child=status(serial)['publish_task_ids'][-1]
    with session_scope(db_engine) as session:ids=list(authorized_targets(session,child))
    for _ in range(2):api._multi_publish.coordinator.run_once(child,ids)
    api._daily.reconcile(parent);api._mix.queue.run_once_until_idle()
    assert status(serial)['serial']['item_received']==2
    assert api._mix.queue.get(parent).state=='waiting_publish'
    assert renderer.calls==renderer.tts_calls==3 and completion_count(db_engine,parent)==0
    api._multi_publish.coordinator.run_once(child,ids)
    for _ in range(3):
        api._daily.reconcile(parent);api._daily.recheck_serial();api._mix.queue.run_once_until_idle()
    assert status(serial)['serial']['stage']=='completed'
    assert api._mix.queue.get(parent).state=='completed'
    assert completion_count(db_engine,parent)==1 and child_count(db_engine,parent)==3


@pytest.mark.parametrize('serial',[{'accounts':1,'quantity':1}],indirect=True)
@pytest.mark.parametrize('legacy_reopen',[False,True])
def test_final_quota_skip_is_settled_after_reopen_without_more_work(serial,db_engine,app_paths,monkeypatch,daily_auth_sync,legacy_reopen):
    from yijianjifa.features.publish.coordinator import AdapterOutcome
    api,parent,renderer,events,adapters,_=serial
    def limited(payload,*,context,before_final):
        before_final()
        return AdapterOutcome('platform_rejected',error_code='douyin_daily_post_limit')
    monkeypatch.setattr(adapters['douyin'],'submit',limited)
    api._mix.queue.run_once_until_idle()
    receive_child(serial,status(serial)['publish_task_ids'][0])
    api._daily.reconcile(parent)
    assert api._mix.queue.get(parent).state=='completed'
    if legacy_reopen:
        with session_scope(db_engine) as s:
            s.get(MixTask,parent).state='paused'
            row=s.get(Setting,'mix.daily.status:'+parent)
            proof=json.loads(row.value_json)
            proof.update(automatic_stopped=False,auth_phase='',reason_code='daily_authorization_required',reason='客户端已重新打开')
            row.value_json=json.dumps(proof)
    before=list(events)
    fresh=reopen(serial,db_engine,app_paths,monkeypatch,daily_auth_sync)
    reopened=fresh[0]
    try:
        result=reopened._daily.status(parent)
        assert reopened._mix.queue.get(parent).state=='completed'
        assert result['serial']['stage']=='completed' and not result['can_continue']
        assert '未发布' in result['reason'] and '合成账号111' in result['reason']
        assert parent not in {task['id'] for task in reopened._runtime.recovery()['tasks']}
        assert renderer.calls==renderer.tts_calls==1 and events==before
        assert completion_count(db_engine,parent)==1
        from datetime import datetime, timedelta, timezone
        from yijianjifa.app_services.mix_execution import read_execution
        timing=read_execution(db_engine,parent)['timing']
        with session_scope(db_engine) as s:
            s.get(MixTask,parent).updated_at=datetime.now(timezone.utc)+timedelta(days=1)
        assert read_execution(db_engine,parent)['timing']==timing
    finally:assert reopened.close(3)


def test_reopen_final_wait_is_visible_even_when_every_video_is_already_made(serial,db_engine,app_paths,monkeypatch,daily_auth_sync):
    api,parent,renderer,*_=serial
    complete_previous(serial,0);complete_previous(serial,1)
    api._mix.queue.run_once_until_idle()
    child=status(serial)['publish_task_ids'][-1]
    fresh=reopen(serial,db_engine,app_paths,monkeypatch,daily_auth_sync);reopened=fresh[0]
    try:
        held=reopened._mix.queue.get(parent)
        assert held.current_index==held.quantity==held.completed_count==3
        # 关闭原会话属于停止意图；重开显示撤权原因，不自动转回发布。
        assert held.state=='paused'
        assert reopened._daily.status(parent)['waiting_reason']=='authorization'
        assert parent in {task['id'] for task in reopened._runtime.recovery()['tasks']}
        with session_scope(db_engine) as session:assert authorized_targets(session,child)=={}
        assert call(reopened,'task.resume',{'id':parent},'continue-final-receipt')['ok']
        assert renderer.calls==renderer.tts_calls==3
        receive_child(fresh,child);reopened._daily.reconcile(parent)
        assert reopened._mix.queue.get(parent).state=='completed'
        assert completion_count(db_engine,parent)==1 and child_count(db_engine,parent)==3
    finally:assert reopened.close(3)


@pytest.mark.parametrize('control',['task.pause','task.cancel'])
def test_user_stop_after_final_receipts_precedes_callback_and_completion_display(serial,db_engine,control):
    api,parent,renderer,*_=serial
    complete_previous(serial,0);complete_previous(serial,1)
    api._mix.queue.run_once_until_idle()
    receive_child(serial,status(serial)['publish_task_ids'][-1])
    response=call(api,control,{'id':parent},'last-receipt-'+control.replace('.','-'))
    assert response['ok'],response
    api._daily.reconcile(parent);api._daily.recheck_serial();api._mix.queue.run_once_until_idle()
    assert api._mix.queue.get(parent).state==('paused' if control.endswith('pause') else 'cancelled')
    assert status(serial)['serial']['stage']=='paused'
    assert renderer.calls==renderer.tts_calls==3 and completion_count(db_engine,parent)==0


def test_committed_render_without_queue_statistics_is_adopted_after_reopen_once(serial,db_engine,app_paths,monkeypatch,daily_auth_sync):
    api,parent,renderer,*_=serial
    task=api._mix.queue.get(parent)
    api._mix.executor.render_item(parent,0,task.snapshot)
    with session_scope(db_engine) as session:
        assert session.get(Setting,f'mix-success:{parent}:0') is not None
        assert session.get(Setting,f'mix-result:{parent}:0') is None
        assert session.get(MixTask,parent).current_index==0
    assert renderer.calls==renderer.tts_calls==usage_count(db_engine)==1
    fresh=reopen(serial,db_engine,app_paths,monkeypatch,daily_auth_sync);reopened=fresh[0]
    try:
        recovered=reopened._mix.queue.get(parent)
        assert recovered.current_index==recovered.completed_count==recovered.success_count==1
        assert recovered.state=='paused'
        assert reopened._daily.status(parent)['waiting_reason']=='authorization'
        assert child_count(db_engine,parent)==0
        assert call(reopened,'task.resume',{'id':parent},'continue-adopted-render')['ok']
        reopened._mix.queue.run_once_until_idle()
        assert renderer.calls==renderer.tts_calls==usage_count(db_engine)==1
        assert child_count(db_engine,parent)==1
    finally:assert reopened.close(3)


def test_saved_child_before_reverse_binding_recovers_same_receipt_only_after_explicit_continue(serial,db_engine,monkeypatch):
    api,parent,renderer,*_=serial
    original=api._multi_publish.service.save;committed=[]
    def crash_after_commit(*args,**kwargs):
        receipt=original(*args,**kwargs);committed.append(receipt['id'])
        raise RuntimeError('受控故障：child已保存，尚未绑定父任务')
    monkeypatch.setattr(api._multi_publish.service,'save',crash_after_commit)
    api._mix.queue.run_once_until_idle()
    assert renderer.calls==renderer.tts_calls==1 and len(committed)==1
    child=committed[0]
    assert child_count(db_engine,parent)==1
    with session_scope(db_engine) as session:
        assert session.get(Setting,'multi_publish.daily_source:'+child) is None
        assert session.get(Setting,f'mix.daily.item:{parent}:0') is None
        assert authorized_targets(session,child)=={}
        before=[(row.key,row.value_json) for row in session.scalars(select(Setting))]
    for _ in range(3):call(api,'mix.daily.status',{'id':parent})
    with session_scope(db_engine) as session:
        assert [(row.key,row.value_json) for row in session.scalars(select(Setting))]==before
    monkeypatch.setattr(api._multi_publish.service,'save',original)
    assert call(api,'task.resume',{'id':parent},'continue-before-source')['ok']
    api._daily.reconcile(parent);api._mix.queue.run_once_until_idle()
    assert status(serial)['publish_task_ids']==[child]
    assert renderer.calls==renderer.tts_calls==usage_count(db_engine)==1
    assert child_count(db_engine,parent)==1


def test_missing_reverse_binding_on_existing_child_never_advances_or_creates_another_child(serial,db_engine):
    api,parent,renderer,*_=serial
    api._mix.queue.run_once_until_idle()
    child=status(serial)['publish_task_ids'][0]
    receive_child(serial,child)
    with session_scope(db_engine) as session:session.delete(session.get(Setting,'multi_publish.daily_source:'+child))
    for _ in range(3):
        call(api,'mix.daily.status',{'id':parent});api._daily.reconcile(parent);api._daily.recheck_serial()
        api._mix.queue.run_once_until_idle()
    assert renderer.calls==renderer.tts_calls==1
    assert child_count(db_engine,parent)==1
    assert status(serial)['serial']['waiting_reason']=='configuration'
    assert status(serial)['serial']['needs_action'] is True


def test_duplicate_final_callbacks_finish_once_without_more_rendering(serial,db_engine):
    api,parent,renderer,*_=serial
    complete_previous(serial,0);complete_previous(serial,1)
    api._mix.queue.run_once_until_idle()
    receive_child(serial,status(serial)['publish_task_ids'][-1])
    barrier=threading.Barrier(3);errors=[]
    def callback():
        try:barrier.wait(timeout=3);api._daily.reconcile(parent)
        except BaseException as error:errors.append(error)
    threads=[threading.Thread(target=callback) for _ in range(2)]
    for thread in threads:thread.start()
    barrier.wait(timeout=3)
    for thread in threads:thread.join(5)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert api._mix.queue.get(parent).state=='completed'
    assert renderer.calls==renderer.tts_calls==3
    assert completion_count(db_engine,parent)==1 and child_count(db_engine,parent)==3
