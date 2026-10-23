"""Finite identity-bound Chromium session lifecycle for publish adapters.

Platform adapters supply their own identity, DOM steps and session archive hooks.
Parked verification retains the original page and yields its worker; after the
final journal only result reconciliation may run. No platform IO is performed
by construction or readiness inspection.
"""
from dataclasses import dataclass,field
import base64
import hashlib
import json
import re
import secrets
import threading
import time
from urllib.parse import unquote, urlsplit

from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.features.publish.coordinator import AdapterOutcome,AdapterValidation
from yijianjifa.features.publish.deliveries import digest
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from .chromium_publish import account_lock
from .publish_intervention import InterventionBroker, intervention_reason
from .playwright_runtime import can_stop_exclusively


def fingerprint(account):return hashlib.sha256((account.session_ref or '').encode()).hexdigest()


VIEW_ACTIONS=frozenset({'visible','refresh','scroll','pointer','resend_code'})

# Read-only hit testing of the actual visible controls. A screenshot click is
# verification input, never a replacement for the host's final-submit journal.
VERIFICATION_POINTER=r'''args => {
 const visible=e=>{
   if(!e?.isConnected||!Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0))return false;
   for(let n=e;n;n=n.parentElement||n.getRootNode()?.host){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)<=0)return false;}
   return true;
 };
 const hit=point=>{let e=document.elementFromPoint(...point),next;while(e?.shadowRoot&&(next=e.shadowRoot.elementFromPoint(...point))&&next!==e)e=next;return e;};
 const parents=e=>{const result=[];for(let n=e;n;n=n.parentElement||n.getRootNode()?.host)result.push(n);return result;};
 const allowed=point=>{
   const e=hit(point);if(!visible(e))return false;
   const chain=parents(e),button=chain.find(n=>n.matches?.('button,[role="button"],a'));
   const label=(button?.innerText||button?.textContent||'').trim();
   const dialog=chain.find(n=>n.matches?.('.weui-desktop-dialog'));
   const originalConfirmation=args.allow_original_confirmation&&dialog&&visible(dialog)&&/管理员本人验证|需在手机上进行确认|直接发表/.test(dialog.innerText);
   if(/^(?:(?:立即|直接|定时|确认|继续|完成)\s*)?(?:发表|发布|投稿)|^(?:保存|存为|存入).*草稿/.test(label))return !!originalConfirmation;
   if(chain.some(n=>n.matches?.('.form-btns,.submit-add,.post-create .form-btns button')))return false;
   const declaration=chain.find(n=>n.matches?.('.declare-original-dialog'));
   if(args.kind==='complex'&&args.allow_original_declaration===true&&declaration&&visible(declaration)&&/原创权益|声明原创/.test(declaration.innerText))return true;
   const named=chain.some(n=>n.matches?.('[class*="captcha"],[id*="captcha"],[class*="verify"],[id*="verify"],[class*="qrcode"],[class*="qr-code"],[class*="qr-refresh"],[class*="login"],[class*="signin"]'));
   const frame=chain.find(n=>n.tagName==='IFRAME');
   if(frame&&/captcha|verify|qrcode|login/i.test([frame.id,frame.className,frame.src].join(' ')))return true;
   if(args.kind==='login')return named||/^(?:微信快捷登录|微信扫码登录|扫码登录|手机号登录|刷新二维码|重新获取二维码|使用其他账号登录|登录)$/.test(label);
   if(args.kind==='complex')return named||!!(dialog&&visible(dialog)&&/验证|验证码|扫码|直接发表/.test(dialog.innerText));
   return named||!!chain.find(n=>n.matches?.('input')&&/验证码/.test([n.placeholder,n.getAttribute('aria-label')].join(' ')));
 };
 return args.points.every(allowed);
}'''



