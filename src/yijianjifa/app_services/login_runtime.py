"""Native-configured platform login providers, never supplied by the page.

Provider(account_id, cancel_event) must own an isolated authentication window,
verify identity, close its resources before returning, and return either None
(cancel/window closed) or verified identity/nickname/session_blob. A real host
must obtain current-run authorization before configuring a live provider.
"""
import threading
import time
from collections.abc import Mapping
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
        self._check_condition=threading.Condition(self._lock)

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
            if account_id in self._session_checks:raise ValueError('account check in progress')
            if self.flow is None:self.flow=LoginFlow(self.engine,SessionStore(self.paths.data_dir/'profiles'))
            job=self.flow.begin(account_id,platform=platform);jid=job['id']
            while len(self._receipts)>=16:self._receipts.pop(next(iter(self._receipts)))
            self._receipts[request_id]=((account_id,platform,backend,probe),jid)
            self._cancel=threading.Event()
            self.thread=threading.Thread(target=self._run,args=(jid,account_id,self._cancel,provider,backend,probe),name='p011-login',daemon=False)
            self.thread.start()
            return job

    def _run(self,jid,account_id,cancel,provider,backend=False,probe=False):
        try:
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

    def check_session(self,account_id,cancel_event):
        """Native-only synchronous IO; callers own their background worker.

        The live provider must finish its isolated browser before returning. A
        relogin's newer archive can never inherit an older check's badge update.
        """
        if type(account_id) is not str or not 1<=len(account_id)<=64:raise ValueError('invalid account')
        if not callable(getattr(cancel_event,'is_set',None)) or not callable(getattr(cancel_event,'set',None)):
            raise ValueError('invalid cancellation event')
        try:account=AccountService(self.engine).get(account_id)
        except KeyError:return {'state':'unavailable','reason':'account_unavailable'}
        original=(account.platform,account.platform_user_id,account.session_ref)
        with self._lock:
            if self._closed:return {'state':'unavailable','reason':'app_closing'}
            if cancel_event.is_set():return {'state':'unavailable','reason':'cancelled'}
            if account_id in self._session_checks:return {'state':'unavailable','reason':'account_busy'}
            if self.thread and self.thread.is_alive():return {'state':'unavailable','reason':'login_in_progress'}
            provider=self._providers.get(account.platform)
            checker=getattr(provider,'check_session',None)
            if not callable(checker):return {'state':'unavailable','reason':'provider_unavailable'}
            self._session_checks[account_id]=cancel_event
        try:
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
            from yijianjifa.features.accounts.service import AccountRow
            from yijianjifa.storage.db import session_scope
            with self.engine.connect() as connection:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    with session_scope(connection) as session:
                        current=session.get(AccountRow,account_id)
                        if current is None or current.login_state=='removed':result={'state':'unavailable','reason':'account_unavailable'}
                        elif (current.platform,current.platform_user_id,current.session_ref)!=original:
                            result={'state':'unavailable','reason':'session_changed'}
                        elif cancel_event.is_set():result={'state':'unavailable','reason':'cancelled'}
                        elif self._closed:result={'state':'unavailable','reason':'app_closing'}
                        elif result['state'] in {'valid','login_required'}:
                            current.login_state=('valid' if result['state']=='valid' else
                                                 'pending_verification' if result.get('reason')=='session_missing' else 'expired')
                    connection.commit()
                except BaseException:
                    connection.rollback();raise
            return result
        finally:
            with self._check_condition:
                self._session_checks.pop(account_id,None)
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
            for event in self._session_checks.values():event.set()
            if self.flow:self.flow.close()
            thread=self.thread
        if thread:thread.join(max(0,deadline-time.monotonic()))
        with self._check_condition:
            self._check_condition.wait_for(lambda:not self._session_checks,timeout=max(0,deadline-time.monotonic()))
            closed=(thread is None or not thread.is_alive()) and not self._session_checks
        # A checker that already reported cleanup_failed gets one bounded,
        # OS-only final retry. OS denial retains its private profile/account
        # lock, but must not turn normal client exit into an endless wait.
        cleanup={getattr(getattr(provider,'check_session',None),'close',None) for provider in self._providers.values()}
        for close in cleanup:
            if callable(close):
                try:close(max(0,deadline-time.monotonic()))
                except Exception:pass  # Best-effort read-only cleanup cannot trap client exit.
        return closed
