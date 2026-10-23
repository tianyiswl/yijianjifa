"""Native V2 publishing contract, with isolated files and host-only adapters."""
import json
import threading
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterValidation, AdapterOutcome
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class HostAdapter:
    platform_id='douyin'
    verified_capabilities={'ordinary','publish_now','platform_schedule','result_query'}
    def __init__(self):self.calls=[];self.thread_ids=[]
    def validate_target(self,target,payload,execution):return AdapterValidation(payload['platform_user_id'],True)
    def submit(self,payload,*,context,before_final):
        self.thread_ids.append(threading.get_ident());self.calls.append(payload['video_id'])
        before_final();return AdapterOutcome('published')
    def reconcile(self,payload,*,context):return AdapterOutcome('published')


def call(api,op,p=None,rid='read',trusted=True):
    return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':'multi_publish.'+op,'params':p or {}}),trusted_origin=trusted)


@pytest.fixture
def fixture(db_engine,app_paths,tmp_path):
    adapter=HostAdapter()
    api=DesktopApi(db_engine,app_paths,login_provider=SimpleNamespace(publish_adapters={'douyin':adapter}))
    account=AccountService(db_engine).create('douyin','111','合成账号')
    videos=[]
    for i in range(2):
        p=tmp_path/f'owned-{i}.mp4';p.write_bytes(f'synthetic {i}'.encode())
        videos.append(FinishedVideoService(db_engine).import_external(p,duration_ms=1000,title=f'测试{i}'))
    yield api,account,videos,adapter
    api.close(5)


def prepared(fixture,rid='plan'):
    api,account,videos,_=fixture
    response=call(api,'prepare',{'video_ids':[v.id for v in videos],'targets':[{'account_id':account.id,'platform':'douyin'}],
        'common_fields':{'body':'本次资料'},'execution_mode':'publish_now'},rid)
    assert response['ok'],response
    return response['data']


def saved(fixture,rid='plan'):
    p=prepared(fixture,rid)
    response=call(fixture[0],'confirm',{'prepared_id':p['prepared_id'],'intent_id':rid,'consent_partial':False},'save-'+rid)
    assert response['ok'],response
    return response['data']


def test_native_origin_strict_schema_and_forged_capability_never_reach_adapter(fixture):
    api,_,_,adapter=fixture
    assert call(api,'catalog',trusted=False)['error']['code']=='origin_denied'
    assert not call(api,'catalog',{'provider':'douyin','verified':True})['ok']
    assert not call(api,'prepare',{'url':'https://arbitrary.invalid','provider':'x'})['ok']
    assert call(api,'catalog')['data']['items'][1]['ordinary_ready'] is False
    assert adapter.calls==[]


def test_prepare_freezes_safe_effective_fields_and_exact_summary(fixture,db_engine):
    p=prepared(fixture)
    assert p['summary']=={'videos':2,'accounts':1,'targets':2,'ready':2,'blocked':0,'excluded':0}
    assert p['targets'][0]['effective_fields']['body']=='本次资料'
    batch=saved(fixture,'other')
    detail=call(fixture[0],'detail',{'id':batch['id']})['data']
    assert detail['version']==2 and detail['summary']['targets']==2
    child=detail['deliveries'][0]
    assert child['delivery_id']==child['id'] and 'start' in child['allowed_actions']
    encoded=json.dumps(detail)
    for secret in ['video_path','platform_user_id','session_fingerprint','owner_token','context_json','request_digest']:assert secret not in encoded
    ordinary=fixture[0].dispatch(json.dumps({'v':1,'request_id':'center','operation':'task.detail','params':{'id':batch['id']}}),trusted_origin=True)
    assert ordinary['ok'] and ordinary['data']['version']==2
    # Unified task-center details use the same exact targets, never legacy items[0].
    center=fixture[0].dispatch(json.dumps({'v':1,'request_id':'center2','operation':'task.center.detail','params':{'id':batch['id']}}),trusted_origin=True)
    assert center['ok'] and center['data']['version']==2


