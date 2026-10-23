"""Execution grants bind frozen attempt versions; synthetic adapters only."""
import json
import threading
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import select
from test_multi_publish_api import fixture,call,saved
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_runtime import AUTH
from yijianjifa.features.publish.coordinator import AdapterOutcome
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow,PublishDeliveryRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def start(api,batch_id,ids,rid):
    check=call(api,'execution.check',{'id':batch_id,'delivery_ids':ids})['data']
    result=call(api,'execution.start',{'id':batch_id,'delivery_ids':ids,'digest':check['digest'],
        'consent':True,'consent_partial':check['requires_partial_consent']},rid)
    assert result['ok'],result
    return result


def authorization(engine,task_id):
    with session_scope(engine) as s:
        row=s.get(Setting,AUTH+task_id)
        return json.loads(row.value_json) if row else None


@pytest.mark.parametrize('change',['same_fields','title','reschedule'])
def test_saved_reprepare_or_reschedule_needs_fresh_start_even_when_fields_are_unchanged(fixture,db_engine,monkeypatch,change):
    api,_,_,adapter=fixture;runtime=api._multi_publish
    monkeypatch.setattr(runtime,'wake',lambda:None)
    batch=saved(fixture);children=call(api,'detail',{'id':batch['id']})['data']['deliveries']
    did,sibling=[row['id'] for row in children]
    start(api,batch['id'],[did,sibling],'first-start')
    sibling_grant=authorization(db_engine,batch['id'])['targets'][sibling].copy()
    submissions=[];second_submission=threading.Event()
    def submit(payload,*,context,before_final):
        submissions.append(payload)
        if len(submissions)==1:return AdapterOutcome('pre_submit_failed',error_code='fixture-before-final')
        before_final();second_submission.set();return AdapterOutcome('published')
    adapter.submit=submit
    assert runtime.coordinator.run_once(batch['id'],[did])=='pre_submit_failed'
    with session_scope(db_engine) as s:old_digest=s.get(PublishDeliveryRow,did).request_digest
    if change=='reschedule':
        when=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        result=call(api,'target.reschedule',{'delivery_id':did,'execution_mode':'platform_schedule','scheduled_at':when},'change')
    else:
        result=call(api,'target.reprepare',{'delivery_id':did,'fields':{} if change=='same_fields' else {'title':'新的合成标题'},'reset_fields':[]},'change')
    assert result['ok'] and result['data']['attempt']==2 and result['data']['state']=='waiting'
    grant=authorization(db_engine,batch['id'])
    assert did not in grant['delivery_ids'] and did not in grant['targets']
    assert grant['targets'][sibling]==sibling_grant
    bindings=[];adapter.bind_delivery=lambda **binding:bindings.append(binding)
    with pytest.raises(ValueError,match='authorized'):
        runtime.catalog.transport_for('douyin').bind_delivery(task_id=batch['id'],delivery_id=did,digest=old_digest)
    assert bindings==[]  # A worker's previously read id cannot open the adapter.
    # Keep the sibling local so a real worker loop can inspect the refrozen
    # target without an unrelated synthetic submission affecting this count.
    assert call(api,'target.pause',{'delivery_id':sibling},'pause-sibling')['ok']
    visited=threading.Event();iterations=[]
    def refresh():
        iterations.append(threading.get_ident())
        if len(iterations)>=2:visited.set()
    adapter.refresh_interventions=refresh
    assert runtime.start() and visited.wait(2)
    assert len(submissions)==1
    assert call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['accepted'] is False
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,did)
        if change=='same_fields':assert row.request_digest==old_digest
        attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==did,DeliveryAttemptRow.number==2))
        assert attempt.owner_hash is None and attempt.final_json is None
    start(api,batch['id'],[did],'second-start')
    assert second_submission.wait(2)
    assert len(submissions)==2
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,did);grant=authorization(db_engine,batch['id'])['targets'][did]
        assert grant=={'attempt':2,'request_digest':row.request_digest}