@dataclass
class LiveSession:
    payload:dict
    binding:dict
    handle:str=field(default_factory=lambda:secrets.token_hex(24))
    broker:object=field(default_factory=InterventionBroker)
    request_id:str|None=None
    context:object=None
    page:object=None
    phase:str='baseline'
    uploaded:bool=False
    final:bool=False
    locked:bool=False
    original_ref:str|None=None
    version_grant:dict|None=None
    accepted_version:str|None=None
    resume_authorized:bool=False
    cancel_response:bool=False
    last_rows:object=None
    stable:int=0
    stage:str='正在核验账号'
    frame_refreshed_at:float=0.0
    inspection_done:bool=False
    cleanup_pending:bool=False
    cleanup_retry_at:float=0.0



class ChromiumPublishSessionAdapter:
    # Normal lifecycle contracts can be exercised without launching a browser.
    # Every real inspection clone still opts in explicitly in its constructor.
    inspection_only=False

    def __init__(self,engine,paths,*,cancel=None,expected_uid=None,step_timeout=30,inspection_only=False):
        if type(inspection_only) is not bool:raise ValueError('invalid inspection mode')
        self.engine,self.paths=engine,paths;self.cancel=cancel or threading.Event();self.expected_uid=expected_uid
        self.service=AccountService(engine);self.store=SessionStore(paths.data_dir/'profiles')
        self.driver=None;self.sessions={};self.bindings={};self.current_bindings={};self.refreshed={};self.thread_id=None
        self.step_timeout=step_timeout;self._lock=threading.RLock()
        self.inspection_only=inspection_only

    def account_available(self,target):
        # Retry only terminal or explicitly abandoned pages, on their original
        # worker. An unresolved parked page keeps its lock without user consent.
        if self.thread_id==threading.get_ident():
            now=time.monotonic()
            for s in list(self.sessions.values()):
                if s.payload['account_id']!=target['account_id'] or not s.cleanup_pending or now<s.cleanup_retry_at:continue
                s.cleanup_retry_at=now+1
                try:
                    self._release_page(s)
                    self._trace(s,'browser.close',{'closed':True})
                except Exception:pass  # Keep the lease until close is proven.
        return not account_lock(target['account_id']).locked()

    def create_inspection_adapter(self,cancel):
        """Isolate checks from normal delivery sessions and their cancellation."""
        return type(self)(self.engine,self.paths,cancel=cancel,expected_uid=self.expected_uid,
                          step_timeout=self.step_timeout,inspection_only=True)

    def _inspection_browser_options(self,options):
        # Service worker requests bypass routes. The host final marker must
        # govern every browser session, including an expanded verification page.
        return {**options,'service_workers':'block'}

    def _install_inspection_guard(self,context):
        """Reject publishing/saving even if a human clicks the full-page view.

        Platform-owned API writes fail closed unless they are upload, account
        verification, or a read operation. Unknown new endpoints require an
        adapter update; normal publishing has no such routing policy. This
        does not claim that a platform cannot create an automatic upload draft.
        """
        if not self.inspection_only:return
        self._install_write_guard(context)

    def _install_publish_guard(self,s):
        if self.inspection_only:self._install_inspection_guard(s.context)
        else:self._install_write_guard(s.context,allow_final=lambda:s.final)

    def _install_write_guard(self,context,allow_final=None):
        roots={'channels':('weixin.qq.com',),'bilibili':('bilibili.com',),
               'kuaishou':('kuaishou.com',),'douyin':('douyin.com',)}[self.platform_id]

        def guard(route):
            if allow_final and allow_final():route.fallback();return
            request=route.request
            parsed=urlsplit(request.url)
            host=(parsed.hostname or '').casefold()
            if not any(host==root or host.endswith('.'+root) for root in roots):
                route.fallback();return
            path=unquote(parsed.path).casefold().rstrip('/')
            # These are API operations, rather than SPA routes such as
            # /platform/post/create, which must remain navigable for a check.
            api=path.startswith(('/cgi-bin/','/x/','/rest/','/api/','/web/api/'))
            parts=set(re.split(r'[/_-]+',path))
            method=request.method.upper()
            transfer=('upload' in parts or path=='/x/vu/web/cover/up')
            final=({'channels':path.endswith('/post/post_create'),
                'bilibili':bool(re.fullmatch(r'/x/vu/web/(?:add|edit)(?:/v\d+)?',path)),
                'kuaishou':path.endswith('/video/pc/submit'),
                'douyin':path=='/web/api/media/aweme/create'}[self.platform_id])
            save_draft=api and not transfer and 'draft' in parts and bool(parts & {'save','add','edit','create','update','delete','remove'})
            if final or save_draft:
                route.abort('blockedbyclient');return
            # Ordinary publishing only adds the pre-journal final-write guard.
            # The stricter unknown-write policy belongs to inspection alone.
            if allow_final is not None:route.fallback();return
            if method in {'GET','HEAD','OPTIONS'}:
                route.fallback();return
            # File transfer may use POST/PUT and completion calls. Cover upload
            # is a transfer, never archive submission. Account verification and
            # POST queries likewise must keep the original checking page alive.
            if not transfer and bool(parts & {'create','add','edit','submit','publish','save','update','delete','remove'}):
                route.abort('blockedbyclient');return
            verification=bool(parts & {'login','auth','captcha','verify','verification','sms','qrcode'})
            read=bool(parts & {'list','query','search','get','info','status','pre','config','options','detail','metadata'})
            if transfer or verification or read:route.fallback()
            else:route.abort('blockedbyclient')

        context.route('**/*',guard)

    def _inspection_complete(self,s):
        if not self.inspection_only or s.final:raise ValueError('invalid inspection boundary')
        self._identity(s)
        s.inspection_done=True;s.phase='inspection_complete';s.stage='检查完成，未发表'
        return AdapterOutcome('inspection_complete',context={'checks':['account','upload','fields','schedule']})

    def _thread(self):
        tid=threading.get_ident()
        if self.thread_id is None:self.thread_id=tid
        if self.thread_id!=tid:raise ValueError('Playwright requires its original host worker thread')

    def bind_delivery(self,**binding):
        from yijianjifa.features.publish.deliveries import PublishDeliveryRow
        self._thread()
        with session_scope(self.engine) as db:
            row=db.get(PublishDeliveryRow,binding['delivery_id'])
            if row is not None and row.request_digest!=binding['digest']:raise ValueError('trusted attempt binding changed')
            binding={**binding,'attempt':row.attempt if row is not None else 1}
        self.bindings[binding['digest']]=dict(binding)
        self.current_bindings[binding['delivery_id']]=dict(binding)

    def _key(self,payload):return digest({k:v for k,v in payload.items() if k!='receipt_reference'})

    def _session(self,payload,context=None):
        self._thread();key=self._key(payload)
        with self._lock:
            session=self.sessions.get(key)
            binding=self.bindings.get(key)
            if session and binding and session.binding!=binding:
                if context or session.final or session.page is not None:raise ValueError('previous live attempt cannot be replaced')
                self.sessions.pop(key);session=None
            if context:
                if type(context) is not dict or set(context)!={'handle'} or not session or context['handle']!=session.handle:raise ValueError('original verification page unavailable')
            if not session:
                binding=self.bindings.get(key)
                if not binding:
                    # Reopened queries bind only through immutable DB authority.
                    from sqlalchemy import select
                    from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow
                    with session_scope(self.engine) as s:
                        row=s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.request_digest==key))
                        if row is None:raise ValueError('missing trusted target binding')
                        attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id,DeliveryAttemptRow.number==row.attempt))
                        binding={'digest':key,'delivery_id':row.id,'task_id':row.task_id,'attempt':row.attempt}
                        final=bool(attempt.final_json)
                else:final=False
                session=LiveSession(dict(payload),dict(binding));session.final=final
                if final:session.phase='result'
                self.sessions[key]=session
            return session

    def _version(self,s,*,query=False):
        a=self._account(s);captured=s.payload.get('session_fingerprint')
        current=fingerprint(a)
        if query:return True
        if not captured:return False
        accepted=s.accepted_version or captured
        scope={k:s.binding[k] for k in ('delivery_id','attempt','digest')}
        if current==accepted:
            if s.version_grant=={**scope,'version':current}:s.version_grant=None
            return True
        if s.version_grant!={**scope,'version':current}:return False
        with session_scope(self.engine) as db:
            key='multi_publish.session_audit:'+s.binding['delivery_id']+':'+str(s.binding['attempt'])+':'+current
            if db.get(Setting,key) is None:db.add(Setting(key=key,value_json=json.dumps({**scope,'old_version':accepted,'new_version':current,'same_identity_authorized':True})))
        s.accepted_version=current;s.version_grant=None
        # Explicit same-UID authorization preserves the current live page. It
        # never recreates/reuploads an in-progress form to import new cookies.
        if s.page is None:s.original_ref=a.session_ref
        return True

    def _park(self,s,kind='sms',message=None,*,reason=None):
        reason=intervention_reason(kind,reason)
        request=s.broker.status()['request']
        if request and (request['expires_at']<time.time() or request['kind']!=kind or request.get('reason')!=reason):
            s.broker.finish(s.request_id);s.request_id=None
        if s.request_id is None:
            a=self._account(s)
            s.request_id=s.broker.request(a.id,a.nickname,kind,message or ('提交已发生，请完成验证后核对原结果。' if s.final else '请完成所选账号验证，再明确继续此目标。'),reason=reason)
        if kind in {'complex','login'}:self._refresh_intervention_frame(s,force=True)
        s.stage={'identity_unavailable':'等待重新核对所选账号','identity_mismatch':'等待切回所选账号','session_changed':'等待确认更新后的账号'}.get(reason,'等待账号验证')
        return AdapterOutcome('result_unknown' if s.final else 'paused_auth',context={'handle':s.handle},error_code='verification_required')

    def _refresh_intervention_frame(self,s,*,force=False):
        request=s.broker.status()['request']
        if (s.page is None or not s.request_id or request is None
                or (request['kind'] not in {'complex','login'} and request.get('view_mode')!='page')
                or request['expires_at']<time.time()):return False
        now=time.monotonic()
        if not force and now-s.frame_refreshed_at<2:return False
        s.frame_refreshed_at=now
        try:
            image=s.page.screenshot(timeout=2000)
            s.broker.frame(s.request_id,base64.b64encode(image).decode('ascii'))
            return True
        except Exception:
            # Keep the original parked page and previous frame during a transient
            # rendering failure; status APIs never touch the browser thread.
            return False

    def refresh_interventions(self):
        if self.thread_id is None or self.cancel.is_set():return False
        self._thread()
        refreshed=False
        with self._lock:
            for s in self.sessions.values():
                if s.cleanup_pending:
                    # A user may delete a task without starting another one.
                    # Retry failed disposal on this owner instead of leaving
                    # its account locked until another publishing job arrives.
                    self.account_available({'account_id':s.payload['account_id']})
                refreshed=self._refresh_intervention_frame(s) or refreshed
        return refreshed

    def validate_target(self,target,payload,execution):
        s=self._session(payload)
        try:result=self._validate_session(s,payload,execution)
        except Exception:
            if not s.final:self._release_page(s)
            raise
        if isinstance(result,AdapterValidation) and not result.authorized and not s.final:self._release_page(s)
        return result

    def submit(self,payload,*,context,before_final):
        s=self._session(payload,context)
        if self.inspection_only and s.inspection_done:raise ValueError('inspection already complete')
        if s.final:raise ValueError('final already attempted; only query is allowed')
        return self._step(s,before_final=before_final)

    def resume(self,payload,*,context,before_final):
        s=self._session(payload,context)
        if self.inspection_only and s.inspection_done:raise ValueError('inspection already complete')
        if s.final:raise ValueError('post-final verification must query')
        if not s.resume_authorized:raise ValueError('explicit same-identity resume required')
        s.resume_authorized=False
        if s.cancel_response:
            self._release_page(s);raise ValueError('verification cancelled')
        if not self._version(s):return self._park(s,'login','所选账号登录资料已更新，请确认仍使用原账号继续。',reason='session_changed')
        self._open(s);self._consume(s)
        return self._step(s,before_final=before_final)

    def reconcile(self,payload,*,context):
        if self.inspection_only:raise ValueError('inspection cannot query submission')
        s=self._session(payload,context if context and any(v.handle==context.get('handle') for v in self.sessions.values()) else None)
        # A missing final marker cannot establish absence of an external side
        # effect. Keep interrupted/protocol-unknown attempts blocked.
        if not s.final:return AdapterOutcome('result_unknown',error_code='recovery_unavailable')
        self._open(s,query=True);self._consume(s)
        s.phase='result'
        self._prepare_result_query(s)
        return self._step(s,query=True)

    def _forget_page(self,s):
        s.context=None;s.page=None;s.cleanup_pending=False
        if s.locked:account_lock(s.payload['account_id']).release();s.locked=False

    def _release_page(self,s):
        try:
            if s.context:s.context.close()
            self._forget_page(s)
        finally:
            # A failed close retains the live context AND its profile lock.
            # Only successful close or exclusive SDK termination proves cleanup.
            if s.request_id:s.broker.finish(s.request_id);s.request_id=None
            s.version_grant=None;s.resume_authorized=False
            s.cancel_response=False

    def _trace(self,s,step,checks=None):
        from yijianjifa.features.publish.execution_log import append_event
        binding=getattr(s,'binding',{})
        if not binding.get('delivery_id'):return
        append_event(self.engine,binding.get('delivery_id'),binding.get('attempt',1),step,checks)

    def _step(self,s,**kwargs):
        self._trace(s,'execution.start',{'query':bool(kwargs.get('query'))})
        started=time.monotonic()
        before=kwargs.get('before_final')
        if before:
            def logged_final():
                self._trace(s,'submission.final_check')
                before()
                self._trace(s,'submission.authorized')
            kwargs['before_final']=logged_final
        try:result=self._advance(s,**kwargs)
        except Exception as error:
            self._trace(s,'execution.error',{'exception_type':type(error).__name__,'stage':getattr(self,'_preparation_stage','unknown'),'elapsed_ms':int((time.monotonic()-started)*1000)})
            if not s.final:self._release_page(s)
            raise
        self._trace(s,'execution.result',{'state':result.state,'error_code':result.error_code,'elapsed_ms':int((time.monotonic()-started)*1000)})
        if result.state not in {'paused_auth','result_unknown'}:
            # Cleanup is separate from the observed delivery outcome. In
            # particular, a browser close error cannot undo a submit receipt.
            try:
                self._release_page(s)
                self._trace(s,'browser.close',{'closed':True})
            except Exception as error:
                s.cleanup_pending=True;s.cleanup_retry_at=time.monotonic()+1
                self._trace(s,'browser.close',{'closed':False,'exception_type':type(error).__name__})
                if self.inspection_only:raise ValueError('inspection browser close failed') from error
                diagnostic=getattr(self,'_failure_diagnostic',None)
                if diagnostic:
                    try:diagnostic(s,error)
                    except Exception:pass
        return result

    def _consume(self,s,*,view_only=False,view_revision=None):
        if not s.request_id:return
        response=s.broker.consume(s.request_id,view_revision=view_revision)
        if not response:return
        action,code=response
        if view_only and action not in VIEW_ACTIONS|{'code'}:raise ValueError('无效的页面查看操作')
        if action=='cancel':
            s.broker.finish(s.request_id);s.request_id=None
            raise ValueError('verification cancelled')
        if action=='visible':
            # Render the original live browser page inside the client. A new
            # headed context would lose the upload and in-flight submission.
            s.broker.open_view(s.request_id)
            if not self._refresh_intervention_frame(s,force=True):raise ValueError('原验证页面画面读取失败，请点击更新画面重试。')
            request=s.broker.status()['request'] or {}
            if request.get('reason')=='identity_unavailable':
                message='已展开原任务页面。程序尚未确认所选账号；请核对页面账号，再点击“重新核对原账号”。'
            elif request.get('reason')=='identity_mismatch':
                message='已展开原任务页面。当前账号与所选账号不一致；请切回原账号后重新核对，不会使用其他账号发布。'
            elif request.get('reason')=='session_changed':
                message='已展开原任务页面。登录资料已更新；确认页面仍为原账号后，再继续原任务。'
            elif s.final:
                message='已展开原任务页面。完成所需验证后，点击“核对原提交结果”；不会再次发布。'
            else:
                message='已展开原任务页面。请按平台提示完成登录或验证码，再继续原任务。'
            s.broker.message(s.request_id,message)
        elif action=='refresh':
            # Refresh only the image, never reload the creator's form.
            if not self._refresh_intervention_frame(s,force=True):raise ValueError('原验证页面画面读取失败，请点击更新画面重试。')
        elif action=='scroll':
            s.page.mouse.wheel(*code)
            if not self._refresh_intervention_frame(s,force=True):raise ValueError('原验证页面画面读取失败，请点击更新画面重试。')
        elif action=='resend_code':
            try:self._request_verification_sms(s)
            except Exception as error:
                from .douyin_sms import safe_error
                s.broker.sms_feedback(s.request_id,'failed',safe_error(error,'未能获取新的验证码，请等待倒计时后重试或查看原页面。'))
            self._refresh_intervention_frame(s,force=True)
        elif action=='code':
            s.broker.code_feedback(s.request_id,'processing','正在填写原平台验证码并提交验证。')
            try:
                self._submit_verification_code(s,code)
                s.broker.code_feedback(s.request_id,'submitted','验证码已填入并提交原平台，正在等待验证结果。')
            except Exception as error:
                from .douyin_sms import safe_error
                s.broker.code_feedback(s.request_id,'failed',safe_error(error,'验证码未通过或未能提交，请查看原页面提示后重新输入或获取。'))
            finally:code=''
            self._refresh_intervention_frame(s,force=True)
        elif action=='pointer':
            kind=self._challenge(s)
            points=[code[:2],code[2:]] if len(code)==4 else [code]
            if kind=='sms' and self._sms_request_pointer(s,points):
                self._refresh_intervention_frame(s,force=True)
                return
            allowed=kind and s.page.evaluate(VERIFICATION_POINTER,{'points':points,'kind':kind,
                'allow_original_confirmation':s.final and not self.inspection_only and self.platform_id=='channels',
                'allow_original_declaration':not s.final and self.platform_id=='channels' and s.payload.get('fields',{}).get('channels_original') is True})
            if not allowed:raise ValueError('请只在登录或验证码区域操作；验证完成后点击“已完成验证”。')
            if len(code)==2:s.page.mouse.click(*code)
            else:
                s.page.mouse.move(*code[:2]);s.page.mouse.down();s.page.mouse.move(*code[2:],steps=20);s.page.mouse.up()
            if not self._refresh_intervention_frame(s,force=True):raise ValueError('原验证页面画面读取失败，请点击更新画面重试。')
        if not view_only and not self._after_intervention_challenge(s):s.broker.finish(s.request_id);s.request_id=None

    def _after_intervention_challenge(self,s):
        """Observe verification completion without replaying its UI action."""
        return self._challenge(s)

    def _submit_verification_code(self,s,code):
        surface=self._verification_surface(s)
        inputs=surface.locator('input[placeholder*="验证码"]:visible, input[aria-label*="验证码"]:visible')
        if inputs.count()!=1:raise ValueError('verification input unavailable')
        inputs.fill(code)
        buttons=surface.get_by_role('button',name='确认',exact=True)
        if buttons.count()!=1:buttons=surface.get_by_role('button',name='验证',exact=True)
        if buttons.count()!=1:raise ValueError('verification button unavailable')
        buttons.click()

    def _request_verification_sms(self,s):
        raise ValueError('当前平台暂不支持再次获取验证码')

    def _sms_request_pointer(self,s,points):return False

    def process_verification_view(self,delivery_id,request_id,view_revision=None):
        """Worker-only viewing of the existing page; no identity grant/advance."""
        self._thread()
        with self._lock:
            s=next((s for s in self.sessions.values() if s.binding['delivery_id']==delivery_id and s.request_id==request_id),None)
            if s is None:raise ValueError('验证请求已结束，请重新查看任务状态。')
            request=s.broker.status()['request']
            if not request:raise ValueError('验证请求已结束，请重新查看任务状态。')
            code_action=request.get('view_action')=='code'
            revision=request.get('view_revision') if view_revision is None else view_revision
            if request.get('view_revision')!=revision:raise ValueError('页面操作已更新，请查看最新验证画面。')
            try:
                if s.page is None or s.page.is_closed():
                    s.broker.consume(request_id,view_revision=revision)
                    raise ValueError('原验证页面已关闭，请重新检查账号登录状态。')
                self._consume(s,view_only=True,view_revision=revision)
            except Exception as error:
                safe=str(error) if str(error) in {'原验证页面已关闭，请重新检查账号登录状态。',
                    '原验证页面画面读取失败，请点击更新画面重试。',
                    '请只在登录或验证码区域操作；验证完成后点击“已完成验证”。'} else '无法更新原验证页面，请重新操作或查看任务状态。'
                s.broker.view_result(request_id,revision,error=safe)
                if code_action:s.broker.code_feedback(request_id,'failed',safe)
                return {'accepted':False}
            s.broker.view_result(request_id,revision)
            if code_action:
                request=s.broker.status()['request'] or {}
                deadline=time.monotonic()+3
                kind=self._after_intervention_challenge(s)
                while request.get('code_state')!='failed' and kind=='sms' and time.monotonic()<deadline and not self.cancel.is_set():
                    s.page.wait_for_timeout(150)
                    if self._verification_rejected(s):
                        s.broker.code_feedback(request_id,'failed','平台提示验证码错误或已失效，请重新输入或获取。')
                    kind=self._after_intervention_challenge(s)
                    request=s.broker.status()['request'] or {}
                complete=request.get('code_state')!='failed' and not kind
                if complete:
                    s.broker.finish(request_id);s.request_id=None
                elif kind and kind!=request.get('kind'):
                    self._park(s,kind)
                return {'accepted':True,'verification_complete':bool(complete),'final_started':s.final}
            return {'accepted':True}

    def _verification_rejected(self,s):
        if self.platform_id!='douyin':return False
        from .douyin_sms import rejection
        return rejection(self._verification_surface(s))

    def _verification_surface(self,s):
        """Resolve text verification on the original live page or trusted frame."""
        return s.page

    def authorize_resume(self,delivery_id):
        with self._lock:
            matched=[s for s in self.sessions.values() if s.binding==self.current_bindings.get(delivery_id) and not s.final]
            if not matched:raise ValueError('original live session unavailable')
            for s in matched:self._grant_resume(s)

    def _grant_resume(self,s):
        s.version_grant={**{k:s.binding[k] for k in ('delivery_id','attempt','digest')},'version':fingerprint(self._account(s))}
        s.resume_authorized=True

    def interventions(self):
        with self._lock:
            result=[]
            for s in self.sessions.values():
                request=s.broker.status()['request']
                if request:result.append({**request,'delivery_id':s.binding['delivery_id'],'platform':self.platform_id,'final_started':s.final,'external_browser':getattr(s,'external_browser',False) is True})
            return result

    def respond(self,request_id,action,code=''):
        with self._lock:
            session=next((s for s in self.sessions.values() if s.request_id==request_id),None)
            if session is None:raise ValueError('verification request unavailable')
            # Response only enters the broker memory, never DB/session audit.
            result=session.broker.respond(request_id,action,code)
            if not session.final and action not in VIEW_ACTIONS:
                if action=='cancel':session.cancel_response=True;session.resume_authorized=True;session.version_grant=None
                else:self._grant_resume(session)
            return result

    def cancel_delivery_session(self,delivery_id):
        self._thread()
        with self._lock:
            for key,s in list(self.sessions.items()):
                if s.binding['delivery_id']!=delivery_id:continue
                if s.final:
                    # Disposal is distinct from cancellation of an external
                    # submission. Require the committed user cancellation and
                    # exact final attempt before closing this retained page;
                    # its immutable submission/history records stay untouched.
                    from sqlalchemy import select
                    from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow
                    with session_scope(self.engine) as db:
                        row=db.get(PublishDeliveryRow,delivery_id)
                        if (row is None or not row.cancel_requested
                                or row.attempt!=s.binding.get('attempt')
                                or row.request_digest!=s.binding.get('digest')
                                or row.task_id!=s.binding.get('task_id')
                                or row.account_id!=s.payload.get('account_id')):continue
                        attempt=db.scalar(select(DeliveryAttemptRow).where(
                            DeliveryAttemptRow.delivery_id==delivery_id,DeliveryAttemptRow.number==row.attempt))
                        if (attempt is None or not attempt.final_json
                                or attempt.request_digest!=s.binding.get('digest')):continue
                try:
                    self._release_page(s)
                    self._trace(s,'browser.close',{'closed':True})
                    # Cached trusted bindings may still reference this exact
                    # attempt. Retain its in-memory final boundary, without a
                    # page, request or lock, so it cannot become a fresh submit.
                    if not s.final:del self.sessions[key]
                except Exception as error:
                    s.cleanup_pending=True;s.cleanup_retry_at=time.monotonic()+1
                    self._trace(s,'browser.close',{'closed':False,'exception_type':type(error).__name__})
                    raise

    def _owned_sessions(self):
        """Subclasses include auxiliary option pages sharing this lease."""
        return list(self.sessions.values())

    def close(self):
        if self.thread_id is None:return
        self._thread();errors=[]
        for s in list(self.sessions.values()):
            if not self.inspection_only and s.context and s.payload['account_id'] in self.refreshed:
                value=self.refreshed[s.payload['account_id']]
                try:value['session_blob']=self._capture_session_blob(s.context,value['platform_user_id'])
                except Exception:value['needs_persist']=False
            try:self._release_page(s)
            except Exception as error:errors.append(error)
        owned=self._owned_sessions()
        live=any(s.context is not None for s in owned)
        # Releasing a shared lease cannot establish that a failed context close
        # succeeded. Keep this owner until its context closes or the SDK is last.
        if self.driver and (not live or can_stop_exclusively(self.driver)):
            exclusive=can_stop_exclusively(self.driver)
            try:
                self.driver.stop();self.driver=None
                if exclusive:
                    for s in owned:self._forget_page(s)
            except Exception as error:errors.append(error)
        self.sessions={key:s for key,s in self.sessions.items() if s.context is not None}
        if not self.inspection_only:
            try:self._persist_sessions()
            except Exception as error:errors.append(error)
        if errors:
            message='inspection browser close failed' if self.inspection_only else 'publish browser close failed'
            raise ValueError(message) from errors[0]

    def _close_inspection(self):
        # Retained as an internal compatibility entry point.
        self.close()

    def request_shutdown(self):self.cancel.set()

    def _prepare_result_query(self,s):
        """Select the platform's original-result page without upload or submit."""
        raise NotImplementedError
    def _capture_session_blob(self,context,uid):
        """Encode only this platform's identity-scoped session archive."""
        raise NotImplementedError
    def _persist_sessions(self):
        """Persist captured archives only if the accepted source is unchanged."""
        raise NotImplementedError
