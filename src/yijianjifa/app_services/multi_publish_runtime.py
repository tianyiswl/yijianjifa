"""One trusted worker, explicit durable authorization and conservative owner proof."""
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.publish.deliveries import LocalWorkerRegistry,PublishDeliveryRow,canonical
from yijianjifa.features.publish.coordinator import PublishCoordinator
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from .multi_publish import MultiPublishService
from .multi_publish_dto import detail

AUTH='multi_publish.authorization:'
OWNER='multi_publish.owner:'
PAUSE='multi_publish.target_pause:'
WINDOWS=os.name=='nt'


def authorized_targets(session,task_id):
    """Read only grants for the exact current frozen attempt; legacy is unproved."""
    row=session.get(Setting,AUTH+task_id)
    if row is None:return {}
    try:data=json.loads(row.value_json)
    except (TypeError,ValueError):return {}
    if (type(data) is not dict or type(data.get('version')) is not int or data['version']!=2 or
        type(data.get('delivery_ids')) is not list or type(data.get('targets')) is not dict):return {}
    result={}
    for did in data['delivery_ids']:
        if type(did) is not str:continue
        grant=data['targets'].get(did)
        if (type(grant) is not dict or set(grant)!={'attempt','request_digest'} or
            type(grant['attempt']) is not int or grant['attempt']<1 or type(grant['request_digest']) is not str):continue
        delivery=session.get(PublishDeliveryRow,did)
        if (delivery is not None and delivery.task_id==task_id and delivery.attempt==grant['attempt'] and
            delivery.request_digest==grant['request_digest']):result[did]=dict(grant)
    return result


def update_authorization(session,task_id,*,add=(),remove=()):
    # Callers own the command's BEGIN IMMEDIATE transaction. Never promote an
    # old id-only record into a grant for today's attempt without a fresh start.
    targets=authorized_targets(session,task_id)
    for did in remove:targets.pop(did,None)
    for did in add:
        delivery=session.get(PublishDeliveryRow,did)
        if delivery is None or delivery.task_id!=task_id:raise ValueError('authorization target mismatch')
        targets[did]={'attempt':delivery.attempt,'request_digest':delivery.request_digest}
    data={'version':2,'delivery_ids':list(targets),'targets':targets}
    row=session.get(Setting,AUTH+task_id)
    if row:row.value_json=canonical(data)
    else:session.add(Setting(key=AUTH+task_id,value_json=canonical(data)))
    return data


def _windows_process_identity(pid):
    """Read creation FILETIME and exit status; access denial proves nothing."""
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*[ctypes.POINTER(wintypes.FILETIME)]*4]
    handle=kernel.OpenProcess(0x1000,False,pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:return None,False if ctypes.get_last_error()==87 else None
    try:
        status=wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle,ctypes.byref(status)):return None,None
        if status.value!=259:return None,False  # STILL_ACTIVE
        times=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):return None,None
        creation=times[0]
        return str((creation.dwHighDateTime<<32)|creation.dwLowDateTime),True
    finally:kernel.CloseHandle(handle)