@pytest.mark.parametrize('change',['same_fields','reschedule'])
def test_collected_worker_subset_cannot_claim_refrozen_attempt_after_grant_revocation(fixture,db_engine,monkeypatch,change):
    """Freeze the real worker between grant collection and coordinator claim."""
    api,_,_,adapter=fixture;runtime=api._multi_publish
    monkeypatch.setattr(runtime,'wake',lambda:None)
    batch=saved(fixture);did=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['id']
    start(api,batch['id'],[did],'race-first-start')
    submissions=[]
    def fail_before_final(payload,*,context,before_final):
        submissions.append(payload)
        return AdapterOutcome('pre_submit_failed',error_code='fixture-before-final')
    adapter.submit=fail_before_final
    assert runtime.coordinator.run_once(batch['id'],[did])=='pre_submit_failed'
    collected=threading.Event();release=threading.Event();processed=threading.Event()
    original=runtime.coordinator.run_once
    def hold_collected_subset(task_id,ids=None):
        if task_id==batch['id'] and ids==[did] and not collected.is_set():
            collected.set()
            assert release.wait(3)
            try:return original(task_id,ids)
            finally:processed.set()
        return original(task_id,ids)
    monkeypatch.setattr(runtime.coordinator,'run_once',hold_collected_subset)
    try:
        assert runtime.start() and collected.wait(2)
        if change=='reschedule':
            when=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
            changed=call(api,'target.reschedule',{'delivery_id':did,'execution_mode':'platform_schedule','scheduled_at':when},'race-refreeze')
        else:
            changed=call(api,'target.reprepare',{'delivery_id':did,'fields':{},'reset_fields':[]},'race-refreeze')
        assert changed['ok'] and changed['data']['attempt']==2
        assert authorization(db_engine,batch['id'])['targets']=={}
        release.set();assert processed.wait(2)
        assert len(submissions)==1  # No new platform operation was authorized.
        with session_scope(db_engine) as s:
            row=s.get(PublishDeliveryRow,did)
            attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==did,DeliveryAttemptRow.number==2))
            assert row.attempt==2 and attempt.owner_hash is None
            assert attempt.final_json is None and attempt.result_json is None
        target=next(d for d in call(api,'detail',{'id':batch['id']})['data']['deliveries'] if d['id']==did)
        assert target['state']=='waiting' and target['accepted'] is False
    finally:
        release.set();assert api.close(3)


def test_legacy_id_only_authorization_does_not_start_browser_or_target(fixture,db_engine):
    api,_,_,adapter=fixture;batch=saved(fixture)
    children=call(api,'detail',{'id':batch['id']})['data']['deliveries'];did,sibling=[d['id'] for d in children]
    with session_scope(db_engine) as s:s.add(Setting(key=AUTH+batch['id'],value_json=json.dumps({'delivery_ids':[did,sibling]})))
    visited=threading.Event();iterations=[]
    adapter.refresh_interventions=lambda:(iterations.append(threading.get_ident()),visited.set() if len(iterations)>=2 else None)
    assert api._multi_publish.start() and visited.wait(2)
    assert adapter.calls==[]
    with session_scope(db_engine) as s:
        attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==did))
        assert attempt.owner_hash is None and attempt.final_json is None
    # Re-authorizing one explicitly selected target must not upgrade any of the
    # old record's other ids into executable attempts.
    submitted=threading.Event()
    def submit(payload,*,context,before_final):
        adapter.calls.append(payload['video_id']);before_final();submitted.set()
        return AdapterOutcome('published')
    adapter.submit=submit
    start(api,batch['id'],[did],'fresh-start')
    assert submitted.wait(2) and adapter.calls==[children[0]['video_id']]
    grant=authorization(db_engine,batch['id'])
    assert grant['version']==2 and grant['delivery_ids']==[did] and sibling not in grant['targets']


def test_rejected_refreeze_rolls_back_without_revoking_or_changing_the_original_grant(fixture,db_engine,monkeypatch):
    api,_,_,_=fixture;monkeypatch.setattr(api._multi_publish,'wake',lambda:None)
    batch=saved(fixture);did=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['id']
    start(api,batch['id'],[did],'original-start');original=authorization(db_engine,batch['id'])
    result=call(api,'target.reprepare',{'delivery_id':did,'fields':{'title':''},'reset_fields':[]},'invalid-change')
    assert not result['ok'] and authorization(db_engine,batch['id'])==original
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,did)
        assert row.attempt==1 and original['targets'][did]['request_digest']==row.request_digest