def test_accepted_start_subset_is_durable_and_reopen_does_not_resubmit(fixture,db_engine,app_paths):
    api,_,_,adapter=fixture;batch=saved(fixture)
    detail=call(api,'detail',{'id':batch['id']})['data'];ids=[d['id'] for d in detail['deliveries']]
    checked=call(api,'execution.check',{'id':batch['id'],'delivery_ids':[ids[0]]})['data']
    payload={'id':batch['id'],'delivery_ids':[ids[0]],'digest':checked['digest'],'consent':True,'consent_partial':True}
    receipt=call(api,'execution.start',payload,'start-1')
    assert receipt['ok'] and receipt['data']['accepted'] is True
    assert call(api,'execution.start',payload,'start-1')==receipt
    deadline=time.monotonic()+3
    # The adapter records entry before requesting final authority. Reopening
    # after a completed submission requires its durable result, not that entry.
    states=[]
    while time.monotonic()<deadline:
        states=[d['state'] for d in call(api,'detail',{'id':batch['id']})['data']['deliveries']]
        if states==['published','waiting']:break
        time.sleep(.01)
    assert states==['published','waiting'],states
    assert api.close(5)
    reopened=DesktopApi(db_engine,app_paths,login_provider=SimpleNamespace(publish_adapters={'douyin':adapter}))
    try:
        reopened.start_local_checks();time.sleep(.05)
        assert len(adapter.calls)==1
        assert call(reopened,'receipt',{'request_id':'start-1'})['data']==receipt['data']
        states=[d['state'] for d in call(reopened,'detail',{'id':batch['id']})['data']['deliveries']]
        assert states==['published','waiting']
    finally:reopened.close(5)


def test_generic_delete_cancels_and_hides_children_without_rewriting_v2(fixture,db_engine):
    batch=saved(fixture)
    raw=json.dumps({'v':1,'request_id':'remove','operation':'task.delete','params':{'id':batch['id']}})
    result=fixture[0].dispatch(raw,trusted_origin=True)
    assert result['ok'],result
    detail=call(fixture[0],'detail',{'id':batch['id']})['data']
    assert all(d['hidden'] and d['state']=='cancelled' for d in detail['deliveries'])
    with session_scope(db_engine) as s:assert s.get(Setting,'task-hidden:'+batch['id']) is None


def test_versioned_settings_presets_retain_intent_and_reject_ephemeral_values(fixture,db_engine,app_paths):
    api,account,_,_=fixture
    initial=call(api,'settings.read')['data']
    assert initial['settings']['start_policy']['kind']=='relative_tomorrow_noon'
    assert initial['settings']['schedule']['interval']=='3'
    assert initial['settings']['schedule']['per_day']=='3'
    assert initial['settings']['schedule']['days']=='5'
    settings={**initial['settings'],'targets':[{'account_id':account.id,'platform':'douyin'}],'common_fields':{'title':'方案标题'}}
    assert call(api,'settings.save',{'settings':settings},'remember')['ok']
    assert not call(api,'settings.save',{'settings':{**settings,'video_ids':['transient']}},'unsafe')['ok']
    p=call(api,'presets.save',{'id':None,'name':'常用方案','settings':settings},'preset')['data']
    assert call(api,'presets.get',{'id':p['id']})['data']['settings']==settings
    assert call(api,'settings.read')['data']['settings']==settings


def test_new_distribution_module_does_not_import_ordinary_publish_settings(fixture,db_engine):
    old={'mode':'daily','start':'2020-01-01T12:00','interval':'6','per_day':'2','days':'9','clock_mode':'interval','date':'2020-01-01','times':['12:00']}
    with session_scope(db_engine) as s:s.add(Setting(key='schedule.publish',value_json=json.dumps(old)))
    result=call(fixture[0],'settings.read')['data']
    assert result['settings']['start_policy']=={'kind':'relative_tomorrow_noon'}
    assert result['settings']['schedule']=={'mode':'daily','interval':'3','per_day':'3','days':'5','clock_mode':'interval','times':['12:00']}
    assert result['migrated_v1'] is False and result['issues']==[]
    with session_scope(db_engine) as s:
        assert json.loads(s.get(Setting,'schedule.publish').value_json)==old
        assert s.get(Setting,'multi_publish.settings.v2') is None


