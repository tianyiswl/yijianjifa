"""Real native job/SQLite/receipt orchestration; only network and rendering IO fake."""
import copy
import json
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.publish.deliveries import PublishBatchReceiptRow, DeliveryAttemptRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_daily_mixing import Adapter, Renderer, call


def until(check, timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=check()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('native daily flow did not reach the expected state')


class OnlineProvider:
    def __init__(self, engine):
        self.engine=engine
        self.results={}
        self.calls=[]
        self.entered=threading.Event()
        self.release=threading.Event();self.release.set()

    def __call__(self, account_id, cancel):
        account=AccountService(self.engine).get(account_id)
        self.results[account_id]={'state':'valid','platform_user_id':account.platform_user_id}
        return dict(platform_user_id=account.platform_user_id,nickname=account.nickname,session_blob=b'synthetic-new-login')

    def check_session(self, account_id, cancel):
        self.calls.append(account_id)
        self.entered.set()
        assert self.release.wait(4)
        account=AccountService(self.engine).get(account_id)
        return self.results.get(account_id,{'state':'valid','platform_user_id':account.platform_user_id})


@pytest.fixture
def flow(db_engine, app_paths, tmp_path, monkeypatch):
    source=tmp_path/'原视频.mp4';source.write_bytes(b'fake-render-source')
    MediaStorageService(db_engine,app_paths,kind='video').import_media(source,keep_copy=False,category_ids=[])
    CopyRepository(db_engine).create(id='same-copy',title='原批次标题',body='原正文',tags='原标签',voice='',full='')
    account=AccountService(db_engine).create('douyin','111','原账号')
    with session_scope(db_engine) as s:s.get(AccountRow,account.id).session_ref='original-synthetic-session'
    provider=OnlineProvider(db_engine)
    api=DesktopApi(db_engine,app_paths,login_provider=provider,publish_adapters={'douyin':Adapter()})
    api._mix.executor.renderer=Renderer();api._mix.probe=lambda path:100000
    monkeypatch.setattr(api._daily,'wake_making',lambda:None)
    monkeypatch.setattr(api._multi_publish,'wake',lambda:None)
    params={'mix':{'quantity':3,'total_ms':1000,'segment_ms':1000},
            'publishing':{'targets':[{'account_id':account.id,'platform':'douyin'}],
                'mode':'platform_schedule','day_offset':1,'time':'12:00','interval_hours':2}}
    yield api,provider,params,account
    provider.release.set();api.close(4)


def submit(flow, rid='one-click'):
    response=call(flow[0],'mix.daily.submit',flow[2],rid)
    assert response['ok'],response
    return response['data']['id']


def legacy_submit(flow, rid='one-click'):
    from legacy_daily_fixture import legacy_call
    return legacy_call(flow[0], flow[2], rid)['id']


def phase(api, task_id, expected):
    return until(lambda:(value if (value:=api._daily.status(task_id))['phase']==expected else None))


def login(api, account):
    result=call(api,'account.login.start',{'account_id':account.id},'login-original')
    assert result['ok'],result
    return until(lambda:(r if (r:=api._login_runtime.status(result['data']['id']))['state']!='waiting' else None))


def test_local_valid_flag_never_skips_the_online_gate_and_replay_never_creates_another_batch(flow):
    api,provider,params,account=flow
    provider.release.clear()
    original=submit(flow)
    assert provider.entered.wait(2)
    assert api._daily.status(original)['phase']=='checking_login'
    assert api._mix.queue.get(original).state=='paused'
    api._mix.queue.run_once_until_idle()
    assert api._mix.executor.renderer.index==0
    assert submit(flow)==original
    assert len(api._mix.tasks())==1
    assert call(api,'mix.daily.read')['data']['active_batch']['id']==original
    provider.release.set();phase(api,original,'making')
    assert api._mix.queue.get(original).state=='waiting'
    assert submit(flow)==original and provider.calls==[account.id]
    assert api._mix.executor.renderer.index==0


@pytest.mark.parametrize('local_state',['valid','expired'])
def test_expired_online_login_prompts_and_relogin_keeps_original_batch_copies_and_times(flow,local_state):
    api,provider,_,account=flow
    AccountService(api._engine).set_login_state(account.id,local_state)
    provider.results[account.id]={'state':'login_required','reason':'login_required'}
    original=submit(flow)
    waiting=phase(api,original,'needs_login')
    assert waiting['can_continue'] and waiting['login_accounts']==[{
        'id':account.id,'platform':'douyin','nickname':'原账号','reason':'daily_login_required'}]
    assert 'session' not in json.dumps(waiting) and 'platform_user_id' not in json.dumps(waiting)
    frozen=copy.deepcopy(api._mix.queue.get(original).snapshot)
    assert api._mix.executor.renderer.index==0
    assert AccountService(api._engine).get(account.id).login_state=='expired'
    assert login(api,account)['state']=='completed'
    assert AccountService(api._engine).get(account.id).session_ref!='original-synthetic-session'
    assert call(api,'mix.daily.continue',{'id':original})['ok']
    phase(api,original,'making')
    current=api._mix.queue.get(original).snapshot
    assert current['copies']==frozen['copies'] and current['daily_publish']['slots']==frozen['daily_publish']['slots']
    assert current==frozen  # Session renewal must not invalidate rendered-file checkpoints.
    with session_scope(api._engine) as s:
        from yijianjifa.storage.models import Setting
        proof=json.loads(s.get(Setting,'mix.daily.status:'+original).value_json)
    assert proof['auth_targets'][0]['session_fingerprint']!=frozen['daily_publish']['targets'][0]['session_fingerprint']
    assert len(api._mix.tasks())==1


def test_expiry_during_making_retains_all_outputs_and_hands_off_only_after_relogin(flow):
    api,provider,_,account=flow
    original=legacy_submit(flow);phase(api,original,'making')
    api._mix.queue._run_single_item(original)
    assert api._mix.queue.get(original).success_count==1
    provider.results[account.id]={'state':'login_required'}
    api._mix.queue.run_once_until_idle()
    blocked=phase(api,original,'needs_login')
    assert blocked['publish_task_ids']==[] and api._mix.executor.renderer.index==3
    snapshot=api._mix.queue.get(original).snapshot
    assert login(api,account)['state']=='completed'
    assert call(api,'mix.daily.continue',{'id':original})['ok']
    ready=phase(api,original,'publishing')
    assert api._mix.executor.renderer.index==3 and len(api._mix.tasks())==1
    assert api._mix.queue.get(original).snapshot['daily_publish']['slots']==snapshot['daily_publish']['slots']
    child=ready['publish_task_ids'][0]
    deliveries=api._multi_publish.detail(child)['deliveries']
    for _ in deliveries:api._multi_publish.coordinator.run_once(child,[d['id'] for d in deliveries])
    phase(api,original,'accepted')
    checks=len(provider.calls)
    assert call(api,'mix.daily.continue',{'id':original})['ok']
    assert api._daily.status(original)['accepted']==3 and len(provider.calls)==checks
    with session_scope(api._engine) as s:
        assert len(list(s.scalars(select(PublishBatchReceiptRow))))==1
        assert len(list(s.scalars(select(DeliveryAttemptRow))))==3


def test_partial_making_pause_and_login_resume_never_remakes_completed_item(flow):
    api,provider,_,account=flow
    original=legacy_submit(flow);phase(api,original,'making')
    renderer=api._mix.executor.renderer
    render=renderer.render
    def pause_in_first_render(request):
        assert call(api,'task.pause',{'id':original},'pause')['ok']
        return render(request)
    renderer.render=pause_in_first_render
    api._mix.queue._run_single_item(original)
    renderer.render=render
    assert api._mix.queue.get(original).state=='paused'
    provider.results[account.id]={'state':'login_required'}
    assert call(api,'task.resume',{'id':original},'resume')['ok']
    phase(api,original,'needs_login')
    assert api._mix.executor.renderer.index==1
    assert login(api,account)['state']=='completed'
    assert call(api,'mix.daily.continue',{'id':original})['ok']
    phase(api,original,'making');api._mix.queue.run_once_until_idle();phase(api,original,'publishing')
    assert api._mix.executor.renderer.index==3


@pytest.mark.parametrize('operation',['task.cancel','task.pause'])
def test_cancellation_or_pause_during_slow_check_cannot_be_revived_by_late_result(flow,operation):
    api,provider,_,_=flow
    provider.release.clear();original=submit(flow);assert provider.entered.wait(2)
    assert call(api,operation,{'id':original},'control')['ok']
    provider.release.set()
    until(lambda:original not in api._daily.auth._jobs)
    api._mix.queue.run_once_until_idle()
    assert api._mix.executor.renderer.index==0
    assert api._mix.queue.get(original).state==('cancelled' if operation=='task.cancel' else 'paused')
    assert api._daily.status(original)['phase']!= 'making'


@pytest.mark.parametrize('observation',[{'state':'unavailable','reason':'network_unavailable'},
                                       {'state':'identity_mismatch','platform_user_id':'other'}])
def test_network_or_other_identity_never_starts_work_or_falsely_expires_account(flow,observation):
    api,provider,_,account=flow
    provider.results[account.id]=observation
    original=submit(flow);status=phase(api,original,'needs_attention')
    assert status['can_continue'] and not status['login_accounts']
    assert api._mix.executor.renderer.index==0
    assert AccountService(api._engine).get(account.id).login_state=='valid'


def test_client_close_during_check_preserves_batch_without_starting_after_late_result(flow,app_paths):
    api,provider,_,_=flow
    provider.release.clear();original=submit(flow);assert provider.entered.wait(2)
    assert api.close(.01) is False
    provider.release.set();assert api.close(4)
    assert api._mix.executor.renderer.index==0
    reopened=DesktopApi(api._engine,app_paths,login_provider=provider,publish_adapters={'douyin':Adapter()})
    try:
        overview=call(reopened,'mix.daily.read')['data']
        assert overview['active_batch'] is None and overview['recent_batch']['id'] == original
        assert overview['recent_batch']['serial']['waiting_reason'] == 'authorization'
        data=call(reopened,'mix.daily.status',{'id':original})['data']
        assert data['id']==original and data['phase']=='needs_attention' and data['can_continue']
        assert reopened._mix.queue.get(original).state=='paused'
        assert len(provider.calls)==1
    finally:reopened.close(4)


def test_continue_rejects_untrusted_or_extra_parameters_and_never_creates_a_task(flow):
    api,_,_,_=flow
    bad=dict(v=1,request_id='forged',operation='mix.daily.continue',params={'id':'missing'})
    assert not api.dispatch(json.dumps(bad),trusted_origin=False)['ok']
    assert not call(api,'mix.daily.continue',{'id':'missing','targets':[]})['ok']
    assert api._mix.tasks()==[]


def test_verified_accounts_can_continue_while_failed_login_account_is_held(flow):
    api,provider,params,account=flow
    second=AccountService(api._engine).create('douyin','222','第二账号')
    with session_scope(api._engine) as s:s.get(AccountRow,second.id).session_ref='second-synthetic-session'
    params['publishing']['targets'].append({'account_id':second.id,'platform':'douyin'})
    provider.results[second.id]={'state':'login_required'}
    original=submit(flow);status=phase(api,original,'making')
    assert provider.calls==[account.id,second.id]
    with session_scope(api._engine) as s:
        saved=json.loads(s.get(Setting,'mix.daily.status:'+original).value_json)
    assert [a['account_id'] for a in saved['login_holds']]==[second.id]
    assert any('第二账号' in row['account_label'] for row in status['execution']['events'])
    assert api._mix.executor.renderer.index==0


@pytest.mark.parametrize('local_state',['expired','pending_verification'])
def test_real_online_success_recovers_a_stale_local_badge_without_requesting_another_login(flow,local_state):
    api,provider,_,account=flow
    AccountService(api._engine).set_login_state(account.id,local_state)
    original=submit(flow);phase(api,original,'making')
    assert AccountService(api._engine).get(account.id).login_state=='valid'
    assert provider.calls==[account.id] and api._login_runtime.flow is None


def test_readonly_check_challenge_opens_original_login_flow_without_falsely_expiring_account(flow):
    api,provider,_,account=flow
    provider.results[account.id]={'state':'unavailable','reason':'verification_required'}
    original=submit(flow);status=phase(api,original,'needs_login')
    assert status['reason_code']=='daily_login_verification_required'
    assert status['login_message']=='平台需要验证，请在登录窗口完成验证后继续本批。'
    assert status['can_continue'] and status['login_accounts']==[{
        'id':account.id,'platform':'douyin','nickname':'原账号',
        'reason':'daily_login_verification_required'}]
    assert status['login_repair_accounts']==[]
    assert AccountService(api._engine).get(account.id).login_state=='valid'
    assert api._login_runtime.flow is None
    assert api._mix.executor.renderer.index==0 and status['publish_task_ids']==[]
    frozen=copy.deepcopy(api._mix.queue.get(original).snapshot)
    assert login(api,account)['state']=='completed'
    assert call(api,'mix.daily.continue',{'id':original},'continue-verified-login')['ok']
    resumed=phase(api,original,'making')
    assert resumed['login_accounts']==[] and resumed['login_message']==''
    assert api._mix.queue.get(original).snapshot==frozen and len(api._mix.tasks())==1
    assert api._mix.executor.renderer.index==0


def test_replaying_a_completed_continue_receipt_never_reauthorizes_after_later_pause(flow):
    api,provider,_,account=flow
    provider.results[account.id]={'state':'login_required'}
    original=submit(flow);phase(api,original,'needs_login')
    assert login(api,account)['state']=='completed'
    first=call(api,'mix.daily.continue',{'id':original},'explicit-continue')
    assert first['ok'];phase(api,original,'making')
    assert call(api,'task.pause',{'id':original},'later-pause')['ok']
    checked=len(provider.calls)
    assert call(api,'mix.daily.continue',{'id':original},'explicit-continue')==first
    assert api._mix.queue.get(original).state=='paused'
    assert len(provider.calls)==checked and api._daily.status(original)['phase']=='needs_attention'
    assert not call(api,'mix.daily.continue',{'id':'a-different-task'},'explicit-continue')['ok']


def test_relogin_preserves_already_rendered_pending_file_and_resumes_registration_without_rendering_it_again(flow,monkeypatch):
    api,provider,_,account=flow
    original=legacy_submit(flow);phase(api,original,'making')
    snapshot=copy.deepcopy(api._mix.queue.get(original).snapshot)
    commit=api._mix.executor._commit_success
    def unavailable_once(*args,**kwargs):
        raise RuntimeError('synthetic registration unavailable')
    monkeypatch.setattr(api._mix.executor,'_commit_success',unavailable_once)
    api._mix.queue._run_single_item(original)
    assert api._mix.queue.get(original).state=='interrupted'
    assert api._mix.executor.renderer.index==1
    with session_scope(api._engine) as s:
        from yijianjifa.storage.models import Setting
        pending=json.loads(s.get(Setting,'mix-pending:'+original+':0').value_json)
    output=Path(pending['output']);original_bytes=output.read_bytes()
    monkeypatch.setattr(api._mix.executor,'_commit_success',commit)
    provider.results[account.id]={'state':'login_required'}
    assert call(api,'task.resume',{'id':original},'resume-pending')['ok']
    phase(api,original,'needs_login')
    assert login(api,account)['state']=='completed'
    assert call(api,'mix.daily.continue',{'id':original},'continue-pending')['ok']
    phase(api,original,'making')
    assert api._mix.queue.get(original).snapshot==snapshot
    assert output.read_bytes()==original_bytes
    api._mix.queue.run_once_until_idle();phase(api,original,'publishing')
    assert api._mix.executor.renderer.index==3
    assert api._mix.queue.get(original).success_count==3
    assert output.read_bytes()==original_bytes


def test_new_login_check_revokes_old_proof_before_any_publish_authorization(flow,monkeypatch):
    api,provider,_,_=flow
    original=legacy_submit(flow);phase(api,original,'making')
    prepared=api._multi_publish.service.prepare
    invalidated=threading.Event()
    def prepare_then_revoke(*args,**kwargs):
        result=prepared(*args,**kwargs)
        provider.release.clear()
        assert call(api,'mix.daily.continue',{'id':original},'newer-check')['ok']
        invalidated.set()
        return result
    monkeypatch.setattr(api._multi_publish.service,'prepare',prepare_then_revoke)
    api._mix.queue.run_once_until_idle();assert invalidated.wait(2)
    from yijianjifa.app_services.multi_publish_runtime import authorized_targets
    def saved():
        status=api._daily.status(original)
        return status if status['publish_task_ids'] else None
    status=until(saved)
    with session_scope(api._engine) as s:
        assert authorized_targets(s,status['publish_task_ids'][0])=={}
    assert status['phase']=='checking_login'
    provider.release.set();phase(api,original,'publishing')


def test_online_success_rechecks_frozen_platform_fields_before_starting_making(flow):
    api,provider,_,_=flow
    provider.release.clear();original=submit(flow);assert provider.entered.wait(2)
    api._multi_publish.catalog.transport_for('douyin').adapter.plan_readiness=lambda payload:'platform_fields_invalid'
    provider.release.set();status=phase(api,original,'needs_attention')
    assert status['reason_code']=='platform_fields_invalid'
    api._mix.queue.run_once_until_idle()
    assert api._mix.executor.renderer.index==0


def test_original_after_final_unknown_target_is_never_retried_by_continue(flow):
    api,provider,_,_=flow
    calls=[]
    def submit_unknown(payload,*,context,before_final):
        calls.append(payload['video_id']);before_final()
        from yijianjifa.features.publish.coordinator import AdapterOutcome
        return AdapterOutcome('result_unknown')
    api._multi_publish.catalog.transport_for('douyin').adapter.submit=submit_unknown
    original=legacy_submit(flow);phase(api,original,'making')
    api._mix.queue.run_once_until_idle();ready=phase(api,original,'publishing')
    child=ready['publish_task_ids'][0];deliveries=api._multi_publish.detail(child)['deliveries']
    api._multi_publish.coordinator.run_once(child,[d['id'] for d in deliveries])
    phase(api,original,'needs_attention');count=len(provider.calls)
    assert call(api,'mix.daily.continue',{'id':original})['ok']
    assert len(calls)==1 and len(provider.calls)==count
    with session_scope(api._engine) as s:
        attempts=list(s.scalars(select(DeliveryAttemptRow)))
        assert len(attempts)==3 and sum(bool(a.final_json) for a in attempts)==1


def test_restart_after_whole_batch_check_before_handoff_can_continue_without_remaking(flow,app_paths,monkeypatch):
    api,provider,_,_=flow
    original=legacy_submit(flow);phase(api,original,'making')
    reconcile=api._daily.reconcile
    def hold_handoff(task_id):
        with session_scope(api._engine) as s:
            from yijianjifa.storage.models import Setting
            p=json.loads(s.get(Setting,'mix.daily.status:'+task_id).value_json)
        return api._daily.status(task_id) if p.get('auth_verified')=='before_publish' else reconcile(task_id)
    monkeypatch.setattr(api._daily,'reconcile',hold_handoff)
    api._mix.queue.run_once_until_idle()
    from yijianjifa.storage.models import Setting
    def checked():
        with session_scope(api._engine) as s:return json.loads(s.get(Setting,'mix.daily.status:'+original).value_json).get('auth_verified')=='before_publish'
    until(checked);assert api.close(4)
    reopened=DesktopApi(api._engine,app_paths,login_provider=provider,publish_adapters={'douyin':Adapter()})
    monkeypatch.setattr(reopened._multi_publish,'wake',lambda:None)
    try:
        overview=call(reopened,'mix.daily.read')['data']
        assert overview['active_batch'] is None and overview['recent_batch'] is None
        data=call(reopened,'mix.daily.status',{'id':original})['data']
        assert data['id']==original and data['can_continue']
        assert call(reopened,'mix.daily.continue',{'id':original})['ok']
        phase(reopened,original,'publishing')
        assert api._mix.executor.renderer.index==3 and len(reopened._mix.tasks())==1
    finally:reopened.close(4)
