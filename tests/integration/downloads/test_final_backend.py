"""Finite repeated-start confirmation and durable background notifications."""
from dataclasses import asdict
import json
import time
from unittest.mock import patch

import pytest
from sqlalchemy import select, func

from yijianjifa.app_services.download_commands import DownloadCommands
from yijianjifa.features.downloads.contracts import DownloadError, DownloadOptions, StageResult
from yijianjifa.features.notifications.service import NotificationService
from yijianjifa.features.tasks.service import TaskCenterRow
from tests.integration.downloads.test_runtime import runtime, candidates, batch, command, detail, eventually, reopen, Provider, Fetcher, Asr


def start_params(worker,ids,**extra):
    return {'candidate_ids':ids,'options':{**asdict(DownloadOptions()),'model_ids':[]},
            'directory_grant_id':worker.query('download.output.read',{})['grant_id'],**extra}


def request(api,identity,params):
    return api.dispatch(json.dumps({'v':1,'request_id':identity,'operation':'download.start','params':params}),trusted_origin=True)


def task_count(worker):
    with worker.engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(TaskCenterRow).where(TaskCenterRow.kind=='download'))


def test_new_repeated_start_requires_confirmation_but_receipts_replay_without_new_batch(runtime):
    worker,_,fetcher,_,_=runtime;ids,_=candidates(worker,4)
    done=batch(worker,ids[:1]);eventually(lambda:detail(worker,done)['state']=='completed')
    preview=command(worker,'preview.prepare',{'candidate_id':ids[2]})
    eventually(lambda:worker.query('download.preview.status',{'job_id':preview['job_id']})['state']=='succeeded')
    source=batch(worker,ids[3:],save_video=False,asr_language='zh',model_ids=('whisper-small-multilingual',))
    eventually(lambda:detail(worker,source)['state']=='completed')
    fetcher.release.clear();active=batch(worker,ids[1:2]);eventually(lambda:fetcher.active==1)
    api=DownloadCommands(worker.engine,worker);params=start_params(worker,ids);before=task_count(worker)
    # The confirmation read uses only the same caller transaction; no media IO.
    with patch.object(worker.files,'_hash',side_effect=AssertionError('confirmation must not hash')):
        first=request(api,'repeat_unconfirmed',params)
    assert first['ok'] and first['data']=={'confirmation_required':True,'existing_count':3,'active_count':1}
    assert task_count(worker)==before
    assert request(api,'repeat_unconfirmed',params)==first and task_count(worker)==before
    confirmed=request(api,'repeat_confirmed',{**params,'confirm_existing':True})
    assert confirmed['ok'] and confirmed['data']['count']==4
    assert task_count(worker)==before+1
    assert request(api,'repeat_confirmed',{**params,'confirm_existing':True})==confirmed
    assert task_count(worker)==before+1
    task=confirmed['data']['task_id']
    assert {row['candidate_id'] for row in detail(worker,task)['items']}==set(ids)
    # Replaying the original cold request also returns its existing task receipt.
    command(worker,'cancel',{'task_id':active});command(worker,'cancel',{'task_id':task})


def test_start_confirmation_is_an_optional_finite_boolean(runtime):
    worker,_,_,_,_=runtime;ids,_=candidates(worker)
    params=start_params(worker,ids)
    DownloadCommands.validate('download.start',params)
    DownloadCommands.validate('download.start',{**params,'confirm_existing':False})
    DownloadCommands.validate('download.start',{**params,'confirm_existing':True})
    for value in (None,1,'true'):
        with pytest.raises(DownloadError) as error:
            DownloadCommands.validate('download.start',{**params,'confirm_existing':value})
        assert error.value.code=='input_limit'


@pytest.mark.parametrize('outcome',['success','partial','failed'])
def test_background_completion_notifies_without_workspace_poll_and_reopen_does_not_repeat(runtime,outcome):
    worker,provider,fetcher,models,asr=runtime;ids,_=candidates(worker)
    if outcome=='failed':fetcher.fail_next=True
    if outcome=='partial':
        original=worker.processor.run
        def fail_audio(source,kind,**kwargs):
            if kind=='wav':raise DownloadError('media_failed')
            return original(source,kind,**kwargs)
        worker.processor.run=fail_audio
    task=batch(worker,ids,**({'audio_format':'wav'} if outcome=='partial' else {}))
    notifications=NotificationService(worker.engine,sound_enabled=False)
    # No view/detail polling is involved in receiving this background signal.
    history=eventually(lambda:notifications.history_for(task))
    assert len(history)==1 and history[0].deep_link=='task:'+task and history[0].read is False
    expected={'success':('download_completed','成功 1','部分完成 0','失败 0'),
              'partial':('download_completed_with_errors','成功 0','部分完成 1','失败 0'),
              'failed':('download_failed','成功 0','部分完成 0','失败 1')}[outcome]
    assert history[0].status==expected[0]
    assert all(value in history[0].message for value in expected[1:])
    assert '下载' in history[0].message and '跳过 0' in history[0].message and '取消 0' in history[0].message
    assert '发布' not in history[0].message and '制作' not in history[0].message
    for _ in range(3):worker.poll(task);worker._progress_for(('item','ignored'))(type('P',(),{'stage':'download','current':1,'total':2})())
    assert len(notifications.history_for(task))==1
    assert eventually(lambda:worker.close(.05))
    replacement=Fetcher(fetcher.source)
    other=reopen(worker,provider,replacement,models,asr)
    try:
        time.sleep(.1)
        assert len(notifications.history_for(task))==1 and replacement.calls==0
    finally:assert eventually(lambda:other.close(.05))