def test_ordinary_and_distribution_settings_and_presets_remain_independent(fixture,db_engine):
    api=fixture[0]
    def legacy(op,p,rid):
        response=api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':op,'params':p}),trusted_origin=True)
        assert response['ok'],response
        return response['data']
    old={'mode':'daily','start':'2030-10-08T12:00','interval':'6','per_day':'2','days':'9','clock_mode':'interval','date':'2030-10-08','times':['12:00']}
    legacy('settings.publish.save',{'view':'publish','settings':old},'ordinary-memory')
    own=call(api,'settings.read')['data']['settings'];own['schedule']['interval']='1.5'
    assert call(api,'settings.save',{'settings':own},'distribution-memory')['ok']
    assert legacy('settings.publish.read',{'view':'publish'},'ordinary-read')['settings']==old
    old['days']='4';legacy('settings.publish.save',{'view':'publish','settings':old},'ordinary-change')
    assert call(api,'settings.read')['data']['settings']==own
    original=legacy('preset.save',{'id':None,'payload':{'kind':'normal_publish','name':'原普通方案','schedule_rule':{'interval_hours':6,'days':4}}},'ordinary-preset')
    separate=call(api,'presets.save',{'id':None,'name':'分发方案','settings':own},'distribution-preset')['data']
    assert legacy('preset.list',{'query':'','kind':'normal_publish','offset':0,'limit':100},'ordinary-list')['items'][0]['id']==original['id']
    assert [p['id'] for p in call(api,'presets.list')['data']['items']]==[separate['id']]
    assert legacy('preset.get',{'id':original['id']},'ordinary-get')['payload']['schedule_rule']['interval_hours']==6
    assert not call(api,'presets.get',{'id':original['id']})['ok']
    assert not api.dispatch(json.dumps({'v':1,'request_id':'other-get','operation':'preset.get','params':{'id':separate['id']}}),trusted_origin=True)['ok']


def test_unknown_without_final_never_has_retry_or_unsubmitted_claim(fixture,db_engine):
    batch=saved(fixture)
    with session_scope(db_engine) as s:
        delivery=s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==batch['id']))
        from yijianjifa.features.tasks.service import TaskItemRow
        from yijianjifa.features.publish.history import PublishHistoryRow
        s.get(TaskItemRow,delivery.item_id).state=s.get(PublishHistoryRow,delivery.history_id).state='result_unknown'
        attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==delivery.id))
        attempt.result_json=json.dumps({'state':'result_unknown','request_digest':delivery.request_digest,'error_code':'result_unknown'})
    child=call(fixture[0],'detail',{'id':batch['id']})['data']['deliveries'][0]
    assert child['submission_status']=='uncertain'
    assert not {'resume','start','reprepare','reschedule'}&set(child['allowed_actions'])


def test_native_capture_rejects_session_rotation_between_prepare_and_confirm(fixture,db_engine):
    from yijianjifa.features.accounts.service import AccountRow
    api,account,_,_=fixture
    with session_scope(db_engine) as s:s.get(AccountRow,account.id).session_ref='version-1'
    p=prepared(fixture)
    with session_scope(db_engine) as s:s.get(AccountRow,account.id).session_ref='version-2'
    result=call(api,'confirm',{'prepared_id':p['prepared_id'],'intent_id':'plan','consent_partial':False},'confirm')
    assert not result['ok']
    with session_scope(db_engine) as s:assert not list(s.scalars(select(PublishDeliveryRow)))


def test_multiple_accounts_yield_worker_and_sensitive_response_is_never_receipted(fixture,db_engine):
    api,account,videos,adapter=fixture
    second=AccountService(db_engine).create('douyin','222','第二合成账号')
    class ParkAdapter(HostAdapter):
        def submit(self,payload,*,context,before_final):
            self.calls.append(payload['account_id'])
            if payload['account_id']==account.id:return AdapterOutcome('paused_auth',context={'handle':'park'})
            before_final();return AdapterOutcome('published')
        def resume(self,payload,*,context,before_final):before_final();return AdapterOutcome('published')
    parked=ParkAdapter()
    api.close(5)
    fresh=DesktopApi(db_engine,api._paths,publish_adapters={'douyin':parked})
    try:
        p=call(fresh,'prepare',{'video_ids':[videos[0].id],'targets':[{'account_id':a.id,'platform':'douyin'} for a in [account,second]],'common_fields':{},'execution_mode':'publish_now','distribution_by_platform':{'douyin':'whole_batch'}},'two')['data']
        batch=call(fresh,'confirm',{'prepared_id':p['prepared_id'],'intent_id':'two','consent_partial':False},'freeze')['data']
        ids=[d['id'] for d in call(fresh,'detail',{'id':batch['id']})['data']['deliveries']]
        check=call(fresh,'execution.check',{'id':batch['id'],'delivery_ids':ids})['data']
        assert call(fresh,'execution.start',{'id':batch['id'],'delivery_ids':ids,'digest':check['digest'],'consent':True,'consent_partial':False},'begin')['ok']
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            states=[d['state'] for d in call(fresh,'detail',{'id':batch['id']})['data']['deliveries']]
            if states==['paused_auth','published']:break
            time.sleep(.01)
        assert parked.calls==[account.id,second.id]
        assert [d['state'] for d in call(fresh,'detail',{'id':batch['id']})['data']['deliveries']]==['paused_auth','published']
    finally:fresh.close(5)