@pytest.mark.parametrize('invalid',['legacy','attempt','digest'])
def test_both_final_guards_independently_reject_stale_authorization(fixture,db_engine,monkeypatch,invalid):
    api,_,_,adapter=fixture;runtime=api._multi_publish
    monkeypatch.setattr(runtime,'wake',lambda:None)
    batch=saved(fixture);did=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['id']
    start(api,batch['id'],[did],'guard-start')
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,did);binding={'task_id':row.task_id,'delivery_id':row.id,'digest':row.request_digest}
    guarded=runtime.catalog.transport_for('douyin');guarded.bind_delivery(**binding)
    with session_scope(db_engine) as s:
        auth=s.get(Setting,AUTH+batch['id']);data=json.loads(auth.value_json)
        if invalid=='legacy':data={'delivery_ids':[did]}
        elif invalid=='attempt':data['targets'][did]['attempt']+=1
        else:data['targets'][did]['request_digest']='0'*64
        auth.value_json=json.dumps(data)
    reached=[]
    with pytest.raises(ValueError,match='authorized'):guarded._final(lambda:reached.append('before-final'))
    assert reached==[]
    with session_scope(db_engine) as s:
        with pytest.raises(ValueError,match='authorized'):runtime._authorize_final(s,s.get(PublishDeliveryRow,did))
    assert adapter.calls==[]


def test_exact_attempt_authorization_survives_reopen_without_automatic_legacy_migration(fixture,db_engine,app_paths,monkeypatch):
    api,_,_,adapter=fixture;runtime=api._multi_publish
    monkeypatch.setattr(runtime,'wake',lambda:None)
    batch=saved(fixture);ids=[d['id'] for d in call(api,'detail',{'id':batch['id']})['data']['deliveries']]
    start(api,batch['id'],ids,'durable-start');original=authorization(db_engine,batch['id'])
    assert original['version']==2 and api.close(3)
    complete=threading.Event()
    def submit(payload,*,context,before_final):
        adapter.calls.append(payload['video_id']);before_final()
        if len(adapter.calls)==2:complete.set()
        return AdapterOutcome('published')
    adapter.submit=submit
    reopened=DesktopApi(db_engine,app_paths,publish_adapters={'douyin':adapter})
    try:
        assert reopened._multi_publish.start() and complete.wait(2)
        assert len(adapter.calls)==2
        assert authorization(db_engine,batch['id'])==original
    finally:reopened.close(3)


def test_same_attempt_verification_resume_and_postfinal_query_never_use_submit_again(fixture,db_engine,monkeypatch):
    api,_,_,adapter=fixture;runtime=api._multi_publish
    monkeypatch.setattr(runtime,'wake',lambda:None)
    batch=saved(fixture);did=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['id']
    start(api,batch['id'],[did],'resume-start');original=authorization(db_engine,batch['id'])['targets'][did]
    calls=[]
    def submit(payload,*,context,before_final):
        calls.append('submit');return AdapterOutcome('paused_auth',context={'handle':'same-fixture-page'})
    def resume(payload,*,context,before_final):
        assert context=={'handle':'same-fixture-page'}
        calls.append('resume');before_final();return AdapterOutcome('result_unknown')
    def reconcile(payload,*,context):
        calls.append('query');return AdapterOutcome('published')
    adapter.submit=submit;adapter.resume=resume;adapter.reconcile=reconcile
    assert runtime.coordinator.run_once(batch['id'],[did])=='paused_auth'
    resumed=call(api,'target.resume',{'delivery_id':did,'consent_same_identity':True},'resume');assert resumed['ok'],resumed
    assert runtime.coordinator.resume_verification(did)=='result_unknown'
    assert authorization(db_engine,batch['id'])['targets'][did]==original
    with session_scope(db_engine) as s:s.delete(s.get(Setting,AUTH+batch['id']))
    assert runtime.coordinator.reconcile_delivery(did)=='published'
    assert calls==['submit','resume','query']
    with session_scope(db_engine) as s:assert s.get(PublishDeliveryRow,did).attempt==1
