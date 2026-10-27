"""用真实默认每日流程、持久成品及正式发布边界验证逐条门闩；仅平台与渲染IO离线。"""
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_runtime import authorized_targets
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.mixing.ffmpeg_renderer import RenderResult
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def call(api, op, params=None, rid='serial-read'):
    return api.dispatch(json.dumps(dict(v=1, request_id=rid, operation=op, params=params or {})), trusted_origin=True)


class SerialAdapter:
    verified_capabilities = {'ordinary', 'publish_now', 'platform_schedule', 'result_query'}

    def __init__(self, platform, events):
        self.platform_id, self.events = platform, events
        self.outcome = 'platform_scheduled'

    def validate_target(self, target, payload, execution):
        return AdapterValidation(payload['platform_user_id'], True)

    def submit(self, payload, *, context, before_final):
        before_final()
        self.events.append(('received', payload['fields']['title'], payload['platform_user_id']))
        return AdapterOutcome(self.outcome, scheduled_at=payload['scheduled_at'],
            receipt_reference={'work_id': 'synthetic-' + payload['video_id']},
            error_code='timeout' if self.outcome == 'result_unknown' else None)

    def reconcile(self, payload, *, context):
        return AdapterOutcome('platform_scheduled', scheduled_at=payload['scheduled_at'],
                              receipt_reference={'work_id': 'synthetic-' + payload['video_id']})


class SerialRenderer:
    def __init__(self, events):
        self.events, self.calls = events, 0
        self.fail_once = False

    def render(self, request):
        self.events.append(('render', self.calls))
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise ValueError('合成制作失败')
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(('serial-render-' + str(self.calls)).encode())
        return RenderResult(request.output_path, sum(s.duration_ms for s in request.segments), 720, 1280)


@pytest.fixture
def serial(db_engine, app_paths, tmp_path, monkeypatch, daily_auth_sync, request):
    media = tmp_path / 'synthetic.mp4'; media.write_bytes(b'synthetic source')
    MediaStorageService(db_engine, app_paths, kind='video').import_media(media, keep_copy=False, category_ids=[])
    for index in range(3):
        CopyRepository(db_engine).create(id=f'SERIAL-{index}', title=f'原文案{index}', voice='合成口播', body=f'正文{index}', tags='合成', full='')
    accounts = [AccountService(db_engine).create(platform, uid, '合成账号' + uid)
                for platform, uid in [('douyin', '111'), ('douyin', '222'), ('kuaishou', '333')]]
    options=getattr(request,'param',3)
    options=options if isinstance(options,dict) else {'accounts':options}
    accounts=accounts[:options.get('accounts',3)]
    with session_scope(db_engine) as s:
        for account in accounts: s.get(AccountRow, account.id).session_ref = 'synthetic-session-' + account.platform_user_id
    events = []
    adapters = {p: SerialAdapter(p, events) for p in ('douyin', 'kuaishou')}
    api = DesktopApi(db_engine, app_paths, publish_adapters=adapters)
    daily_auth_sync(api)
    monkeypatch.setattr(api._multi_publish, 'wake', lambda: None)
    renderer = SerialRenderer(events); api._mix.executor.renderer = renderer
    renderer.tts_calls = 0
    def voice(text, selected, speed, folder, tts):
        renderer.tts_calls += 1
        Path(folder).mkdir(parents=True, exist_ok=True)
        audio = Path(folder) / '合成.wav'; audio.write_bytes(b'synthetic audio')
        return SimpleNamespace(output_path=audio, duration_ms=1000, sentence_spans=[])
    monkeypatch.setattr('yijianjifa.features.mixing.desktop_service.synthesize_sentences', voice)
    api._mix.probe = lambda path: 100000
    params = {'mix': {'quantity': options.get('quantity',3), 'total_ms': 1000, 'segment_ms': 1000},
              'publishing': {'targets': [{'account_id': a.id, 'platform': a.platform} for a in accounts],
                  'mode': 'platform_schedule', 'time': '12:00', 'day_offset': 1, 'interval_hours': 3}}
    submitted = call(api, 'mix.daily.submit', params, 'serial-click')
    assert submitted['ok'], submitted
    yield api, submitted['data']['id'], renderer, events, adapters, accounts
    api.close(3)


def status(serial):
    return serial[0]._daily.status(serial[1])


def receive_child(serial, child):
    api = serial[0]
    with session_scope(api._engine) as s:
        ids = list(authorized_targets(s, child))
    for _ in range(len(ids)):
        api._multi_publish.coordinator.run_once(child, ids)


