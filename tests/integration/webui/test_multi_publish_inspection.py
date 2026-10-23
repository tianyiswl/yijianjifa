"""Native inspection is transient and never obtains publishing authority."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, ResourceConsumerRow
from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class InspectionAdapter:
    platform_id='channels'
    verified_capabilities={'ordinary','publish_now','platform_schedule','result_query'}
    inspection_only=False
    def __init__(self):self.clones=[];self.calls=[];self.requests=[];self.closed=False;self.paused=False;self.cross_final=False;self.cancel=threading.Event()
    def create_inspection_adapter(self,cancel):
        clone=InspectionAdapter();clone.inspection_only=True;clone.cancel=cancel
        clone.paused=self.paused;clone.cross_final=self.cross_final;self.clones.append(clone);return clone
    def bind_delivery(self,**binding):self.binding=binding
    def validate_target(self,target,payload,execution):return AdapterValidation(payload['platform_user_id'],True)
    def submit(self,payload,*,context,before_final):
        self.calls.append(('upload',threading.get_ident(),dict(payload)))
        if self.cross_final:before_final();raise AssertionError('final guard must raise')
        if self.paused:
            self.requests=[{'id':'verify','delivery_id':self.binding['delivery_id'],'kind':'sms','account_name':'检查账号','platform':'channels','final_started':False}]
            return AdapterOutcome('paused_auth',context={'handle':'private-handle'},error_code='verification_required')
        return AdapterOutcome('inspection_complete')
    def resume(self,payload,*,context,before_final):
        assert context=={'handle':'private-handle'}
        self.calls.append(('resume',threading.get_ident(),dict(payload)));self.requests=[]
        return AdapterOutcome('inspection_complete')
    def interventions(self):return self.requests
    def reconcile(self,*args,**kwargs):raise AssertionError('inspection never reconciles a submission')
    def respond(self,rid,action,code):self.response=(rid,action,code);return {'accepted':True}
    def close(self):self.closed=True;self.requests=[]
    def request_shutdown(self):self.cancel.set()


def call(api,name,params=None,rid='read',trusted=True):
    return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':'multi_publish.'+name,'params':params or {}}),trusted_origin=trusted)


@pytest.fixture
def case(db_engine,app_paths,tmp_path):
    adapter=InspectionAdapter()
    api=DesktopApi(db_engine,app_paths,login_provider=SimpleNamespace(publish_adapters={'channels':adapter}))
    account=AccountService(db_engine).create('channels','finder-inspection','检查账号')
    with session_scope(db_engine) as s:
        row=s.get(AccountRow,account.id);row.login_state='valid';row.session_ref='synthetic-session'
    path=tmp_path/'source.mp4';path.write_bytes(b'owned-inspection-media')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=3000,title='仅检查测试')
    yield api,account,video,adapter
    api.close(5)


def prepare(case):
    api,account,video,_=case
    r=call(api,'prepare',{'video_ids':[video.id],'targets':[{'platform':'channels','account_id':account.id}],
        'common_fields':{},'execution_mode':'publish_now'},'plan')
    assert r['ok'],r
    return r['data']['prepared_id']


def wait(api,inspection_id,state):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        r=call(api,'inspection.status',{'inspection_id':inspection_id})
        assert r['ok'],r
        if r['data']['state']==state:return r['data']
        time.sleep(.01)
    raise AssertionError(r)


def assert_no_publish_records(db_engine):
    with session_scope(db_engine) as s:
        for model in (TaskCenterRow,PublishDeliveryRow,ResourceConsumerRow):
            assert s.scalar(select(func.count()).select_from(model))==0
        assert not list(s.scalars(select(Setting).where(Setting.key.startswith('multi_publish.authorization:'))))


def test_check_copies_media_does_not_create_publish_task_and_token_is_single_use(case,db_engine):
    api,_,video,adapter=case;token=prepare(case)
    first=call(api,'inspection.start',{'prepared_id':token},'check-1')
    assert first['ok'],first
    iid=first['data']['inspection_id'];status=wait(api,iid,'inspection_complete')
    again=call(api,'inspection.start',{'prepared_id':token},'check-2')
    assert again['data']['inspection_id']==iid
    assert len(adapter.clones)==1 and len(adapter.clones[0].calls)==1
    _,tid,payload=adapter.clones[0].calls[0]
    assert tid!=threading.get_ident()
    assert payload['video_path']!=str(video.effective_path)
    assert not Path(payload['video_path']).exists() and video.effective_path.exists()
    assert adapter.clones[0].closed and status['submitted'] is False
    assert status['report']
    encoded=json.dumps(status)
    for hidden in ('video_path','session_fingerprint','private-handle','video_hash'):assert hidden not in encoded
    assert_no_publish_records(db_engine)


def test_final_callback_is_hard_denied_even_for_broken_inspection_adapter(case,db_engine):
    api,_,_,adapter=case;adapter.cross_final=True
    start=call(api,'inspection.start',{'prepared_id':prepare(case)},'start')
    assert start['ok'],start
    status=wait(api,start['data']['inspection_id'],'failed')
    assert status['error_code']=='inspection_final_blocked' and status['submitted'] is False
    assert adapter.clones[0].closed
    assert_no_publish_records(db_engine)


def test_verification_resumes_original_check_not_publish_coordinator(case,db_engine):
    api,_,_,adapter=case;adapter.paused=True
    start=call(api,'inspection.start',{'prepared_id':prepare(case)},'start');assert start['ok'],start
    iid=start['data']['inspection_id'];wait(api,iid,'paused_auth')
    request=call(api,'intervention.status')['data']['request']
    assert request['inspection_id']==iid and 'delivery_id' not in request
    assert not call(api,'inspection.respond',{'inspection_id':iid,'id':'verify','action':'done'})['ok']
    response=call(api,'inspection.respond',{'inspection_id':iid,'id':'verify','action':'done','consent_same_identity':True})
    assert response['ok'],response
    wait(api,iid,'inspection_complete')
    assert [r[0] for r in adapter.clones[0].calls]==['upload','resume']
    assert adapter.clones[0].calls[0][2]['video_path']==adapter.clones[0].calls[1][2]['video_path']
    assert_no_publish_records(db_engine)


def test_cancel_paused_check_closes_original_page_and_reopen_never_restarts(case,db_engine,app_paths):
    api,_,_,adapter=case;adapter.paused=True
    start=call(api,'inspection.start',{'prepared_id':prepare(case)},'start');assert start['ok'],start
    iid=start['data']['inspection_id'];wait(api,iid,'paused_auth')
    assert call(api,'inspection.cancel',{'inspection_id':iid})['ok']
    wait(api,iid,'cancelled');assert adapter.clones[0].closed
    assert api.close(5)
    reopened=DesktopApi(db_engine,app_paths,login_provider=SimpleNamespace(publish_adapters={'channels':adapter}))
    try:
        reopened.start_local_checks();time.sleep(.05)
        assert not call(reopened,'inspection.status',{'inspection_id':iid})['ok']
        assert len(adapter.clones)==1
        assert_no_publish_records(db_engine)
    finally:reopened.close(5)


def test_check_rejects_expired_or_forged_token_and_untrusted_native_origin(case):
    api,*_=case;token=prepare(case)
    assert not call(api,'inspection.start',{'prepared_id':'a'*48},'fake')['ok']
    assert not call(api,'inspection.start',{'prepared_id':token},'unsafe',trusted=False)['ok']
    assert not call(api,'inspection.start',{'prepared_id':token,'publish':True},'extra')['ok']
    record=api._multi_publish.service._drafts[token]
    record['expires']=datetime.now(timezone.utc)-timedelta(seconds=1)
    assert not call(api,'inspection.start',{'prepared_id':token},'expired')['ok']


def test_check_revalidates_media_and_account_before_any_upload(case):
    api,account,video,adapter=case;token=prepare(case)
    with session_scope(api._engine) as s:s.get(AccountRow,account.id).platform_user_id='other-finder'
    r=call(api,'inspection.start',{'prepared_id':token},'start')
    if r['ok']:wait(api,r['data']['inspection_id'],'failed')
    assert not adapter.clones or not adapter.clones[0].calls


def test_changed_video_never_uploads_and_selection_is_exactly_one(case,db_engine,tmp_path):
    api,account,video,adapter=case;token=prepare(case)
    video.effective_path.write_bytes(b'changed-after-preparation')
    start=call(api,'inspection.start',{'prepared_id':token},'changed');assert start['ok'],start
    status=wait(api,start['data']['inspection_id'],'failed')
    assert status['error_code']=='inspection_media_changed' and not adapter.clones
    video.effective_path.write_bytes(b'owned-inspection-media')
    other=tmp_path/'second.mp4';other.write_bytes(b'another-owned-video')
    second=FinishedVideoService(db_engine).import_external(other,duration_ms=3000,title='第二条')
    prepared=call(api,'prepare',{'video_ids':[video.id,second.id],
        'targets':[{'platform':'channels','account_id':account.id}], 'common_fields':{},'execution_mode':'publish_now'},'two')
    assert prepared['ok'],prepared
    denied=call(api,'inspection.start',{'prepared_id':prepared['data']['prepared_id']},'many')
    assert denied['error']['code']=='inspection_selection_required'


def test_verification_expiry_cleans_media_browser_and_account(case,monkeypatch):
    api,account,_,adapter=case;adapter.paused=True
    start=call(api,'inspection.start',{'prepared_id':prepare(case)},'start');assert start['ok'],start
    iid=start['data']['inspection_id'];wait(api,iid,'paused_auth')
    jobs=api._multi_publish.inspections
    with jobs.lock:jobs.jobs[iid]['verification_deadline']=time.monotonic()-1
    status=wait(api,iid,'failed')
    assert status['error_code']=='inspection_verification_expired'
    assert adapter.clones[0].closed and not jobs.busy_accounts()
    assert not Path(adapter.clones[0].calls[0][2]['video_path']).exists()


def test_unexpired_broker_request_does_not_get_permanently_stuck_after_refresh_actions(case):
    api,_,_,adapter=case;adapter.paused=True
    start=call(api,'inspection.start',{'prepared_id':prepare(case)},'start');assert start['ok'],start
    iid=start['data']['inspection_id'];wait(api,iid,'paused_auth')
    jobs=api._multi_publish.inspections
    with jobs.lock:
        # Broker deadlines survive resume/refresh; they may expire earlier than
        # the controller's most recent step deadline.
        adapter.clones[0].requests[0]['expires_at']=time.time()-1
    status=wait(api,iid,'failed')
    assert status['error_code']=='inspection_verification_expired' and adapter.clones[0].closed


def test_formal_start_is_rejected_while_inspection_owns_same_account(case,db_engine):
    api,_,_,adapter=case;adapter.paused=True;token=prepare(case)
    start=call(api,'inspection.start',{'prepared_id':token},'check');assert start['ok'],start
    wait(api,start['data']['inspection_id'],'paused_auth')
    batch=call(api,'confirm',{'prepared_id':token,'intent_id':'plan','consent_partial':False},'save')
    assert batch['ok'],batch
    details=call(api,'detail',{'id':batch['data']['id']})['data'];ids=[d['id'] for d in details['deliveries']]
    checked=call(api,'execution.check',{'id':batch['data']['id'],'delivery_ids':ids})['data']
    result=call(api,'execution.start',{'id':batch['data']['id'],'delivery_ids':ids,'digest':checked['digest'],'consent':True,'consent_partial':False},'formal')
    assert result['error']['code']=='inspection_busy'
    with session_scope(db_engine) as s:assert not list(s.scalars(select(Setting).where(Setting.key.startswith('multi_publish.authorization:'))))


@pytest.mark.parametrize('close_always_fails',[False,True])
def test_shutdown_retries_terminal_inspection_cleanup_on_original_worker(case,db_engine,monkeypatch,close_always_fails):
    api,account,_,adapter=case
    create=adapter.create_inspection_adapter
    close_threads=[]

    def create_with_failed_close(cancel):
        clone=create(cancel)
        close=clone.close

        def retryable_close():
            close_threads.append(threading.get_ident())
            if close_always_fails or len(close_threads)==1:
                raise RuntimeError('synthetic inspection close failure')
            close()

        clone.close=retryable_close
        return clone

    monkeypatch.setattr(adapter,'create_inspection_adapter',create_with_failed_close)
    start=call(api,'inspection.start',{'prepared_id':prepare(case)},'start')
    assert start['ok'],start
    iid=start['data']['inspection_id']
    status=wait(api,iid,'inspection_complete')
    jobs=api._multi_publish.inspections
    clone=adapter.clones[0]
    with jobs.lock:
        assert jobs.jobs[iid]['adapter'] is clone
        assert jobs.jobs[iid]['cleanup_failed'] is True
    assert jobs.busy_accounts()=={account.id}
    assert not clone.closed and '未能确认关闭' in status['message']

    assert api.close(5)
    # Failed terminal cleanup is retried after the other SDK owners close.
    assert len(close_threads)==(4 if close_always_fails else 2)
    assert len(set(close_threads))==1 and close_threads[0]!=threading.get_ident()
    assert len(clone.calls)==1
    with jobs.lock:
        job=jobs.jobs[iid]
        assert job['state']=='inspection_complete' and job['error_code'] is None
        if close_always_fails:
            assert job['adapter'] is clone and job['cleanup_failed'] is True
            assert not clone.closed and '未能确认关闭' in job['message']
        else:
            assert job['adapter'] is None and job['cleanup_failed'] is False
            assert clone.closed and '未能确认关闭' not in job['message']
    assert jobs.busy_accounts()==({account.id} if close_always_fails else set())
    assert_no_publish_records(db_engine)
