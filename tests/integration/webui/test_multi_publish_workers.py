import json
import os
import threading
import time
from types import SimpleNamespace
from sqlalchemy import select
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_runtime import HostWorkerRegistry,OWNER
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterValidation,AdapterOutcome
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def test_stopped_proof_requires_registered_finish_or_exact_host_death(db_engine,monkeypatch):
    from yijianjifa.app_services import multi_publish_runtime as module
    registry=HostWorkerRegistry(db_engine)
    assert registry.stopped('unknown') is False
    registry.started('joined');assert registry.stopped('joined') is False
    registry.finished('joined');assert registry.stopped('joined') is True
    with session_scope(db_engine) as s:s.add(Setting(key=OWNER+'old',value_json=json.dumps({'pid':12345,'birth':'old-birth','host_id':'old','stopped':False})))
    monkeypatch.setattr(module,'process_birth',lambda pid:'different-birth')
    assert HostWorkerRegistry(db_engine).stopped('old') is False
    assert HostWorkerRegistry(db_engine,host_lock_held=True).stopped('old') is True
    monkeypatch.setattr(module,'process_birth',lambda pid:'old-birth')
    assert HostWorkerRegistry(db_engine,host_lock_held=True).stopped('old') is False


def test_close_prevents_final_and_joins_exact_worker(db_engine,app_paths,tmp_path):
    entered=threading.Event();release=threading.Event();clicks=[]
    class Adapter:
        platform_id='douyin';verified_capabilities={'ordinary','publish_now','result_query'}
        def validate_target(self,target,payload,execution):return AdapterValidation(payload['platform_user_id'],True)
        def submit(self,payload,*,context,before_final):
            entered.set();release.wait(2);before_final();clicks.append('external-final');return AdapterOutcome('published')
        def reconcile(self,*a,**k):return AdapterOutcome('result_unknown')
    api=DesktopApi(db_engine,app_paths,publish_adapters={'douyin':Adapter()})
    account=AccountService(db_engine).create('douyin','111','合成')
    path=tmp_path/'synthetic.mp4';path.write_bytes(b'synthetic-close')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='合成')
    def call(op,p,rid):return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':'multi_publish.'+op,'params':p}),trusted_origin=True)
    p=call('prepare',{'video_ids':[video.id],'targets':[{'account_id':account.id,'platform':'douyin'}],'common_fields':{},'execution_mode':'publish_now'},'plan')['data']
    batch=call('confirm',{'prepared_id':p['prepared_id'],'intent_id':'plan','consent_partial':False},'save')['data']
    did=call('detail',{'id':batch['id']},'detail')['data']['deliveries'][0]['id']
    check=call('execution.check',{'id':batch['id'],'delivery_ids':[did]},'check')['data']
    assert call('execution.start',{'id':batch['id'],'delivery_ids':[did],'digest':check['digest'],'consent':True,'consent_partial':False},'start')['ok']
    assert entered.wait(2)
    assert api.close(0) is False
    release.set();assert api.close(5) is True
    assert not clicks
    with session_scope(db_engine) as s:
        attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==did))
        assert attempt.final_json is None
        assert api._multi_publish.registry.stopped(attempt.owner_hash)


def test_windows_dead_owner_probe_never_uses_posix_zero_signal(db_engine,monkeypatch):
    from yijianjifa.app_services import multi_publish_runtime as module
    with session_scope(db_engine) as s:s.add(Setting(key=OWNER+'windows-owner',value_json=json.dumps({'pid':12345,'birth':'creation-filetime','host_id':'old','stopped':False})))
    monkeypatch.setattr(module,'WINDOWS',True,raising=False)
    monkeypatch.setattr(module,'process_birth',lambda pid:None)
    monkeypatch.setattr(module,'_windows_process_identity',lambda pid:(None,False),raising=False)
    def forbidden(*args):raise AssertionError('Windows os.kill(pid,0) must not be a liveness probe')
    monkeypatch.setattr(module.os,'kill',forbidden)
    assert HostWorkerRegistry(db_engine,host_lock_held=True).stopped('windows-owner') is True
    monkeypatch.setattr(module,'_windows_process_identity',lambda pid:(None,None))
    assert HostWorkerRegistry(db_engine,host_lock_held=True).stopped('windows-owner') is False