@pytest.mark.parametrize('control',['task.pause','task.cancel'])
def test_serial_cancel_releases_children_without_hiding_original_evidence(serial,monkeypatch,control):
    api,parent,renderer,_,adapters,_=serial
    adapters['douyin'].outcome='result_unknown'
    api._mix.queue.run_once_until_idle()
    child=status(serial)['publish_task_ids'][0];receive_child(serial,child)
    with session_scope(api._engine) as s:
        rows=list(s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==child)))
        ids=[r.id for r in rows]
        before={a.delivery_id:a.final_json for a in s.scalars(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id.in_(ids)))}
    closed=[];monkeypatch.setattr(api._multi_publish,'cancel_session',closed.append)
    result=call(api,control,{'id':parent},'cancel-with-children')
    assert result['ok'],result
    assert set(closed)==(set(ids) if control=='task.cancel' else set())
    if control=='task.cancel':
        assert call(api,control,{'id':parent},'cancel-with-children')['ok']
        cleaned=len(closed)
        assert not call(api,control,{'id':'different-parent'},'cancel-with-children')['ok']
        assert len(closed)==cleaned
    with session_scope(api._engine) as s:
        for did in ids:
            row=s.get(PublishDeliveryRow,did)
            assert row.cancel_requested==(control=='task.cancel')
            assert not row.hidden
        assert {a.delivery_id:a.final_json for a in s.scalars(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id.in_(ids)))}==before
    assert renderer.calls==1


def test_new_default_makes_one_then_all_three_accounts_receive_before_next(serial):
    api, parent, renderer, events, _, accounts = serial
    for index in range(3):
        api._mix.queue.run_once_until_idle()
        assert renderer.calls == index + 1, '全部账号接收前不能制作下一条'
        progress = status(serial)
        assert progress['serial']['item_index'] == index
        assert progress['total'] == 9 and progress['failed'] == 0
        assert len(progress['publish_task_ids']) == index + 1
        child = progress['publish_task_ids'][-1]
        detail = api._multi_publish.detail(child)
        assert {d['account_id'] for d in detail['deliveries']} == {a.id for a in accounts}
        assert len(detail['deliveries']) == 3
        snapshot = api._mix.queue.get(parent).snapshot
        with session_scope(api._engine) as s:
            vid = json.loads(s.get(Setting, f'mix-success:{parent}:{index}').value_json)['finished_id']
            for delivery in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child)):
                payload = json.loads(delivery.snapshot_json)
                assert delivery.video_id == vid
                assert {k: payload['fields'][k] for k in ('title', 'body', 'tags')} == {
                    k: snapshot['copies'][index][k] for k in ('title', 'body', 'tags')}
        receive_child(serial, child)
        api._daily.reconcile(parent)
    assert [e[0] for e in events] == ['render', 'received', 'received', 'received'] * 3
    assert status(serial)['serial']['stage'] == 'completed'
    assert api._mix.queue.get(parent).state == 'completed'


def test_one_batch_reuses_online_checks_but_validates_every_publish_target(serial,monkeypatch):
    api,parent,renderer,events,adapters,accounts=serial
    checks=[];validations=[]
    original=api._daily.auth.start
    def check(task_id):checks.append(task_id);return original(task_id)
    monkeypatch.setattr(api._daily.auth,'start',check)
    for adapter in adapters.values():
        validate=adapter.validate_target
        def observed(target,payload,execution,validate=validate):
            validations.append((payload['video_id'],payload['platform_user_id']));return validate(target,payload,execution)
        monkeypatch.setattr(adapter,'validate_target',observed)
    # The fixture already completed the batch's initial online check.
    for index in range(3):
        api._mix.queue.run_once_until_idle()
        child=status(serial)['publish_task_ids'][-1]
        receive_child(serial,child);api._daily.reconcile(parent)
    assert renderer.calls==3 and len([e for e in events if e[0]=='received'])==9
    assert checks==[], '同一批次不应在每个视频前重开全部账号的登录检查浏览器'
    assert len(set(validations))==9 and {uid for _,uid in validations}=={a.platform_user_id for a in accounts}


@pytest.mark.parametrize('uid',['111','333'])
@pytest.mark.parametrize('rejected',[False,True])
def test_one_failed_publication_keeps_other_accounts_and_next_videos_running(serial,monkeypatch,uid,rejected):
    api,parent,renderer,events,adapters,_=serial
    calls=[]
    for adapter in adapters.values():
        original=adapter.submit
        def submit(payload,*,context,before_final,original=original):
            key=(payload['video_id'],payload['platform_user_id']);calls.append(key)
            if payload['platform_user_id']==uid and sum(k[1]==uid for k in calls)==1:
                if rejected:before_final()
                return AdapterOutcome('platform_rejected' if rejected else 'pre_submit_failed',
                    error_code='platform_rejected' if rejected else 'adapter_failed',
                    error_stage=None if rejected else 'form_preparation',
                    error_category=None if rejected else 'cover_unconfirmed')
            return original(payload,context=context,before_final=before_final)
        monkeypatch.setattr(adapter,'submit',submit)
    first=None
    for index in range(3):
        api._mix.queue.run_once_until_idle()
        assert renderer.calls==index+1
        child=status(serial)['publish_task_ids'][-1]
        receive_child(serial,child)
        first=first or child
        api._daily.reconcile(parent)
    progress=status(serial)
    assert progress['serial']['stage']=='completed'
    assert progress['serial']['targets_received']==8 and progress['serial']['error_count']==1
    assert len(calls)==len(set(calls))==9
    with session_scope(api._engine) as s:
        row=s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==first,PublishDeliveryRow.platform_user_id==uid))
        assert s.get(TaskItemRow,row.item_id).state==('platform_rejected' if rejected else 'pre_submit_failed')
        attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id))
        assert bool(attempt.final_json)==rejected