def test_rich_fields_use_account_bound_option_refs_not_frontend_assertions(fixture,db_engine):
    from yijianjifa.features.accounts.service import AccountRow
    api,account,videos,adapter=fixture
    with session_scope(db_engine) as s:
        row=s.get(AccountRow,account.id);row.session_ref='version';row.capability_json=json.dumps({'ordinary':'available','commerce':'pending','location_scope':'pending'})
    selection={'id':'loc','name':'合成地点','address':'合成地址'}
    from yijianjifa.app_services.douyin_declaration_catalog import catalog
    declaration=catalog()['items'][0]
    api._publish_locations._fetch=lambda *a:[selection]
    api._publish_declarations._fetch=lambda *a:pytest.fail('built-in declaration must not fetch platform options')
    location=api._publish_locations.search({'account_id':account.id,'query':'合成'})
    policy=api._publish_declarations.query({'account_id':account.id})
    adapter.validate_fields=lambda p:p['fields'].get('location')==selection and p['fields'].get('declaration')==declaration
    params={'video_ids':[videos[0].id],'targets':[{'account_id':account.id,'platform':'douyin','fields':{'location':{'token':location['token'],'id':'loc'},'declaration':{'token':policy['token'],'id':declaration['id']}}}],
        'common_fields':{},'execution_mode':'publish_now'}
    result=call(api,'prepare',params,'real-option')
    assert result['ok'] and result['data']['targets'][0]['state']=='waiting'
    assert result['data']['targets'][0]['effective_fields']['location']==selection
    assert result['data']['targets'][0]['effective_fields']['declaration']==declaration
    params['targets'][0]['fields']['location']={**selection,'verified':True}
    assert not call(api,'prepare',params,'forged')['ok']


@pytest.mark.parametrize('legacy', ['broken', json.dumps({
    'started_at':'2020-01-01T00:00:00+00:00', 'expires_at':'2021-01-01T00:00:00+00:00',
    'last_seen_at':'2026-01-01T00:00:00+00:00'})])
def test_legacy_term_keeps_receipts_and_allows_new_synthetic_submission(fixture,db_engine,tmp_path,legacy):
    api,account,videos,adapter=fixture;batch=saved(fixture)
    from yijianjifa.app_services.use_term import UseTerm
    with session_scope(db_engine) as s:
        row=s.get(Setting,UseTerm.KEY)
        if row is None:s.add(Setting(key=UseTerm.KEY,value_json=legacy))
        else:row.value_json=legacy
    assert call(api,'receipt',{'request_id':'plan'})['data']['id']==batch['id']
    child=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]
    assert child['can_submit'] and 'start' in child['allowed_actions']
    new_path=tmp_path/'new-free-video.mp4';new_path.write_bytes(b'fresh synthetic content')
    new_video=FinishedVideoService(db_engine).import_external(new_path,duration_ms=1000,title='免费制作成品')
    prepared_result=call(api,'prepare',{'video_ids':[new_video.id],'targets':[{'account_id':account.id,'platform':'douyin'}],
        'common_fields':{},'execution_mode':'publish_now'},'new')
    assert prepared_result['ok'],prepared_result
    fresh=call(api,'confirm',{'prepared_id':prepared_result['data']['prepared_id'],'intent_id':'new','consent_partial':False},'new-save')
    assert fresh['ok'],fresh
    # Keep worker scheduling deterministic; use the real authorization, claim
    # and final boundaries with the fully synthetic platform adapter.
    api._multi_publish.wake=lambda: None
    identity=fresh['data']['id']
    children=call(api,'detail',{'id':identity})['data']['deliveries']
    ids=[child['id'] for child in children]
    checked=call(api,'execution.check',{'id':identity,'delivery_ids':ids})['data']
    started=call(api,'execution.start',{'id':identity,'delivery_ids':ids,'digest':checked['digest'],
        'consent':True,'consent_partial':False},'free-start')
    assert started['ok'],started
    assert api._multi_publish.coordinator.run_once(identity)=='published'
    assert adapter.calls==[new_video.id]
    with session_scope(db_engine) as s:
        assert s.get(Setting,UseTerm.KEY).value_json==legacy


