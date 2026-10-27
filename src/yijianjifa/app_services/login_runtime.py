"""Native-configured platform login providers, never supplied by the page.

Provider(account_id, cancel_event) must own an isolated authentication window,
verify identity, close its resources before returning, and return either None
(cancel/window closed) or verified identity/nickname/session_blob. A real host
must obtain current-run authorization before configuring a live provider.
"""
import threading
import time
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from yijianjifa.features.accounts.login_flow import LoginFlow
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.features.accounts.service import AccountService, AccountIdentityMismatch
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from yijianjifa.features.publish.account_wait import AccountBusy
from yijianjifa.webui.session_check_diagnostics import sanitize_diagnostic


class LoginRuntime:
    def __init__(self,engine,paths,provider=None,*,providers=None):
        if providers is not None and not isinstance(providers,Mapping):raise ValueError('invalid login providers')
        self._providers={}
        for platform,configured in (providers or {}).items():
            PlatformCatalog().get(platform)
            if not callable(configured):raise ValueError('invalid login provider')
            self._providers[platform]=configured
        if callable(provider):
            if 'douyin' in self._providers and self._providers['douyin'] is not provider:raise ValueError('duplicate login provider')
            self._providers['douyin']=provider
        self.engine=engine;self.paths=paths;self.provider=self._providers.get('douyin',provider)
        self.flow=None;self.thread=None;self._cancel=None
        self._lock=threading.RLock();self._receipts={};self._closed=False
        self._session_checks={}
        self._check_started={}
        self._background_checks={}
        self._check_condition=threading.Condition(self._lock)
        self._manual_checks={};self._manual_thread=None;self._manual_active=None

    def check_activity(self,account_id):
        with self._lock:
            started=self._check_started.get(account_id)
            job=self._manual_checks.get(self._manual_active)
            if (started is None and job and job['state']=='running'
                    and account_id in job['account_ids'][job['completed']:]):
                started=job['started_at']
            return {'state':'checking','checked_at':started} if started else None

    @staticmethod
    def _check_job_status(job):
        return {key:job[key] for key in ('id','state','completed','total')}

    def start_checks(self,request_id,account_ids):
        if (type(account_ids) is not list or not 1<=len(account_ids)<=200
                or any(type(a) is not str or not 1<=len(a)<=64 for a in account_ids)
                or len(set(account_ids))!=len(account_ids)):
            raise ValueError('invalid selected accounts')
        with self._lock:
            if self._closed:raise ValueError('application closing')
            previous=next((job for job in self._manual_checks.values() if job['request_id']==request_id),None)
            if previous:
                if previous['account_ids']!=account_ids:raise ValueError('check request changed')
                return self._check_job_status(previous)
            if self._manual_thread and self._manual_thread.is_alive():raise ValueError('account check running')
            service=AccountService(self.engine)
            for account_id in account_ids:
                account=service.get(account_id)
                if not callable(getattr(self._providers.get(account.platform),'check_session',None)):
                    raise ValueError('account check unavailable')
            job=dict(id=secrets.token_hex(24),request_id=request_id,account_ids=list(account_ids),state='running',
                     started_at=datetime.now(timezone.utc).isoformat(),completed=0,total=len(account_ids),cancel=threading.Event())
            self._manual_checks[job['id']]=job;self._manual_active=job['id']
            self._manual_thread=threading.Thread(target=self._run_checks,args=(job,),name='p011-account-check',daemon=False)
            self._manual_thread.start()
            return self._check_job_status(job)

    def _run_checks(self,job):
        try:
            for account_id in job['account_ids']:
                if job['cancel'].is_set() or self._closed:break
                try:self.check_session(account_id,job['cancel'])
                except Exception:pass  # Other saved accounts remain independently checkable.
                with self._lock:
                    if not job['cancel'].is_set():job['completed']+=1
        finally:
            with self._lock:
                job['state']='cancelled' if job['cancel'].is_set() or self._closed else 'completed'
                if self._manual_active==job['id']:self._manual_active=None

    def checks_status(self,job_id):
        with self._lock:return self._check_job_status(self._manual_checks[job_id])

    def cancel_checks(self,job_id):
        with self._lock:
            job=self._manual_checks[job_id]
            if job['state']=='running':job['cancel'].set()
            return self._check_job_status(job)

    def available(self,platform=None):
        return not self._closed and (bool(self._providers) if platform is None else platform in self._providers)

    def platforms(self):
        return [item['platform'] for item in PlatformCatalog().list() if self.available(item['platform'])]

    def resolve_platform(self,account_id,platform=None):
        if account_id is not None:
            if type(account_id) is not str or not 1<=len(account_id)<=64:raise ValueError('invalid account')
            account=AccountService(self.engine).get(account_id)
            if platform is not None and platform!=account.platform:raise ValueError('account platform mismatch')
            platform=account.platform
        elif platform is None:platform='douyin'
        PlatformCatalog().get(platform)
        return platform

    def start(self,request_id,account_id,*,platform=None,backend=False,probe=False,upload_consent=False):
        platform=self.resolve_platform(account_id,platform)
        provider=self._providers.get(platform)
        if probe:
            if platform!='douyin' or not account_id or upload_consent is not True or not callable(getattr(provider,"check_capabilities",None)):raise ValueError("capability provider or consent unavailable")
            from yijianjifa.features.accounts.capability_probe import fixture_path
            fixture_path()
        if backend and (not account_id or not callable(getattr(provider,"open_backend",None))):raise ValueError("backend unavailable")
        with self._lock:
            if not self.available(platform):raise ValueError('login provider unavailable')
            if request_id in self._receipts:
                previous,jid=self._receipts[request_id]
                if previous!=(account_id,platform,backend,probe):raise ValueError('login request conflict')
                return self.flow.status(jid)
            if self.thread and self.thread.is_alive():raise ValueError('login in progress')
            waiting_check=None
            if account_id in self._session_checks:
                waiting_check=self._background_checks.get(account_id)
                if waiting_check is None:raise ValueError('account check in progress')
                # User login wins over the optional startup pass. Its worker
                # waits for the isolated check to close before reusing profile IO.
                waiting_check.set()
            if self.flow is None:self.flow=LoginFlow(self.engine,SessionStore(self.paths.data_dir/'profiles'))
            job=self.flow.begin(account_id,platform=platform);jid=job['id']
            while len(self._receipts)>=16:self._receipts.pop(next(iter(self._receipts)))
            self._receipts[request_id]=((account_id,platform,backend,probe),jid)
            self._cancel=threading.Event()
            self.thread=threading.Thread(target=self._run,args=(jid,account_id,self._cancel,provider,backend,probe,waiting_check),name='p011-login',daemon=False)
            self.thread.start()
            return job

    def _run(self,jid,account_id,cancel,provider,backend=False,probe=False,waiting_check=None):
        try:
            if waiting_check is not None:
                deadline=time.monotonic()+30
                with self._check_condition:
                    while self._session_checks.get(account_id) is waiting_check:
                        if cancel.is_set() or self._closed:
                            self.flow.window_closed(jid);return
                        remaining=deadline-time.monotonic()
                        if remaining<=0:raise AccountBusy()
                        self._check_condition.wait(min(.1,remaining))
            if cancel.is_set() or self._closed:
                self.flow.window_closed(jid);return
            if probe:
                from yijianjifa.features.accounts.capability_probe import fixture_path
                result=provider.check_capabilities(account_id,cancel,fixture_path())
                if result is None or cancel.is_set():self.flow.window_closed(jid)
                else:self.flow.complete_capabilities(jid,result)
                return
            result=(provider.open_backend if backend else provider)(account_id,cancel)
            if result is None or cancel.is_set():self.flow.window_closed(jid)
            elif type(result) is dict and {'platform_user_id','nickname','session_blob'}<=set(result) and not set(result)-{'platform_user_id','nickname','session_blob','avatar_data','ordinary_available'}:
                self.flow.complete(jid,**result,preserve_capabilities=backend)
            else:self.flow.provider_failed(jid)
        except AccountIdentityMismatch:self.flow.provider_failed(jid,identity_mismatch=True)
        except AccountBusy:self.flow.provider_failed(jid,account_busy=True)
        except Exception:self.flow.provider_failed(jid)

    def status(self,jid):
        if not self.flow:raise ValueError('login attempt unavailable')
        return self.flow.status(jid)

    def check_session(self,account_id,cancel_event,*,background=False):
        """Native-only synchronous IO; callers own their background worker.

        The live provider must finish its isolated browser before returning. A
        relogin's newer archive can never inherit an older check's badge update.
        """
        if type(account_id) is not str or not 1<=len(account_id)<=64:raise ValueError('invalid account')
        if type(background) is not bool:raise ValueError('invalid check priority')
        if not callable(getattr(cancel_event,'is_set',None)) or not callable(getattr(cancel_event,'set',None)):
            raise ValueError('invalid cancellation event')
        try:account=AccountService(self.engine).get(account_id)
        except KeyError:return {'state':'unavailable','reason':'account_unavailable'}
        original=(account.platform,account.platform_user_id,account.session_ref)
        with self._lock:
            if self._closed:return {'state':'unavailable','reason':'app_closing'}
            if cancel_event.is_set():return {'state':'unavailable','reason':'cancelled'}
            if account_id in self._session_checks:
                waiting_check=self._background_checks.get(account_id) if not background else None
                if waiting_check is None:return {'state':'unavailable','reason':'account_busy'}
                # Starting a real batch has the same priority as explicit
                # login: replace only this optional startup check, after its
                # browser has closed. Other foreground checks keep their lease.
                waiting_check.set()
                deadline=time.monotonic()+30
                while self._session_checks.get(account_id) is waiting_check:
                    if cancel_event.is_set():return {'state':'unavailable','reason':'cancelled'}
                    if self._closed:return {'state':'unavailable','reason':'app_closing'}
                    remaining=deadline-time.monotonic()
                    if remaining<=0:return {'state':'unavailable','reason':'account_busy'}
                    self._check_condition.wait(min(.1,remaining))
                if cancel_event.is_set():return {'state':'unavailable','reason':'cancelled'}
                if self._closed:return {'state':'unavailable','reason':'app_closing'}
                if account_id in self._session_checks:return {'state':'unavailable','reason':'account_busy'}
            if self.thread and self.thread.is_alive():return {'state':'unavailable','reason':'login_in_progress'}
            provider=self._providers.get(account.platform)
            checker=getattr(provider,'check_session',None)
            if not callable(checker):return {'state':'unavailable','reason':'provider_unavailable'}
            self._session_checks[account_id]=cancel_event
            self._check_started[account_id]=datetime.now(timezone.utc).isoformat()
            manual=self._manual_checks.get(self._manual_active)
            if background or manual and manual['cancel'] is cancel_event:
                # Optional manual checks yield to an explicit login or real
                # publish check just like startup IO; never cancel a publisher.
                self._background_checks[account_id]=cancel_event
        try:
            observed_at=datetime.now(timezone.utc)
            try:result=checker(account_id,cancel_event)
            except AccountIdentityMismatch:result={'state':'identity_mismatch'}
            except Exception:result={'state':'unavailable','reason':'network_unavailable'}
            states={'valid','login_required','identity_mismatch','unavailable'}
            reasons={'cancelled','account_busy','timeout','network_unavailable','verification_required',
                     'identity_unavailable','session_unavailable','cleanup_failed','platform_unavailable','session_changed',
                     'invalid_provider_result','login_in_progress','app_closing','account_unavailable','identity_mismatch',
                     'login_required','session_missing','provider_unavailable'}
            diagnostic=sanitize_diagnostic(result['diagnostic']) if type(result) is dict and 'diagnostic' in result else None
            if (type(result) is not dict or set(result)-{'state','platform_user_id','reason','diagnostic'}
                    or type(result.get('state')) is not str or result['state'] not in states
                    or ('reason' in result and (type(result['reason']) is not str or result['reason'] not in reasons))
                    or ('diagnostic' in result and diagnostic is None)):
                result={'state':'unavailable','reason':'invalid_provider_result'}
            else:
                uid=result.get('platform_user_id')
                if ('platform_user_id' in result and (type(uid) is not str or not 1<=len(uid)<=255
                        or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in uid))):
                    result={'state':'unavailable','reason':'invalid_provider_result'}
                elif result['state']=='valid' and uid is None:
                    result={'state':'unavailable','reason':'invalid_provider_result'}
                elif result['state']=='valid' and uid!=account.platform_user_id:
                    result={'state':'identity_mismatch','platform_user_id':uid}
                else:result=dict(result)
                if diagnostic is not None and result.get('reason')!='invalid_provider_result':
                    result['diagnostic']=diagnostic
            service=AccountService(self.engine)
            try:current=service.get(account_id)
            except KeyError:return {'state':'unavailable','reason':'account_unavailable'}
            if (current.platform,current.platform_user_id,current.session_ref)!=original:
                return {'state':'unavailable','reason':'session_changed'}
            with self._lock:
                if cancel_event.is_set():return {'state':'unavailable','reason':'cancelled'}
                if self._closed:return {'state':'unavailable','reason':'app_closing'}
                if result.get('reason') not in {'cancelled','account_busy','login_in_progress','app_closing',
                                               'session_changed','account_unavailable'}:
                    state='session_missing' if result.get('reason')=='session_missing' else result['state']
                    detail=result.get('diagnostic')
                    brief={key:detail[key] for key in ('stage','elapsed_ms')} if detail else None
                    reason=result.get('reason') or ('identity_unavailable' if state=='unavailable' else None)
                    if not service.observe_login_check(account_id,platform_user_id=account.platform_user_id,
                            session_ref=account.session_ref,state=state,observed_at=observed_at,reason=reason,diagnostic=brief):
                        try:current=service.get(account_id)
                        except KeyError:return {'state':'unavailable','reason':'account_unavailable'}
                        if (current.platform,current.platform_user_id,current.session_ref)!=original:
                            return {'state':'unavailable','reason':'session_changed'}
                        latest=service.login_checks([current])[account_id]
                        if latest is None:return {'state':'unavailable','reason':'identity_unavailable'}
                        # A newer proof on this exact archive wins. It is not a
                        # changed login session, and an older valid read cannot
                        # overrule a newer explicit logout observation.
                        result={'state':latest['state']}
                        if latest['state']=='valid':result['platform_user_id']=current.platform_user_id
                        elif latest['state']=='login_required' and current.login_state=='pending_verification':
                            result['reason']='session_missing'
            return result
        finally:
            with self._check_condition:
                self._session_checks.pop(account_id,None)
                self._check_started.pop(account_id,None)
                self._background_checks.pop(account_id,None)
                self._check_condition.notify_all()

    def cancel(self,jid):
        with self._lock:
            result=self.status(jid)
            if result['state']=='waiting':
                self._cancel.set();result=self.flow.window_closed(jid)
            return result

    def close(self,timeout=0):
        deadline=time.monotonic()+max(0,timeout)
        with self._lock:
            self._closed=True
            if self._cancel:self._cancel.set()
            manual=self._manual_checks.get(self._manual_active)
            if manual:manual['cancel'].set()
            for event in self._session_checks.values():event.set()
            if self.flow:self.flow.close()
            thread=self.thread
            manual_thread=self._manual_thread
        if thread:thread.join(max(0,deadline-time.monotonic()))
        if manual_thread:manual_thread.join(max(0,deadline-time.monotonic()))
        with self._check_condition:
            self._check_condition.wait_for(lambda:not self._session_checks,timeout=max(0,deadline-time.monotonic()))
            closed=(thread is None or not thread.is_alive()) and (manual_thread is None or not manual_thread.is_alive()) and not self._session_checks
        # A checker that already reported cleanup_failed gets one bounded,
        # OS-only final retry. OS denial retains its private profile/account
        # lock, but must not turn normal client exit into an endless wait.
        cleanup={getattr(getattr(provider,'check_session',None),'close',None) for provider in self._providers.values()}
        for close in cleanup:
            if callable(close):
                try:close(max(0,deadline-time.monotonic()))
                except Exception:pass  # Best-effort read-only cleanup cannot trap client exit.
        return closed