def test_unknown_account_is_held_while_other_accounts_finish_all_videos(serial,monkeypatch):
    api,parent,renderer,_,adapters,accounts=serial
    original=adapters['douyin'].submit;calls=[]
    def submit(payload,*,context,before_final):
        calls.append(payload['platform_user_id'])
        if payload['platform_user_id']=='111':
            before_final();return AdapterOutcome('result_unknown',error_code='result_unknown')
        return original(payload,context=context,before_final=before_final)
    monkeypatch.setattr(adapters['douyin'],'submit',submit)
    for index in range(3):
        api._mix.queue.run_once_until_idle()
        assert renderer.calls==index+1
        child=status(serial)['publish_task_ids'][-1]
        receive_child(serial,child);api._daily.reconcile(parent)
    progress=status(serial)['serial']
    assert progress['stage']=='completed' and progress['targets_received']==6
    assert progress['deferred_count']==3 and progress['error_count']==0
    assert calls.count('111')==1 and calls.count('222')==3
    report=status(serial)['execution']['report']
    assert [issue['state'] for issue in report['missed']].count('unknown')==1
    assert [issue['state'] for issue in report['missed']].count('not_submitted')==2
    with session_scope(api._engine) as s:
        rows=list(s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.account_id==accounts[0].id)))
        assert sorted(s.get(TaskItemRow,row.item_id).state for row in rows)==['result_unknown','waiting','waiting']
        assert sum(s.get(Setting,'multi_publish.target_pause:'+row.id) is not None for row in rows)==2


def test_one_logged_out_account_does_not_stop_verified_accounts(serial):
    api,parent,renderer,_,_,accounts=serial
    with api._engine.connect() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE');api._daily.prepare_continue(c,parent,automatic=True);c.commit()
    targets=api._daily.auth_begin(parent)
    observations=[]
    for index,target in enumerate(targets):
        if index==0:AccountService(api._engine).set_login_state(target['account_id'],'expired')
        observations.append({**target,'state':'login_required' if index==0 else 'valid','reason':'login_required' if index==0 else ''})
    api._daily.auth_update(parent,phase='needs_login',login_accounts=observations)
    assert status(serial)['phase'] not in {'needs_login','needs_attention'},status(serial).get('reason')
    for index in range(3):
        api._mix.queue.run_once_until_idle()
        assert renderer.calls==index+1
        receive_child(serial,status(serial)['publish_task_ids'][-1]);api._daily.reconcile(parent)
    assert status(serial)['serial']['targets_received']==6


def test_video_number_is_shared_by_all_accounts_and_follows_making_order(serial):
    api,parent,*_=serial
    for index in range(2):
        api._mix.queue.run_once_until_idle()
        child=status(serial)['publish_task_ids'][-1]
        receive_child(serial,child)
        with session_scope(api._engine) as s:
            for delivery in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==child)):
                s.add(Setting(key=f'publish.execution:{delivery.id}:1',value_json=json.dumps([
                    {'time':f'2026-10-27T09:10:0{index}Z','step':'douyin.upload'},
                    {'time':f'2026-10-27T09:10:1{index}Z','step':'execution.result','checks':{'state':'platform_scheduled'}}])))
        records=[event for event in status(serial)['execution']['events'] if event.get('stage')=='publishing']
        assert {event['title'] for event in records}=={f'第{i+1}个视频' for i in range(index+1)}
        for number in range(index+1):
            uploads=[event for event in records if event['title']==f'第{number+1}个视频' and event['message']=='上传视频']
            assert len(uploads)==3 and all(event['account_label'] for event in uploads)
        api._daily.reconcile(parent)


def test_one_daily_limit_skips_only_that_account_and_all_other_videos_continue(serial,monkeypatch):
    api,parent,renderer,events,adapters,accounts=serial
    original=adapters['douyin'].submit
    def submit(payload,*,context,before_final):
        if payload['platform_user_id']=='111':
            before_final();events.append(('limited',payload['fields']['title'],'111'))
            return AdapterOutcome('platform_rejected',error_code='douyin_daily_post_limit')
        return original(payload,context=context,before_final=before_final)
    monkeypatch.setattr(adapters['douyin'],'submit',submit)
    for index in range(3):
        api._mix.queue.run_once_until_idle()
        assert renderer.calls==index+1
        child=status(serial)['publish_task_ids'][-1]
        receive_child(serial,child);api._daily.reconcile(parent)
    progress=status(serial)
    assert renderer.calls==3
    assert sum(e[0]=='limited' for e in events)==1
    assert sum(e[0]=='received' for e in events)==6
    assert progress['serial']['stage']=='completed'
    assert progress['serial']['skipped_count']==3
    assert progress['serial']['targets_received']==6
    assert progress['serial']['items_received']==0
    assert not progress['serial']['needs_action']
    with session_scope(api._engine) as db:
        limited=[]
        for row in db.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.account_id==accounts[0].id)):
            attempt=db.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id))
            limited.append((json.loads(attempt.result_json)['error_code'],bool(attempt.final_json)))
        assert sorted(limited)==sorted([('douyin_daily_post_limit',True),('posting_limit_skipped',False),('posting_limit_skipped',False)])