def test_parked_intervention_refresh_stays_on_worker_and_never_resubmits_or_recreates_page(db_engine,app_paths,tmp_path,monkeypatch):
    import playwright.sync_api
    def forbidden_browser():raise AssertionError('fixture must never launch a live browser')
    monkeypatch.setattr(playwright.sync_api,'sync_playwright',forbidden_browser)
    refreshed=threading.Event();release=threading.Event();main_thread=threading.get_ident()
    class ParkedAdapter:
        platform_id='douyin';verified_capabilities={'ordinary','publish_now','result_query'}
        def __init__(self):
            self.thread_id=None;self.page=None;self.launches=0;self.submissions=[]
            self.refresh_calls=[];self.refreshes=0;self.cached=[];self.binding=None
        def bind_delivery(self,**binding):self.binding=binding
        def validate_target(self,target,payload,execution):return AdapterValidation(payload['platform_user_id'],True)
        def submit(self,payload,*,context,before_final):
            self.thread_id=threading.get_ident();self.submissions.append(payload['account_id'])
            assert self.page is None
            self.page=object();self.original_page=self.page;self.launches+=1
            self.cached=[{'id':'fixture-verification','delivery_id':self.binding['delivery_id'],'platform':'douyin','screenshot':'initial-fixture'}]
            return AdapterOutcome('paused_auth',context={'handle':'original-fixture-page'})
        def resume(self,*args,**kwargs):raise AssertionError('refresh must never resume a delivery')
        def reconcile(self,*args,**kwargs):raise AssertionError('refresh must never reconcile a delivery')
        def refresh_interventions(self):
            tid=threading.get_ident();self.refresh_calls.append(tid)
            if self.thread_id is None:return
            assert tid==self.thread_id and self.page is self.original_page
            self.refreshes+=1
            self.cached[0]={**self.cached[0],'screenshot':'fixture-'+str(self.refreshes)}
            if self.refreshes==2:
                refreshed.set();assert release.wait(2)
        def interventions(self):return [dict(row) for row in self.cached]
    class BrokenRefresh:
        platform_id='kuaishou';verified_capabilities=set()
        def __init__(self):self.threads=[]
        def refresh_interventions(self):
            self.threads.append(threading.get_ident());raise RuntimeError('fixture screenshot failure')
        def submit(self,*args,**kwargs):raise AssertionError('unselected platform must not submit')
        def reconcile(self,*args,**kwargs):raise AssertionError('unselected platform must not reconcile')
    parked=ParkedAdapter();broken=BrokenRefresh()
    api=DesktopApi(db_engine,app_paths,publish_adapters={'kuaishou':broken,'douyin':parked})
    try:
        account=AccountService(db_engine).create('douyin','111','合成停泊账号')
        path=tmp_path/'synthetic-parked.mp4';path.write_bytes(b'synthetic-parked-video')
        video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='合成验证')
        def call(op,p,rid):return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':'multi_publish.'+op,'params':p}),trusted_origin=True)
        plan=call('prepare',{'video_ids':[video.id],'targets':[{'account_id':account.id,'platform':'douyin'}],'common_fields':{},'execution_mode':'publish_now'},'plan')['data']
        batch=call('confirm',{'prepared_id':plan['prepared_id'],'intent_id':'plan','consent_partial':False},'save')['data']
        did=call('detail',{'id':batch['id']},'detail')['data']['deliveries'][0]['id']
        check=call('execution.check',{'id':batch['id'],'delivery_ids':[did]},'check')['data']
        assert call('execution.start',{'id':batch['id'],'delivery_ids':[did],'digest':check['digest'],'consent':True,'consent_partial':False},'start')['ok']
        assert refreshed.wait(2)
        runtime=api._multi_publish
        status=runtime.intervention_status()  # Main-thread status reads cached data only.
        assert status['request']['screenshot']=='fixture-2'
        assert parked.submissions==[account.id] and parked.launches==1
        assert parked.page is parked.original_page
        assert set(parked.refresh_calls)=={runtime.thread.ident} and runtime.thread.ident!=main_thread
        assert broken.threads and set(broken.threads)=={runtime.thread.ident}
        with session_scope(db_engine) as s:
            attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==did))
            assert attempt.final_json is None
        assert call('detail',{'id':batch['id']},'paused-detail')['data']['deliveries'][0]['state']=='paused_auth'
    finally:release.set();assert api.close(3)