def test_background_auth_intervention_is_download_specific_and_not_replayed_on_restart(runtime):
    worker,provider,fetcher,models,asr=runtime;ids,_=candidates(worker);provider.fail=True
    task=batch(worker,ids)
    notifications=NotificationService(worker.engine,sound_enabled=False)
    history=eventually(lambda:notifications.history_for(task))
    assert len(history)==1 and history[0].status=='action_required'
    assert history[0].deep_link=='task:'+task and '下载' in history[0].message and '手动验证' in history[0].message
    assert '发布' not in history[0].message and 'private' not in history[0].message
    eventually(lambda:detail(worker,task)['state']=='paused_auth')
    for _ in range(3):worker.poll(task)
    assert len(notifications.history_for(task))==1
    assert eventually(lambda:worker.close(.05))
    other=reopen(worker,provider,Fetcher(fetcher.source),models,asr)
    try:
        time.sleep(.1);assert len(notifications.history_for(task))==1
    finally:assert eventually(lambda:other.close(.05))


@pytest.mark.parametrize('reason',['watermark','model'])
def test_ordinary_paused_work_notifies_the_specific_required_action(runtime,reason):
    worker,provider,fetcher,models,_=runtime;ids,_=candidates(worker)
    if reason=='watermark':provider.watermark='unknown'
    else:models.ready=False
    task=batch(worker,ids,**({'asr_language':'zh','model_ids':('whisper-small-multilingual',)} if reason=='model' else {}))
    notifications=NotificationService(worker.engine,sound_enabled=False)
    history=eventually(lambda:notifications.history_for(task))
    expected='原样下载或跳过' if reason=='watermark' else '下载或导入所需识别模型'
    assert len(history)==1 and history[0].status=='action_required' and expected in history[0].message
    assert '登录' not in history[0].message and history[0].deep_link=='task:'+task
    for _ in range(3):worker.poll(task)
    assert len(notifications.history_for(task))==1
    if reason=='watermark':
        item=detail(worker,task)['items'][0]['id']
        command(worker,'decision',{'task_id':task,'item_id':item,'choice':'skip'})
    else:
        models.ready=True;command(worker,'resume',{'task_id':task,'show_browser':False})
    history=eventually(lambda:notifications.history_for(task) if len(notifications.history_for(task))==2 else None)
    assert history[-1].status=='download_completed'
    assert ('跳过 1' if reason=='watermark' else '成功 1') in history[-1].message


def test_notification_and_transition_marker_rollback_with_task_state(runtime):
    from yijianjifa.app_services.download_runtime import DownloadRuntime
    worker,provider,fetcher,models,asr=runtime;ids,_=candidates(worker);params=start_params(worker,ids)
    assert eventually(lambda:worker.close(.05))
    bare=DownloadRuntime(worker.engine,worker.paths,media=worker.media,providers={'direct':provider},fetcher=fetcher,models=models,asr=asr)
    try:
        task=command(bare,'start',params)['task_id'];item=detail(bare,task)['items'][0]['id']
        with bare.engine.connect() as connection:
            transaction=connection.begin()
            bare.repository.record_stage(connection,item,StageResult('video','failed',error_code='network'))
            bare._refresh(connection,task)
            transaction.rollback()
        assert not NotificationService(worker.engine,sound_enabled=False).history_for(task)
        with bare.engine.connect() as connection:assert bare._get(connection,'notice',task) is None
        assert detail(bare,task)['state']=='waiting'
        with bare.engine.begin() as connection:
            bare.repository.record_stage(connection,item,StageResult('video','failed',error_code='network'))
            bare._refresh(connection,task)
        assert len(NotificationService(worker.engine,sound_enabled=False).history_for(task))==1
    finally:assert bare.close()