@pytest.mark.parametrize('serial',[1],indirect=True)
def test_single_limited_account_never_claims_other_accounts_received(serial,monkeypatch):
    from yijianjifa.features.mixing.serial_gate import inspect_item
    api,parent,_,_,adapters,_=serial
    def submit(payload,*,context,before_final):
        before_final();return AdapterOutcome('platform_rejected',error_code='douyin_daily_post_limit')
    monkeypatch.setattr(adapters['douyin'],'submit',submit)
    api._mix.queue.run_once_until_idle();receive_child(serial,status(serial)['publish_task_ids'][0])
    with session_scope(api._engine) as db:
        item=inspect_item(db,db.get(MixTask,parent),0)
    assert item['received']==0 and item['skipped_count']==1
    assert '抖音' in item['reason'] and '合成账号111' in item['reason'] and '未发布' in item['reason']
    assert '其余账号' not in item['reason'] and '已接收' not in item['reason']


def test_all_accounts_limited_pause_before_rendering_more_paid_audio(serial,monkeypatch):
    api,parent,renderer,events,adapters,_=serial
    def submit(payload,*,context,before_final):
        before_final();events.append(('limited',payload['fields']['title'],payload['platform_user_id']))
        return AdapterOutcome('platform_rejected',error_code='platform_daily_post_limit')
    for adapter in adapters.values():monkeypatch.setattr(adapter,'submit',submit)
    api._mix.queue.run_once_until_idle()
    receive_child(serial,status(serial)['publish_task_ids'][0]);api._daily.reconcile(parent)
    api._mix.queue.run_once_until_idle()
    assert renderer.calls==1 and renderer.tts_calls==1
    progress=status(serial)
    assert progress['serial']['stage']=='paused'
    assert progress['serial']['needs_action']
    assert '全部账号' in progress['serial']['reason'] and '上限' in progress['serial']['reason']


def test_limit_arriving_before_other_receipts_does_not_stop_reauthorization(serial,monkeypatch):
    api,parent,_,_,adapters,accounts=serial
    original=adapters['douyin'].submit
    def submit(payload,*,context,before_final):
        if payload['platform_user_id']=='111':
            before_final();return AdapterOutcome('platform_rejected',error_code='douyin_daily_post_limit')
        return original(payload,context=context,before_final=before_final)
    monkeypatch.setattr(adapters['douyin'],'submit',submit)
    api._mix.queue.run_once_until_idle();child=status(serial)['publish_task_ids'][0]
    detail=api._multi_publish.detail(child)
    limited=next(d for d in detail['deliveries'] if d['account_id']==accounts[0].id)
    assert api._multi_publish.coordinator.run_once(child,[limited['id']])=='platform_rejected'
    api._daily.reconcile(parent)
    assert not status(serial).get('reason_code')=='daily_serial_runtime_error'
    with session_scope(api._engine) as db:
        proof=json.loads(db.get(Setting,'mix.daily.status:'+parent).value_json)
        assert not proof.get('automatic_stopped')
    receive_child(serial,child);api._daily.reconcile(parent)
    assert status(serial)['serial']['item_index']==1


def test_last_item_and_unknown_remain_waiting_without_being_counted_failed(serial):
    api, parent, renderer, _, adapters, _ = serial
    for adapter in adapters.values():adapter.outcome = 'result_unknown'
    api._mix.queue.run_once_until_idle()
    assert renderer.calls == 1
    child = status(serial)['publish_task_ids'][0]
    receive_child(serial, child)
    api._daily.reconcile(parent)
    api._mix.queue.run_once_until_idle()
    progress = status(serial)
    assert renderer.calls == 1
    assert progress['failed'] == 0 and progress['serial']['deferred_count']==3
    assert progress['serial']['stage'] == 'paused' and progress['needs_action'] is True
    assert '全部账号' in progress['reason']


def test_status_reads_never_dispatch_or_authorize(serial, monkeypatch):
    api, parent, renderer, _, _, _ = serial
    def forbidden(*a, **k): raise AssertionError('只读接口禁止启动工作')
    monkeypatch.setattr(api._daily, 'reconcile', forbidden)
    monkeypatch.setattr(api._multi_publish, 'wake', forbidden)
    for _ in range(3):
        assert call(api, 'mix.daily.status', {'id': parent})['ok']
    assert renderer.calls == 0 and status(serial)['publish_task_ids'] == []


@pytest.mark.parametrize('accepted', ['published', 'platform_scheduled', 'platform_accepted', 'processing', 'reviewing'])
def test_all_receiving_states_release_next_item_without_waiting_for_publication(serial, accepted):
    api, parent, renderer, _, adapters, _ = serial
    for adapter in adapters.values(): adapter.outcome = accepted
    api._mix.queue.run_once_until_idle()
    receive_child(serial, status(serial)['publish_task_ids'][0])
    api._daily.reconcile(parent)
    api._mix.queue.run_once_until_idle()
    assert renderer.calls == 2