def process_birth(pid):
    if WINDOWS:
        try:return _windows_process_identity(pid)[0]
        except (OSError,AttributeError):return None
    try:
        p=subprocess.run(['ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=2)
        return p.stdout.strip() if p.returncode==0 and p.stdout.strip() else None
    except (OSError,subprocess.SubprocessError):return None


class HostWorkerRegistry(LocalWorkerRegistry):
    def __init__(self,engine,*,host_lock_held=False):
        super().__init__();self.engine=engine;self.host_id=uuid.uuid4().hex
        self.pid=os.getpid();self.birth=process_birth(self.pid);self.host_lock_held=host_lock_held is True
    def _save(self,owner,stopped):
        with session_scope(self.engine) as s:
            data={'host_id':self.host_id,'pid':self.pid,'birth':self.birth,'stopped':stopped}
            row=s.get(Setting,OWNER+owner)
            if row:row.value_json=json.dumps(data)
            else:s.add(Setting(key=OWNER+owner,value_json=json.dumps(data)))
    def started(self,owner):super().started(owner);self._save(owner,False)
    def finished(self,owner):super().finished(owner);self._save(owner,True)
    def resumed(self,owner):super().resumed(owner);self._save(owner,False)
    def stopped(self,owner):
        with self._lock:
            if owner in self._owners:return self._owners[owner] is True
        with session_scope(self.engine) as s:
            row=s.get(Setting,OWNER+owner)
            if row is None:return False
            try:p=json.loads(row.value_json)
            except ValueError:return False
        if p.get('stopped') is True:return True
        # Host startup holds the exact data-directory lock. Only an absent old
        # process or a different nonempty birth identity proves that owner dead.
        if not self.host_lock_held or type(p.get('pid')) is not int or not p.get('birth'):return False
        observed=process_birth(p['pid'])
        if observed is not None:return observed!=p['birth']
        if WINDOWS:
            try:return _windows_process_identity(p['pid'])[1] is False
            except (OSError,AttributeError):return False
        try:os.kill(p['pid'],0)
        except ProcessLookupError:return True
        except (OSError,PermissionError):return False
        return False


class GuardedAdapter:
    def __init__(self,adapter,runtime):self.adapter=adapter;self.runtime=runtime;self.binding=None
    def __getattr__(self,key):return getattr(self.adapter,key)
    def bind_delivery(self,**binding):
        # Reject stale worker subsets before the platform adapter can open or
        # upload anything. A refrozen attempt may retain the same payload hash.
        with session_scope(self.runtime.engine) as s:
            target=authorized_targets(s,binding['task_id']).get(binding['delivery_id'])
            if target is None or target['request_digest']!=binding['digest']:raise ValueError('target is not authorized')
            self.binding={**binding,'attempt':target['attempt']}
        binder=getattr(self.adapter,'bind_delivery',None)
        if binder:binder(**binding)
    def _final(self,before):
        if self.runtime.closed.is_set():raise ValueError('host closing')
        if self.binding is None:raise ValueError('missing host binding')
        with session_scope(self.runtime.engine) as s:
            grant=authorized_targets(s,self.binding['task_id']).get(self.binding['delivery_id'])
            if grant!={'attempt':self.binding['attempt'],'request_digest':self.binding['digest']}:raise ValueError('target is not authorized')
        before()
    def submit(self,payload,*,context,before_final):return self.adapter.submit(payload,context=context,before_final=lambda:self._final(before_final))
    def resume(self,payload,*,context,before_final):return self.adapter.resume(payload,context=context,before_final=lambda:self._final(before_final))


class HostMultiPublishService(MultiPublishService):
    def cleanup_resources(self):
        from sqlalchemy.engine import Connection
        if isinstance(self.engine,Connection):return
        return super().cleanup_resources()
    def cancel_delivery(self,delivery_id):
        with session_scope(self.engine) as s:
            row,item,*_=self.deliveries.load(s,delivery_id)
            if item.state in {'interrupted','result_unknown','published','platform_scheduled','platform_accepted','processing','reviewing','platform_rejected'}:
                row.cancel_requested=True
                return {'id':delivery_id,'state':item.state,'cancel_requested':True,'withdrawn':False}
        return super().cancel_delivery(delivery_id)
    def capture_account(self,account):
        import hashlib
        return {'session_fingerprint':hashlib.sha256((account.session_ref or '').encode()).hexdigest()}
    def _hold(self,payload):
        if payload.get('content_type') == 'image_post':
            return self._hold_image_post(payload)
        try:host_adapter=self.catalog.transport_for(payload['platform'])
        except ValueError:host_adapter=None
        if host_adapter is None and payload['platform'] in {'kuaishou','bilibili'}:
            return 'waiting_configuration','official_service_not_configured'
        readiness=getattr(host_adapter,'plan_readiness',None)
        if callable(readiness):
            reason=readiness(payload)
            if reason:return 'waiting_configuration',reason
        rich={k:v for k,v in payload['fields'].items() if k in {'location','declaration'} and v is not None}
        if rich:
            try:adapter=self.catalog.transport_for(payload['platform'])
            except ValueError:return super()._hold(payload)
            check=getattr(adapter,'validate_fields',None)
            if not callable(check) or not check(payload):return 'waiting_configuration','platform_fields_require_validation'
        clean={**payload,'fields':{k:v for k,v in payload['fields'].items() if k not in {'location','declaration'}}}
        return super()._hold(clean)


class MultiPublishRuntime:
    def __init__(self,engine,paths,*,adapters=None,host_lock_held=False):
        self.engine,self.paths=engine,paths;self.closed=threading.Event();self.wakeup=threading.Event()
        self.registry=HostWorkerRegistry(engine,host_lock_held=host_lock_held)
        original=PlatformCatalog(transports=adapters)
        transports={p:GuardedAdapter(original.transport_for(p),self) for p in original._transports}
        self.catalog=PlatformCatalog(transports=transports)
        self.service=HostMultiPublishService(engine,paths,catalog=self.catalog,workers=self.registry)
        self.service.deliveries.claim_guard=self._authorize_claim
        self.service.deliveries.final_guard=self._authorize_final
        self.coordinator=PublishCoordinator(self.service);self.jobs=queue.Queue();self.thread=None
        self._start_lock=threading.Lock();self.current=None;self.deferred=set()
        self.daily_recheck = None
        self.daily_recheck_error = None
        from .multi_publish_platform_options import PlatformOptionsJobs
        self.platform_options=PlatformOptionsJobs(self)
        from .multi_publish_inspection import InspectionJobs
        self.inspections=InspectionJobs(self)
    def _authorize_claim(self,session,delivery):
        # Check before owner/state writes in the boundary's BEGIN IMMEDIATE.
        # Collected ids are only candidates, never authority for a refrozen
        # attempt whose original grant was revoked by the command transaction.
        if self.closed.is_set():raise ValueError('host closing')
        grant=authorized_targets(session,delivery.task_id).get(delivery.id)
        if session.get(Setting,PAUSE+delivery.id) is not None or grant!={'attempt':delivery.attempt,'request_digest':delivery.request_digest}:
            raise ValueError('target is paused or not authorized')

    def _authorize_final(self,session,delivery):
        # Repeat the same exact-attempt permission at the final journal boundary.
        self._authorize_claim(session,delivery)

    def start(self):
        with self._start_lock:
            if self.closed.is_set():return False
            if self.thread and self.thread.is_alive():return True
            self.service.recover_after_restart()
            self.thread=threading.Thread(target=self._run,name='p011-multi-publish',daemon=False);self.thread.start()
            return True
    def wake(self):self.start();self.wakeup.set()
    def enqueue(self,action,delivery_id):
        if self.closed.is_set():raise ValueError('app closing')
        self.jobs.put((action,delivery_id));self.wake()
        return {'delivery_id':delivery_id,'accepted':True,'stage':'waiting_for_worker','submitted':False if action=='resume' else None}
    def cancel_session(self,delivery_id):
        # Only an existing worker can own a live page. Cancelling a saved local
        # arrangement does not start publishing or open a browser.
        if self.thread and self.thread.is_alive() and not self.closed.is_set():
            self.jobs.put(('cancel_session',delivery_id));self.wakeup.set()
    def _run(self):
        last_daily_check = 0.0
        try:
            while not self.closed.is_set():
                self.platform_options.expire()
                self.inspections.refresh()
                # Live-page work stays on its owning worker. Status APIs only
                # read cached interventions; each adapter skips inactive or
                # foreign-thread sessions and owns its refresh cadence.
                for platform in self.catalog._transports:
                    try:
                        refresh=getattr(self.catalog.transport_for(platform),'refresh_interventions',None)
                        if callable(refresh):refresh()
                    except Exception:pass  # One screenshot failure must not stop other targets.
                progressed=False
                try:action,did=self.jobs.get_nowait()
                except queue.Empty:pass
                else:
                    self.current=did[1] if action=='verification_view' else did
                    try:
                        if action=='resume':self.coordinator.resume_verification(did)
                        elif action=='reconcile':self.coordinator.reconcile_delivery(did)
                        elif action=='verification_view':
                            platform,delivery_id,request_id,revision=did
                            result=self.catalog.transport_for(platform).process_verification_view(delivery_id,request_id,revision)
                            if result.get('verification_complete') is True:
                                self.enqueue('reconcile' if result.get('final_started') else 'resume',delivery_id)
                        elif action=='platform_options':self.platform_options.run(did)
                        elif action=='platform_options_action':self.platform_options.run_action(*did)
                        elif action=='inspection_start':self.inspections.run(did)
                        elif action=='inspection_resume':self.inspections.run(did,resume=True)
                        elif action=='inspection_view':self.inspections.process_view(*did)
                        elif action=='inspection_cancel':self.inspections.finish_cancel(did)
                        elif action=='cancel_session':
                            for platform in self.catalog._transports:
                                closer=getattr(self.catalog.transport_for(platform),'cancel_delivery_session',None)
                                if closer:closer(did)
                    except Exception:pass  # Persistent child evidence remains authoritative.
                    finally:self.current=None
                    progressed=True
                with session_scope(self.engine) as s:
                    batches=[]
                    for record in s.scalars(select(Setting).where(Setting.key.startswith(AUTH))):
                        task_id=record.key[len(AUTH):]
                        busy=self.inspections.busy_accounts()|self.platform_options.busy_accounts()
                        ids=[did for did in authorized_targets(s,task_id) if s.get(PublishDeliveryRow,did).account_id not in busy]
                        if ids:batches.append((task_id,ids))
                for task_id,ids in batches:
                    if self.closed.is_set():break
                    try:
                        result=self.coordinator.run_once(task_id,ids)
                        progressed=progressed or result is not None
                    except Exception:continue
                if self.daily_recheck is not None and time.monotonic() - last_daily_check >= .5:
                    last_daily_check = time.monotonic()
                    try:self.daily_recheck()
                    except Exception as error:
                        if self.daily_recheck_error is None: raise
                        self.daily_recheck_error(error)
                        if isinstance(error, AssertionError): raise
                if not progressed:self.wakeup.wait(.2);self.wakeup.clear()
        finally:
            # A failed context close retains its SDK lease and account lock.
            # Retry only cleanup after the other owners release their pages;
            # this never repeats an upload or a final submission.
            cleaners=[jobs.close for jobs in (self.platform_options,self.inspections)]
            cleaners.extend(closer for platform in self.catalog._transports
                if (closer:=getattr(self.catalog.transport_for(platform),'close',None)))
            failed=[]
            for closer in cleaners:
                try:closer()
                except Exception:failed.append(closer)
            for closer in failed:
                try:closer()
                except Exception:pass  # Keep failed owner references; no false close proof.
            # Job cleanup records failures rather than raising. Once ordinary
            # leases are gone, retry those clones; a final SDK stop may prove
            # their page dead while still reporting the original close error.
            for _ in range(2):
                for jobs in (self.platform_options,self.inspections):
                    try:jobs.close()
                    except Exception:pass
    def close(self,timeout=0):
        self.closed.set();self.wakeup.set()
        self.inspections.request_shutdown()
        for platform in self.catalog._transports:
            signal=getattr(self.catalog.transport_for(platform),'request_shutdown',None)
            if signal:signal()
        if self.thread:self.thread.join(timeout)
        return self.thread is None or not self.thread.is_alive()
    def detail(self,task_id):
        result=detail(self.service,task_id,coordinator=self.coordinator)
        with session_scope(self.engine) as s:targets=authorized_targets(s,task_id)
        for child in result.get('deliveries',[]):child['accepted']=child['id'] in targets
        return result
    def interventions(self):
        items=self.inspections.interventions()
        for platform in self.catalog._transports:
            getter=getattr(self.catalog.transport_for(platform),'interventions',None)
            if getter:
                items.extend({**item, 'content_type':self.catalog._route(platform)[1]} for item in getter())
        return items
    def intervention_status(self,delivery_id=None,account_id=None):
        items=self.interventions()
        # A targeted read opens only that original request in the UI. It does
        # not clear its deferred choice or resume any publishing work.
        visible=([r for r in items if r.get('account_id')==account_id] if account_id is not None else
                 [r for r in items if r.get('delivery_id')==delivery_id] if delivery_id is not None
                 else [r for r in items if r['id'] not in self.deferred])
        return {'request':visible[0] if visible else None,'pending':len(items)}
    def respond(self,p):
        match=next((r for r in self.interventions() if r['id']==p['id'] and r.get('delivery_id')==p['delivery_id']),None)
        if match is None:raise ValueError('verification request expired')
        view_action=p['action'] in {'visible','refresh','scroll','pointer','code','resend_code'}
        if not match.get('final_started') and p['action'] not in {'cancel','visible','refresh','scroll','resend_code'}:
            from .library_queries import InvalidRequest
            if p.get('consent_same_identity') is not True:raise InvalidRequest()
        route=match['platform'] if match.get('content_type','video')=='video' else match['platform']+':'+match['content_type']
        adapter=self.catalog.transport_for(route)
        result=adapter.respond(p['id'],p['action'],p.get('code',''))
        self.deferred.discard(p['id'])
        if view_action:
            self.enqueue('verification_view',(route,p['delivery_id'],p['id'],result['view_revision']))
        else:self.enqueue('reconcile' if match.get('final_started') else 'resume',p['delivery_id'])
        return result
