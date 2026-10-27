"""发布报告使用真实成品名，并区分未提交和可能已提交。"""
import json
from sqlalchemy import select
from test_daily_serial_flow import serial,status,receive_child,call
from yijianjifa.storage.db import session_scope
from yijianjifa.features.finished.service import FinishedVideo
from yijianjifa.features.publish.coordinator import AdapterOutcome
from yijianjifa.features.publish.deliveries import PublishDeliveryRow
from yijianjifa.storage.models import Setting


def test_report_lists_exact_finished_filename_and_failed_account_without_republishing(serial,monkeypatch):
    api,parent,renderer,events,adapters,_=serial
    original=adapters['douyin'].submit;failed=[]
    def submit(payload,*,context,before_final):
        if payload['platform_user_id']=='111' and not failed:
            failed.append(payload['video_id'])
            return AdapterOutcome('pre_submit_failed',error_code='adapter_failed',error_stage='form_preparation',error_category='cover_unconfirmed')
        return original(payload,context=context,before_final=before_final)
    monkeypatch.setattr(adapters['douyin'],'submit',submit)
    for _ in range(3):
        api._mix.queue.run_once_until_idle();receive_child(serial,status(serial)['publish_task_ids'][-1]);api._daily.reconcile(parent)
    before=list(events)
    report=status(serial)['execution']['report']
    assert report['ready'] and report['accepted']==8 and report['total']==9
    assert len(report['missed'])==1
    issue=report['missed'][0]
    with session_scope(api._engine) as s:
        video=s.get(FinishedVideo,failed[0])
        assert issue['filename']==video.display_name
        row=s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.video_id==video.id,PublishDeliveryRow.platform_user_id=='111'))
        assert issue['title']==json.loads(row.snapshot_json)['fields']['title']
    assert '合成账号111' in issue['account_label'] and issue['state']=='not_submitted'
    assert issue['filename'] in report['text'] and '封面' in report['text']
    assert events==before and renderer.calls==3


def test_cancel_keeps_platform_acceptance_in_report_even_when_local_targets_hidden(serial):
    api,parent,*_=serial
    api._mix.queue.run_once_until_idle();receive_child(serial,status(serial)['publish_task_ids'][-1])
    assert call(api,'task.cancel',{'id':parent},'cancel-for-report')['ok']
    report=status(serial)['execution']['report']
    assert report['ready'] and report['accepted']==3 and report['unmade']==2
    assert not report['missed']


def test_report_keeps_platform_receipts_after_user_deletes_finished_files(serial):
    from yijianjifa.app_services.finished_delete import FinishedDeletion
    api,parent,renderer,events,*_=serial
    for _ in range(3):
        api._mix.queue.run_once_until_idle();receive_child(serial,status(serial)['publish_task_ids'][-1]);api._daily.reconcile(parent)
    with session_scope(api._engine) as s:
        ids=list(s.scalars(select(FinishedVideo.id)))
        children={r.task_id for r in s.scalars(select(PublishDeliveryRow))}
    for child in children:assert call(api,'task.delete',{'id':child},'delete-'+child)['ok']
    before=list(events)
    for video_id in ids:assert FinishedDeletion(api._engine).delete(video_id)['deleted']
    report=status(serial)['execution']['report']
    assert report['accepted']==9 and report['unmade']==0 and report['removed']==3
    assert '已从成品库删除：3 条' in report['text']
    assert events==before and renderer.calls==3


def test_damaged_binding_is_reported_for_verification_without_guessing_success_or_missing(serial):
    api,parent,*_=serial
    api._mix.queue.run_once_until_idle();receive_child(serial,status(serial)['publish_task_ids'][-1])
    assert call(api,'task.pause',{'id':parent},'pause-for-report')['ok']
    with session_scope(api._engine) as s:
        row=s.get(Setting,'mix.daily.item:'+parent+':0');saved=json.loads(row.value_json)
        saved['snapshot_digest']='damaged';row.value_json=json.dumps(saved)
    report=status(serial)['execution']['report']
    assert report['ready'] and report['accepted']==0
    assert len(report['missed'])==3 and all(i['state']=='unknown' for i in report['missed'])
    assert '核对平台后台' in report['text']


def test_completed_report_survives_local_target_cleanup_without_restoring_execution(serial):
    api,parent,renderer,events,*_=serial
    for _ in range(3):
        api._mix.queue.run_once_until_idle();receive_child(serial,status(serial)['publish_task_ids'][-1]);api._daily.reconcile(parent)
    assert status(serial)['execution']['report']['accepted']==9
    before=list(events)
    with session_scope(api._engine) as s:
        for row in s.scalars(select(PublishDeliveryRow)):
            row.hidden=True
    saved=status(serial)
    assert saved['execution']['report']['ready'] and saved['execution']['report']['accepted']==9
    assert saved['accepted']==9 and saved['pending']==0 and saved['can_continue'] is False
    assert saved['execution']['closed_report'] is True and saved['execution']['serial'] is None
    assert saved['phase']=='accepted' and renderer.calls==3 and events==before