@pytest.mark.parametrize('corruption', ['hidden', 'cancelled', 'digest', 'source', 'missing'])
def test_incomplete_or_contradictory_gate_never_claims_next(serial, corruption):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    child = status(serial)['publish_task_ids'][0]
    receive_child(serial, child)
    with session_scope(api._engine) as s:
        delivery = s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child))
        if corruption == 'hidden': delivery.hidden = True
        elif corruption == 'cancelled': s.get(TaskItemRow, delivery.item_id).state = 'cancelled'
        elif corruption == 'source': s.delete(s.get(Setting, 'multi_publish.daily_source:' + child))
        else:
            attempt = s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery.id))
            if corruption == 'digest':
                evidence = json.loads(attempt.result_json); evidence['request_digest'] = '0' * 64
                attempt.result_json = json.dumps(evidence)
            else: s.delete(attempt)
    api._daily.reconcile(parent)
    with session_scope(api._engine) as s: s.get(MixTask, parent).state = 'waiting'
    api._mix.queue.run_once_until_idle()
    assert renderer.calls == 1


@pytest.mark.parametrize('control', ['task.pause', 'task.cancel'])
def test_user_stop_beats_last_received_callback(serial, control):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    receive_child(serial, status(serial)['publish_task_ids'][0])
    response = call(api, control, {'id': parent}, control.replace('.', '-'))
    assert response['ok'], response
    api._daily.reconcile(parent); api._daily.recheck_serial()
    api._mix.queue.run_once_until_idle()
    assert renderer.calls == 1
    assert api._mix.queue.get(parent).state == ('paused' if control.endswith('pause') else 'cancelled')


def test_lost_callback_is_repaired_by_authorized_runtime_recheck(serial):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    receive_child(serial, status(serial)['publish_task_ids'][0])
    assert api._mix.queue.get(parent).state == 'waiting_publish'
    api._daily.recheck_serial()
    api._mix.queue.run_once_until_idle()
    assert renderer.calls == 2


def test_failed_item_explicit_retry_keeps_original_index_and_error_history(serial):
    api, parent, renderer, _, _, _ = serial
    renderer.fail_once = True
    api._mix.queue.run_once_until_idle()
    first = api._mix.queue.get(parent)
    assert first.current_index == 0 and first.failure_count == first.completed_count == 1
    assert status(serial)['publish_task_ids'] == []
    assert call(api, 'task.resume', {'id': parent}, 'retry-original')['ok']
    api._mix.queue.run_once_until_idle()
    current = api._mix.queue.get(parent)
    assert renderer.calls == renderer.tts_calls == 2
    assert current.current_index == current.completed_count == current.success_count == 1 and current.failure_count == 0
    with session_scope(api._engine) as s:
        history = json.loads(s.get(Setting, f'mix-history:{parent}:0').value_json)
        assert len(history) == 1 and history[0]['state'] == 'failed'
        assert s.get(Setting, f'mix-success:{parent}:1') is None


def test_independent_queue_after_claim_commit_does_not_render_or_tts_twice(serial):
    from yijianjifa.features.mixing.queue import MixQueue
    from yijianjifa.features.library.usage_progress import UsageProgress
    from sqlalchemy import func
    api, parent, renderer, _, _, _ = serial
    entered, finish = threading.Event(), threading.Event()
    original = renderer.render
    def slow(request):
        entered.set()
        assert finish.wait(10)
        return original(request)
    renderer.render = slow
    errors = []
    def first():
        try: api._mix.queue.run_once_until_idle()
        except BaseException as error: errors.append(error)
    thread = threading.Thread(target=first); thread.start()
    assert entered.wait(5)
    other = MixQueue(api._engine, api._mix.executor)
    other.serial_host_session = api._daily.session_id
    other.run_once_until_idle()
    finish.set(); thread.join(10)
    assert not thread.is_alive() and not errors
    assert renderer.calls == renderer.tts_calls == 1
    with session_scope(api._engine) as s:
        assert s.scalar(select(func.count()).select_from(UsageProgress)) == 1


def test_external_same_uid_session_change_stops_automatic_next_render(serial):
    api, parent, renderer, _, _, accounts = serial
    api._mix.queue.run_once_until_idle()
    receive_child(serial, status(serial)['publish_task_ids'][0])
    with session_scope(api._engine) as s: s.get(AccountRow, accounts[0].id).session_ref = 'external-unverified-session'
    api._daily.reconcile(parent); api._mix.queue.run_once_until_idle()
    assert renderer.calls == 1
    assert status(serial)['needs_action'] is True


def test_authorized_making_runtime_repairs_lost_render_notification(serial, monkeypatch):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.on_settled = None
    saved = threading.Event()
    original = api._multi_publish.service.save
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        saved.set()
        return result
    monkeypatch.setattr(api._multi_publish.service, 'save', observe)
    api._runtime.start()
    assert saved.wait(3), '已授权制作runtime须从持久success补偿丢失通知'
    assert renderer.calls == 1


def test_delete_waiting_parent_stops_future_flow_but_keeps_submission_evidence(serial):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    child = status(serial)['publish_task_ids'][0]
    response = call(api, 'task.delete', {'id': parent}, 'delete-waiting-parent')
    assert response['ok'], response
    receive_child(serial, child)
    api._daily.reconcile(parent); api._daily.recheck_serial(); api._mix.queue.run_once_until_idle()
    assert renderer.calls == 1
    with session_scope(api._engine) as s:
        assert s.get(Setting, 'task-hidden:' + parent) is not None
        proof = json.loads(s.get(Setting, 'mix.daily.status:' + parent).value_json)
        assert proof['automatic_stopped'] is True
        assert len(list(s.scalars(select(PublishDeliveryRow)))) == 3