def test_unsupported_platform_login_cannot_invoke_douyin_provider(fixture):
    api,_,_,_=fixture
    raw=json.dumps({'v':1,'request_id':'login','operation':'account.login.start','params':{'account_id':None,'platform':'kuaishou'}})
    result=api.dispatch(raw,trusted_origin=True)
    assert not result['ok'] and result['error']['code']=='platform_not_integrated'


def test_public_overrides_restore_common_without_changing_finished_metadata(fixture):
    api,account,videos,_=fixture
    response=call(api,'prepare',{'video_ids':[videos[0].id],'targets':[{'account_id':account.id,'platform':'douyin','fields':{'title':'平台标题'}}],
        'common_fields':{'title':'公共标题'},'execution_mode':'publish_now'},'edit')
    assert response['ok']
    batch=call(api,'confirm',{'prepared_id':response['data']['prepared_id'],'intent_id':'edit','consent_partial':False},'freeze-edit')['data']
    did=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['id']
    result=call(api,'target.reprepare',{'delivery_id':did,'fields':{},'reset_fields':['title']},'restore')
    assert result['ok'],result
    child=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]
    assert child['effective_fields']['title']=='公共标题'
    assert FinishedVideoService(api._engine).get(videos[0].id).title=='测试0'


def test_utf8_payload_limit_counts_bytes_and_duplicate_keys_are_rejected(fixture):
    api=fixture[0]
    obj={'v':1,'request_id':'big','operation':'multi_publish.settings.save','params':{'settings':{'title':'中'*6000}}}
    result=api.dispatch(json.dumps(obj,ensure_ascii=False),trusted_origin=True)
    assert result['error']['code']=='invalid_request'
    assert not api.dispatch('{"v":1,"v":1,"request_id":"dup","operation":"multi_publish.catalog","params":{}}',trusted_origin=True)['ok']


def test_target_pause_resume_has_precise_allowed_actions_and_preserves_siblings(fixture):
    api,_,_,_=fixture;batch=saved(fixture)
    children=call(api,'detail',{'id':batch['id']})['data']['deliveries'];did=children[0]['id']
    result=call(api,'target.pause',{'delivery_id':did},'pause-one');assert result['ok']
    children=call(api,'detail',{'id':batch['id']})['data']['deliveries']
    assert children[0]['paused'] is True and 'resume' in children[0]['allowed_actions']
    assert children[1]['paused'] is False
    assert call(api,'target.resume',{'delivery_id':did,'consent_same_identity':True},'resume-one')['ok']


def test_task_center_list_labels_v2_counts_without_claiming_public_results(fixture):
    api,_,_,_=fixture;batch=saved(fixture)
    raw=json.dumps({'v':1,'request_id':'list','operation':'task.center.list','params':{'query':'','tab':'publish','status':'','offset':0,'limit':20}})
    row=next(r for r in api.dispatch(raw,trusted_origin=True)['data']['items'] if r['id']==batch['id'])
    assert row['version']==2 and row['summary']['targets']==2 and row['published_count']==0


@pytest.mark.parametrize('change',[{'common_fields':None},{'fields_by_video':[]},{'common_fields':{'cover':{'kind':'custom','path':'/private/arbitrary'}}}])
def test_public_prepare_rejects_malformed_nested_fields_and_native_path(fixture,change):
    api,account,videos,_=fixture
    p={'video_ids':[videos[0].id],'targets':[{'account_id':account.id,'platform':'douyin'}],'common_fields':{},'execution_mode':'publish_now',**change}
    assert call(api,'prepare',p,'malformed')['error']['code']=='invalid_request'


def test_verification_response_requires_explicit_same_identity_consent_before_resume(fixture,monkeypatch):
    api,_,_,adapter=fixture;responses=[]
    adapter.interventions=lambda:[{'id':'challenge','delivery_id':'delivery','platform':'douyin','final_started':False}]
    adapter.respond=lambda *args:responses.append(args) or {'accepted':True}
    monkeypatch.setattr(api._multi_publish,'enqueue',lambda *a:None)
    assert not call(api,'intervention.respond',{'id':'challenge','delivery_id':'delivery','action':'done'},'denied')['ok']
    assert responses==[]
    assert call(api,'intervention.respond',{'id':'challenge','delivery_id':'delivery','action':'done','consent_same_identity':True},'explicit')['ok']