def test_verified_sdk_rotations_preserve_snapshot_and_allow_following_item(serial, app_paths):
    from yijianjifa.webui.douyin_scheduled import persist_refreshed_sessions
    api, parent, renderer, _, _, accounts = serial
    original_snapshot = api._mix.queue.get(parent).snapshot
    api._mix.queue.run_once_until_idle()
    receive_child(serial, status(serial)['publish_task_ids'][0])
    for index in range(2):
        account = AccountService(api._engine).get(accounts[0].id)
        persist_refreshed_sessions(api._engine, app_paths, {account.id: {
            'original_ref': account.session_ref, 'platform_user_id': account.platform_user_id,
            'session_blob': f'sdk-verified-{index}'.encode()}})
    api._daily.reconcile(parent); api._mix.queue.run_once_until_idle()
    assert renderer.calls == 2
    assert api._mix.queue.get(parent).snapshot == original_snapshot


@pytest.mark.parametrize('field,value', [('copies', None), ('daily_publish.targets', None), ('daily_publish.slots', [])])
def test_damaged_new_flow_is_readable_but_cannot_render(serial, field, value):
    api, parent, renderer, _, _, _ = serial
    with session_scope(api._engine) as s:
        row = s.get(MixTask, parent); snapshot = json.loads(row.snapshot_json)
        if '.' in field:
            name, key = field.split('.'); snapshot[name][key] = value
        else: snapshot[field] = value
        row.snapshot_json = json.dumps(snapshot)
    response = call(api, 'mix.daily.status', {'id': parent})
    assert response['ok'], response
    assert response['data']['needs_action'] and response['data']['waiting_reason'] == 'configuration'
    api._mix.queue.run_once_until_idle()
    assert renderer.calls == 0


@pytest.mark.parametrize('kind', [RuntimeError, AssertionError])
def test_recheck_failure_is_visible_sanitized_and_does_not_block_other_parent(serial, monkeypatch, kind):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    with session_scope(api._engine) as s:
        row = s.get(MixTask, parent)
        other = MixTask(id='other-authorized-parent', snapshot_json=row.snapshot_json, state='waiting_publish', quantity=3)
        s.add(other)
        s.add(Setting(key='mix.daily.status:' + other.id, value_json=json.dumps({'auth_host': api._daily.session_id})))
    calls = []
    def broken(identity):
        calls.append(identity)
        if identity == parent: raise kind('SECRET-RAW-COOKIE-AND-PATH')
    monkeypatch.setattr(api._daily, 'reconcile', broken)
    if kind is AssertionError:
        with pytest.raises(AssertionError): api._daily.recheck_serial()
    else: api._daily.recheck_serial()
    assert 'other-authorized-parent' in calls
    progress = status(serial)
    assert progress['needs_action'] and kind.__name__ in progress['reason']
    assert 'SECRET-RAW' not in json.dumps(progress)
    with session_scope(api._engine) as s:
        proof = json.loads(s.get(Setting, 'mix.daily.status:' + parent).value_json)
        assert proof['automatic_stopped'] is True
        audit = json.loads(s.get(Setting, 'mix.daily.diagnostics:' + parent).value_json)
        assert len(audit) == 1 and audit[0]['error_type'] == kind.__name__
        assert 'SECRET-RAW' not in json.dumps(audit)


def test_controlled_reopen_does_not_declare_live_render_owner_stopped(serial):
    from yijianjifa.features.mixing.queue import MixQueue
    api, parent, renderer, _, _, _ = serial
    entered, finish = threading.Event(), threading.Event()
    original = renderer.render
    def slow(request):
        entered.set(); assert finish.wait(10)
        return original(request)
    renderer.render = slow
    thread = threading.Thread(target=lambda: api._mix.queue._run_single_item(parent)); thread.start()
    assert entered.wait(5)
    try:
        MixQueue(api._engine, api._mix.executor).recover_interrupted()
        assert api._mix.queue.get(parent).state == 'running'
    finally:
        finish.set(); thread.join(10)
    assert renderer.calls == renderer.tts_calls == 1


@pytest.mark.parametrize('state', ['paused_auth', 'result_unknown', 'interrupted', 'waiting_adapter', 'waiting_configuration', 'missed_schedule'])
def test_waiting_and_action_states_are_pending_not_failed(serial, state):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    child = status(serial)['publish_task_ids'][0]
    with session_scope(api._engine) as s:
        delivery = s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child))
        s.get(TaskItemRow, delivery.item_id).state = state
    progress = status(serial)
    assert progress['failed'] == progress['serial']['error_count'] == 0
    assert progress['pending'] == 9 and progress['needs_action_count'] == 1
    api._daily.reconcile(parent); api._mix.queue.run_once_until_idle()
    assert renderer.calls == 1


@pytest.mark.parametrize('corruption', ['missing', 'result_digest', 'final_digest', 'attempt_digest', 'payload_digest', 'result_state', 'no_final', 'no_result'])
def test_contradictory_received_evidence_requires_receipt_review_not_ordinary_wait(serial, corruption):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    child = status(serial)['publish_task_ids'][0]
    receive_child(serial, child)
    with session_scope(api._engine) as s:
        row = s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == child))
        attempt = s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == row.id))
        if corruption == 'missing': s.delete(attempt)
        elif corruption == 'attempt_digest': attempt.request_digest = '0' * 64
        elif corruption == 'payload_digest': row.request_digest = '0' * 64
        elif corruption == 'no_final': attempt.final_json = None
        elif corruption == 'no_result': attempt.result_json = None
        elif corruption == 'final_digest':
            final = json.loads(attempt.final_json); final['request_digest'] = '0' * 64
            attempt.final_json = json.dumps(final)
        else:
            evidence = json.loads(attempt.result_json)
            evidence['request_digest' if corruption == 'result_digest' else 'state'] = '0' * 64 if corruption == 'result_digest' else 'published'
            attempt.result_json = json.dumps(evidence)
    progress = status(serial)
    assert progress['failed'] == 0 and progress['accepted'] == 2
    assert progress['needs_action'] and progress['needs_action_count'] == 1
    assert progress['waiting_reason'] == 'result' and '回执' in progress['serial']['reason']
    api._daily.reconcile(parent); api._mix.queue.run_once_until_idle()
    assert renderer.calls == 1


@pytest.mark.parametrize('control', ['pause', 'cancel', 'close'])
def test_reopen_after_final_receipts_keeps_user_stop_and_never_notifies_completed(serial, control):
    from yijianjifa.features.mixing.queue import MixQueue
    from yijianjifa.features.notifications.service import NotificationRow
    from sqlalchemy import func
    api, parent, renderer, _, _, _ = serial
    for index in range(3):
        api._mix.queue.run_once_until_idle()
        receive_child(serial, status(serial)['publish_task_ids'][-1])
        if index < 2: api._daily.reconcile(parent)
    if control == 'close': api._mix.queue.request_pause(parent)
    else: assert call(api, 'task.' + control, {'id': parent}, 'stop-' + control)['ok']
    MixQueue(api._engine, api._mix.executor).recover_interrupted()
    assert api._mix.queue.get(parent).state == ('cancelled' if control == 'cancel' else 'paused')
    assert status(serial)['serial']['stage'] == 'paused'
    with session_scope(api._engine) as s:
        assert s.scalar(select(func.count()).select_from(NotificationRow).where(NotificationRow.task_id == parent)) == 0
    assert renderer.calls == renderer.tts_calls == 3