def test_interrupted_without_final_cancel_retains_uncertain_evidence(fixture,db_engine):
    api,_,_,_=fixture;batch=saved(fixture)
    with session_scope(db_engine) as s:
        row=s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==batch['id']))
        from yijianjifa.features.tasks.service import TaskItemRow
        from yijianjifa.features.publish.history import PublishHistoryRow
        s.get(TaskItemRow,row.item_id).state=s.get(PublishHistoryRow,row.history_id).state='interrupted'
        did=row.id
    assert call(api,'target.cancel',{'delivery_id':did},'cancel-uncertain')['ok']
    child=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]
    assert child['state']=='interrupted' and child['submission_status']=='uncertain'


def test_cancel_receipt_rollback_cannot_delete_owned_bytes(fixture,db_engine,monkeypatch):
    from pathlib import Path
    api,_,_,_=fixture;batch=saved(fixture)
    with session_scope(db_engine) as s:
        children=list(s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==batch['id'])))
        did=children[0].id;path=Path(json.loads(children[0].snapshot_json)['video_path'])
    apply=api._multi_commands.apply
    def fail_after_apply(c,name,p):
        apply(c,name,p);raise ValueError('synthetic receipt failure')
    monkeypatch.setattr(api._multi_commands,'apply',fail_after_apply)
    assert not call(api,'target.cancel',{'delivery_id':did},'rollback')['ok']
    assert path.is_file()
    assert call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['cancel_requested'] is False


def test_hidden_v2_task_center_detail_remains_readable_evidence(fixture):
    api,_,_,_=fixture;batch=saved(fixture)
    raw={'v':1,'request_id':'hide-all','operation':'task.delete','params':{'id':batch['id']}}
    assert api.dispatch(json.dumps(raw),trusted_origin=True)['ok']
    raw.update(request_id='read-hidden',operation='task.center.detail')
    response=api.dispatch(json.dumps(raw),trusted_origin=True)
    assert response['ok'] and response['data']['version']==2
    assert all(d['hidden'] for d in response['data']['deliveries'])


def test_platform_fixed_date_intent_is_not_silently_hidden_by_relative_common(fixture):
    api,account,_,_=fixture
    settings=call(api,'settings.read')['data']['settings']
    settings['targets']=[{'account_id':account.id,'platform':'douyin'}]
    settings['schedule_by_platform']={'douyin':{'start_policy':{'kind':'fixed','value':'2020-01-01T12:00'},'schedule':settings['schedule']}}
    assert call(api,'settings.save',{'settings':settings},'platform-fixed')['ok']
    read=call(api,'settings.read')['data']
    assert read['settings']==settings and 'fixed_date_expired' in read['issues']


def test_unknown_target_cannot_be_falsely_relabelled_as_paused(fixture,db_engine):
    api,_,_,_=fixture;batch=saved(fixture)
    with session_scope(db_engine) as s:
        row=s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==batch['id']))
        from yijianjifa.features.tasks.service import TaskItemRow
        from yijianjifa.features.publish.history import PublishHistoryRow
        s.get(TaskItemRow,row.item_id).state=s.get(PublishHistoryRow,row.history_id).state='result_unknown';did=row.id
    assert not call(api,'target.pause',{'delivery_id':did},'wrong-pause')['ok']
    child=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]
    assert child['stage']=='提交结果待核对'


def test_pending_task_filter_counts_action_required_and_deleted_records_disappear(fixture,db_engine):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    api,_,_,adapter=fixture;batch=saved(fixture)
    with session_scope(db_engine) as session:
        children=list(session.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==batch['id'])))
        for child in children:
            _,item,history,*_=api._multi_publish.service.deliveries.load(session,child.id)
            item.state=history.state='pre_submit_failed'
        api._multi_publish.service.deliveries.aggregate(session,batch['id'])
    access=TaskCenterAccess(db_engine,multi_publish=api._multi_publish)
    data=access.list('', 'action', '', 0, 1)
    assert data['total']==1 and data['items'][0]['state']=='action_required'
    result=api.dispatch(json.dumps({'v':1,'request_id':'delete-pending','operation':'task.delete','params':{'id':batch['id']}}),trusted_origin=True)
    assert result['ok'],result
    assert access.list('', 'action', '', 0, 1)['total']==0
    assert adapter.calls==[]
    with session_scope(db_engine) as session:
        assert all(api._multi_publish.service.deliveries.load(session,c.id)[3].final_json is None for c in children)