def test_result_settlement_cannot_overwrite_a_real_cancel_command(serial, monkeypatch):
    """旧无锁结算确定性插入取消；有写锁时让真实取消在线程外等事务提交。"""
    import inspect
    from sqlalchemy import event
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Session
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.on_settled = None
    original = Session.get
    threads, responses, injected = [], [], []
    def mark(connection, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith('BEGIN IMMEDIATE'): connection.info['serial_test_write_lock'] = True
    def clear(connection): connection.info.pop('serial_test_write_lock', None)
    event.listen(api._engine, 'before_cursor_execute', mark)
    event.listen(api._engine, 'commit', clear); event.listen(api._engine, 'rollback', clear)
    def cancel(): responses.append(call(api, 'task.cancel', {'id': parent}, 'cancel-at-settlement'))
    def get(session, entity, ident, *args, **kwargs):
        row = original(session, entity, ident, *args, **kwargs)
        frame = inspect.currentframe().f_back
        if entity is MixTask and ident == parent and renderer.calls == 1 and not injected and frame.f_code.co_name == '_run_single_item_inner':
            injected.append(True)
            bind = session.get_bind()
            if isinstance(bind, Connection) and bind.info.get('serial_test_write_lock'):
                thread = threading.Thread(target=cancel); threads.append(thread); thread.start()
            else: cancel()
        return row
    monkeypatch.setattr(Session, 'get', get)
    try:
        api._mix.queue.run_once_until_idle()
        for thread in threads: thread.join(10)
        assert injected and responses and all(r['ok'] for r in responses)
        assert api._mix.queue.get(parent).state == 'cancelled'
        progress = status(serial)
        assert progress['phase'] == 'cancelled' and progress['can_continue'] is False
        assert call(api, 'task.resume', {'id': parent}, 'cannot-resume-cancel')['ok'] is False
    finally:
        event.remove(api._engine, 'before_cursor_execute', mark)
        event.remove(api._engine, 'commit', clear); event.remove(api._engine, 'rollback', clear)


def test_persistent_cancel_intent_is_permanent_even_if_old_row_state_is_wrong(serial):
    api, parent, renderer, _, _, _ = serial
    api._mix.queue.run_once_until_idle()
    assert call(api, 'task.cancel', {'id': parent}, 'cancel-before-old-row')['ok']
    with session_scope(api._engine) as s: s.get(MixTask, parent).state = 'waiting_publish'
    progress = status(serial)
    assert progress['phase'] == 'cancelled' and progress['can_continue'] is False
    assert call(api, 'task.resume', {'id': parent}, 'reject-old-cancelled-row')['ok'] is False
    assert renderer.calls == 1


def test_reopen_after_one_received_authorizes_only_two_remaining_without_resubmit(serial, app_paths, monkeypatch, daily_auth_sync):
    api, parent, renderer, events, adapters, _ = serial
    api._mix.queue.run_once_until_idle()
    child = status(serial)['publish_task_ids'][0]
    with session_scope(api._engine) as s: ids = list(authorized_targets(s, child))
    api._multi_publish.coordinator.run_once(child, ids)
    original = api._multi_publish.detail(child)['deliveries']
    received = {d['id'] for d in original if d['state'] == 'platform_scheduled'}
    assert len(received) == 1
    assert api.close(3)
    reopened = DesktopApi(api._engine, app_paths, publish_adapters=adapters)
    reopened._mix.executor.renderer = renderer; reopened._mix.probe = lambda path: 100000
    daily_auth_sync(reopened); monkeypatch.setattr(reopened._multi_publish, 'wake', lambda: None)
    try:
        response = call(reopened, 'task.resume', {'id': parent}, 'resume-partially-received')
        assert response['ok'], response
        with session_scope(api._engine) as s:
            grants = authorized_targets(s, child)
            assert set(grants) == set(ids) - received
            assert all(s.get(PublishDeliveryRow, did).attempt == 1 for did in ids)
        assert renderer.calls == renderer.tts_calls == 1
        assert len([e for e in events if e[0] == 'received']) == 1
        assert reopened._daily.status(parent)['publish_task_ids'] == [child]
    finally: reopened.close(3)


@pytest.mark.parametrize('kind', ['normal', 'render_error', 'voice_pause'])
def test_making_has_no_future_publish_reason_but_real_error_reasons_are_preserved(serial, monkeypatch, kind):
    api, parent, renderer, _, _, _ = serial
    if kind == 'normal':
        assert status(serial)['serial']['stage'] == 'making'
        assert status(serial)['serial']['reason'] == ''
        api._mix.queue.run_once_until_idle()
        receive_child(serial, status(serial)['publish_task_ids'][0]); api._daily.reconcile(parent)
        assert status(serial)['serial']['stage'] == 'making'
        assert status(serial)['serial']['reason'] == ''
    elif kind == 'render_error':
        renderer.fail_once = True; api._mix.queue.run_once_until_idle()
        assert '失败' in status(serial)['serial']['reason']
    else:
        from yijianjifa.features.mixing.tts.hosted import VoicePaused
        def paused(*args): raise VoicePaused('authentication_required', '请先登录合成配音账号，再继续本条。')
        monkeypatch.setattr(api._mix.executor, 'render_item', paused)
        api._mix.queue.run_once_until_idle()
        assert status(serial)['serial']['reason'] == '请先登录合成配音账号，再继续本条。'


@pytest.mark.parametrize('marker', [None, False, '', 'per_item_v9', {}, []])
def test_present_but_invalid_flow_marker_never_falls_back_to_legacy_render_or_dispatch(serial, marker):
    api, parent, renderer, _, _, _ = serial
    with session_scope(api._engine) as s:
        row = s.get(MixTask, parent); snapshot = json.loads(row.snapshot_json)
        snapshot['daily_publish']['execution_flow'] = marker
        row.snapshot_json = json.dumps(snapshot)
    api._mix.queue.run_once_until_idle()
    assert renderer.calls == renderer.tts_calls == 0, '未知或损坏marker不能降级为旧bulk执行'
    progress = status(serial)
    assert progress['serial']['waiting_reason'] == 'configuration' and progress['needs_action']
    assert progress['phase'] == 'needs_attention' and progress['publish_task_ids'] == []
    assert progress['can_continue'] is False
    assert call(api, 'task.resume', {'id': parent}, 'reject-invalid-marker')['ok'] is False


@pytest.mark.parametrize('marker', [None, 'per_item_v9'])
def test_invalid_flow_marker_cannot_complete_or_dispatch_during_recovery(serial, marker):
    from yijianjifa.features.mixing.queue import MixQueue
    from yijianjifa.features.notifications.service import NotificationRow
    from sqlalchemy import func
    api, parent, renderer, _, _, _ = serial
    for index in range(3):
        api._mix.queue.run_once_until_idle(); receive_child(serial, status(serial)['publish_task_ids'][-1])
        if index < 2: api._daily.reconcile(parent)
    with session_scope(api._engine) as s:
        row = s.get(MixTask, parent); snapshot = json.loads(row.snapshot_json)
        snapshot['daily_publish']['execution_flow'] = marker
        row.snapshot_json = json.dumps(snapshot)
    MixQueue(api._engine, api._mix.executor).recover_interrupted()
    api._daily.reconcile(parent)
    assert api._mix.queue.get(parent).state == 'paused'
    assert status(serial)['serial']['waiting_reason'] == 'configuration'
    with session_scope(api._engine) as s:
        assert s.scalar(select(func.count()).select_from(NotificationRow).where(NotificationRow.task_id == parent)) == 0
        assert len(list(s.scalars(select(PublishDeliveryRow)))) == 9
    assert renderer.calls == renderer.tts_calls == 3
