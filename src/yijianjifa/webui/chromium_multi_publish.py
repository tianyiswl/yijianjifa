"""Finite V2 Douyin DOM steps on the shared original-session lifecycle.

No verification waits, daily-browser profiles, scripts/URLs from the page, or
re-upload in resume/query. The V1 Chromium factory remains independent.
"""
from yijianjifa.features.publish.account_wait import AccountBusy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import time

from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome,AdapterValidation,failure_category
from yijianjifa.features.publish.read_diagnostics import (
    IdentityReadFailure, IdentityReadTimeout, error_details, identity_failure_details,
)
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from . import chromium_publish as legacy
from .chromium_publish_session import ChromiumPublishSessionAdapter,LiveSession,fingerprint
from .sms_cooldown import SMSCooldownActive
from .login_observation import douyin_login_state
from . import submit_feedback


# Preserve the original search/read behavior and add only finite DOM gates.
# The old result reader returns early while the search control hydrates; without
# these gates its ordinary 30-second readiness timeout has no usable evidence.
READ_RESULT_WITH_DIAGNOSTICS = 'const observedResult=await(async()=>{' + legacy.READ_RESULT + r'''})();
const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
const search=Array.from(document.querySelectorAll('input[placeholder="搜索作品"]')).filter(visible);
const diagnostic={search_count:search.length,
 search_matches:search.length===1&&search[0].value===title_text,
 loading:!!document.body?.innerText.includes('加载中，请稍后')};
if(observedResult?.ready===true){
 // 只记录完全匹配的数量，不保存标题、正文、作品标识或页面内容。
 const norm=s=>s.replace(/\u200b/g,'').replace(/\s+/g,' ').trim();
 const expected=norm(title_text+' '+body_text);
 diagnostic.text_match_count=Math.min(10000,Array.from(document.querySelectorAll('body *'))
  .filter(e=>visible(e)&&norm(e.innerText||'')===expected).length);
}
return {...observedResult,diagnostic,posting_limit:window.p011PostingLimitWatch?.seen===true};
'''

# 只观察本次最终点击之后新增的明确平台提示，不读取文案作为拒绝证据。
POSTING_LIMIT_WATCH = r'''
window.p011PostingLimitWatch?.observer?.disconnect();
const limitNodes=()=>Array.from(document.querySelectorAll('[role="alert"],[class*="toast"]')).filter(e=>
 e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&
 /^(?:抱歉[，,]\s*)?今天投稿次数已达到上限[，,]\s*请明天再试[。！!]?$/u.test((e.innerText||'').trim()));
const previousLimitNodes=new Set(limitNodes());
const limitWatch={seen:false};
limitWatch.observer=new MutationObserver(()=>{
 if(limitNodes().some(e=>!previousLimitNodes.has(e))){limitWatch.seen=true;limitWatch.observer.disconnect();}
});
window.p011PostingLimitWatch=limitWatch;
limitWatch.observer.observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true});
'''
FINAL_CLICK_WITH_LIMIT_WATCH=legacy.FINAL_CLICK.replace('window.p011FinalClicked=true;',
    POSTING_LIMIT_WATCH+'\n('+submit_feedback.INSTALL+')();\nwindow.p011FinalClicked=true;')


NORMAL_READ_IDENTITY = r'''
if(location.origin!=='https://creator.douyin.com')return null;
const identityDeadline=performance.now()+30000,identityTimeout=Symbol(),identityFailure=Symbol();
const remaining=()=>{const ms=identityDeadline-performance.now();if(ms<=0)throw identityTimeout;return ms;};
async function read(path,endpoint,request_index){
 const started=performance.now();
 const budget=Math.floor(Math.min(20000,remaining()));
 if(budget<=0)throw identityTimeout;
 const requestDeadline=Math.min(identityDeadline,performance.now()+budget),signal=AbortSignal.timeout(budget);
 const check=()=>{if(signal.aborted||performance.now()>=requestDeadline)throw identityTimeout;remaining();};
 let read_operation='fetch',http_status,content_kind='missing';
 const fail=error_kind=>{
  const details={error_kind,endpoint,request_index,read_operation,
   request_elapsed_ms:Math.min(30000,Math.max(0,Math.floor(performance.now()-started))),content_kind};
  if(Number.isInteger(http_status)&&http_status>=100&&http_status<=599)details.http_status=http_status;
  throw {[identityFailure]:details};
 };
 try{
  const response=await fetch(path,{credentials:'same-origin',cache:'no-store',redirect:'error',signal});
  http_status=response.status;
  const type=response.headers?.get?.('content-type');
  if(typeof type==='string'&&type.trim()){
   const mime=type.split(';',1)[0].trim().toLowerCase();
   content_kind=mime==='application/json'||mime.endsWith('+json')?'json':mime==='text/html'?'html':'other';
  }
  check();if(!response.ok)fail('http_error');
  read_operation='json';
  const value=await response.json();check();return value;
 }catch(error){
  if(error&&Object.prototype.hasOwnProperty.call(error,identityFailure))throw error;
  if(error===identityTimeout||signal.aborted||performance.now()>=requestDeadline||performance.now()>=identityDeadline)fail('timeout');
  fail(read_operation==='json'?(error instanceof SyntaxError?'json_error':'body_read_error'):'fetch_error');
 }
}
let identity;
try{
 const a=await read('/aweme/v1/creator/pc/user/info/','pc_user_info_before',1);
 const b=await read('/aweme/v1/creator/user/info/','profile_user_info',2);
 const c=await read('/aweme/v1/creator/pc/user/info/','pc_user_info_after',3);
 identity={uid:a.uid,uid_after:c.uid,status:a.status_code,profile_status:b.status_code,after_status:c.status_code,nickname:b.user_profile?.nick_name};
 remaining();
}catch(error){
 if(error===identityTimeout)return {p011_identity_timeout:true};
 if(error&&Object.prototype.hasOwnProperty.call(error,identityFailure))return {p011_identity_failure:error[identityFailure]};
 throw error;
}
'''


PREPARE_FORM_WITH_DIAGNOSTICS = 'const observedForm=await(async()=>{' + legacy.PREPARE_FORM + r'''})();
try {
 const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none'&&!e.closest('[aria-hidden="true"],.semi-modal-content-animate-hide');
 const titles=Array.from(document.querySelectorAll('input[placeholder="填写作品标题，为作品获得更多流量"]')).filter(visible);
 const editors=Array.from(document.querySelectorAll('[contenteditable="true"]')).filter(visible);
 const expected=typeof topic_names==='undefined'?[]:topic_names;
 const actual=editors.length===1?Array.from(editors[0].querySelectorAll('[data-mention="#"]')).map(e=>e.textContent.trim().replace(/^#/,'')):[];
 const prefix=editors.length===1&&actual.length<=expected.length&&actual.every((name,i)=>name===expected[i]);
 const clean=s=>s.replace(/\u200b/g,'').replace(/\r\n/g,'\n').trim();
 const norm=s=>expected.length?clean(s).replace(/\s+/g,' '):clean(s);
 const modes=Array.from(document.querySelectorAll('input')).filter(e=>visible(e)&&['radio','checkbox'].includes(e.type)&&e.closest('label')?.innerText.trim()==='定时发布');
 const dates=Array.from(document.querySelectorAll('input[placeholder="日期和时间"]')).filter(visible);
 return {...observedForm,diagnostic:{title_matches:titles.length===1&&titles[0].value===title_text,
  body_matches:editors.length===1&&norm(editors[0].innerText)===norm(body_text),
  topics_confirmed:prefix?actual.length:-1,topic_order_matches:prefix,
  schedule_mode_matches:modes.length===1&&modes[0].checked,
  schedule_time_matches:dates.length===1&&dates[0].value===scheduled_time_text}};
} catch { return observedForm; }
'''


_FORM_GATES={
    '账号身份不符':'identity','等待视频编辑页':'editor_wait','上传内置测试片':'upload_entry',
    '核验标题和简介控件':'controls','关闭视频预览引导':'onboarding','填写标题':'title',
    '话题与安排不符':'topic_mismatch','简介已核对':'body','清空待替换简介':'body',
    '填写话题前的简介':'body','等待可编辑的话题光标':'topic_cursor','话题输入内容不符':'topic_mismatch',
    '简介与话题回读不符':'body_mismatch','填写简介':'body','未能确认声明为空':'declaration_empty',
    '声明选项不可用':'declaration_options','选择声明':'declaration','确认声明':'declaration',
    '打开声明':'declaration','发布时间控件待适配':'schedule_mode','设置发布时间方式':'schedule_mode',
    '日期控件待适配':'schedule_time','填写定时时间':'schedule_time','核对发布地点':'location',
    '展开发布地点':'location','等待地点范围':'location','切换国内地点':'location',
    '地点未找到或存在歧义，请刷新地点后重试':'location','页面存在未安排的发布地点':'location',
    '表单已核对，未发布':'ready','等待上传完成':'upload_wait',
}
_FORM_GATE_PREFIXES=(('定位话题光标：','topic_cursor'),('输入话题：','topic_input'),
    ('话题未能识别：','topic_candidate'),('重新搜索话题：','topic_candidate'),
    ('等待完全匹配的话题：','topic_candidate'),('选择话题：','topic_select'),
    ('搜索发布地点：','location'),('等待名称及地址完全匹配的地点：','location'),('选择发布地点：','location'))


class ChromiumMultiPublishAdapter(ChromiumPublishSessionAdapter):
    platform_id='douyin'
    preparation_timeout=180
    # Exact scheduling DOM remains independent of the shared session lifecycle.
    verified_capabilities=frozenset({'ordinary','platform_schedule','result_query'})

    public_field_schema={'title':{'type':'string','max_length':30},'body':{'type':'string','max_length':1000},
        'tags':{'type':'string'},'cover':{'kinds':['first_frame'],'custom_ready':False},
        'location':{'selection':'trusted_query_reference'},'declaration':{'selection':'trusted_policy_reference'}}

    def failure_diagnostic(self,error):
        value=getattr(self,'_preparation_failure',None);self._preparation_failure=None
        return dict(value) if value else {}

    def _observe(self,s,step,stage,operation,**checks):
        self._preparation_stage=stage
        context={'phase':s.phase if s.phase in {'baseline','prepare','result'} else 'unknown',
                 'final_started':bool(s.final),**checks}
        self._trace(s,'douyin.'+step,{**context,'state':'started'})
        started=time.monotonic()
        try:value=operation()
        except Exception as error:
            category=failure_category(error)
            self._preparation_failure={'error_stage':stage,'error_category':category}
            self._trace(s,'douyin.'+step,{**context,'state':'failed','error_category':category,
                                       **error_details(error),'log_schema':1,
                                       'elapsed_ms':max(0,int((time.monotonic()-started)*1000))})
            raise
        if step=='final_click':
            context['click_reported_attempted']=type(value) is dict and value.get('attempted') is True
        self._trace(s,'douyin.'+step,{**context,'state':'completed',
                                   'elapsed_ms':max(0,int((time.monotonic()-started)*1000))})
        return value

    def _navigate(self,s,path):
        destination='manage' if path=='/creator-micro/content/manage' else 'upload'
        return self._observe(s,'navigation','page_navigation',
            lambda:s.page.goto('https://'+legacy.HOST+path,wait_until='domcontentloaded'),destination=destination)

    def _read_identity(self,s,*,query=False):
        normal=not query and not s.final
        script=NORMAL_READ_IDENTITY if normal else legacy.READ_IDENTITY
        def read():
            value=legacy.evaluate_script(s.page,script+'return identity;',{})
            if normal and type(value) is dict and set(value)=={'p011_identity_failure'}:
                details=identity_failure_details(value['p011_identity_failure'])
                if details is None:
                    raise ValueError('invalid identity diagnostic')
                if details['error_kind']=='timeout':
                    raise IdentityReadTimeout(details)
                raise IdentityReadFailure(details)
            if normal and type(value) is dict and set(value)=={'p011_identity_timeout'} and value['p011_identity_timeout'] is True:
                raise TimeoutError('Douyin identity read timed out')
            return value
        return self._login_read(s,lambda:self._observe(s,'identity','account_observation',read),
                                classify=lambda value:douyin_login_state(value,s.payload['platform_user_id']))

    def _receipt_checks(self,s,raw,form):
        """回执门槛的只读摘要；不改变匹配判据、稳定计数或提交状态。"""
        rows=raw.get('matches',[])
        if type(rows) is not list or len(rows)>10000 or any(type(row) is not dict for row in rows):
            return {'receipt_gate':'invalid_rows'}
        matching=legacy.matching_schedule_rows(rows,form)
        review=matching[0].get('review') if len(matching)==1 else None
        review_state=('missing' if not matching else 'ambiguous' if len(matching)>1 else
                      {'定时发布中':'scheduled','审核中':'reviewing','已发布':'published'}.get(review,'other')
                      if type(review) is str else 'other')
        stable=min(2,s.stable+1 if rows==s.last_rows else 1) if raw.get('ready') is True else 0
        checks={'result_count':len(rows),'schedule_match_count':len(matching),
                'stable_reads':stable,'review_state':review_state}
        try:legacy.verified_identity(raw.get('identity'),expected_uid=s.payload['platform_user_id'])
        except (TypeError,ValueError,KeyError):gate='identity'
        else:
            gate=('page' if raw.get('path')!='/creator-micro/content/manage' else
                  'not_ready' if raw.get('ready') is not True else
                  'query' if raw.get('query')!=form['title_text'] else
                  ('no_exact_text' if type(raw.get('diagnostic')) is dict
                   and type(raw['diagnostic'].get('text_match_count')) is int
                   and raw['diagnostic']['text_match_count']==0 else 'no_result_rows') if not rows else
                  'no_schedule_match' if not matching else
                  'ambiguous_schedule' if len(matching)!=1 else
                  'review_unconfirmed' if review_state not in {'scheduled','reviewing'} else
                  'unstable' if stable<2 else 'confirmed')
        return {**checks,'receipt_gate':gate}

    def _readback(self,s,raw,*,form=None):
        if type(raw) is not dict:return
        path=raw.get('path')
        page_kind={'/creator-micro/content/manage':'manage',legacy.UPLOAD_PATH:'upload',
                   legacy.PUBLISH_PATH:'post'}.get(path,'other') if type(path) is str else 'other'
        checks={'ready':raw.get('ready') is True,'uploaded':bool(s.uploaded),'final_started':bool(s.final),
                'page_kind':page_kind}
        diagnostic=raw.get('diagnostic')
        diagnostic=diagnostic if type(diagnostic) is dict else {}
        for key in ('search_count','text_match_count'):
            value=diagnostic.get(key)
            if type(value) is int and 0<=value<=10000:checks[key]=value
        for key in ('search_matches','loading'):
            if type(diagnostic.get(key)) is bool:checks[key]=diagnostic[key]
        if s.phase=='result':
            try:checks.update(self._receipt_checks(s,raw,form or self._form(s.payload,query=True)))
            except (KeyError,TypeError,ValueError):
                # 可选诊断不能中断原有回读；未知结构不作为已接收的证据。
                checks['receipt_gate']='invalid_rows'
        if s.phase=='prepare':
            upload=raw.get('upload_complete')
            checks['upload_state']='complete' if upload is True else 'pending' if upload is False else 'unknown'
            if type(upload) is bool:checks['upload_complete']=upload
            stage=raw.get('stage')
            checks['form_gate']=(_FORM_GATES.get(stage) or next((gate for prefix,gate in _FORM_GATE_PREFIXES if stage.startswith(prefix)),'unknown')) if type(stage) is str else 'unknown'
            for key in ('title_count','editor_count','final_button_count'):
                if type(raw.get(key)) is int and 0<=raw[key]<=10000:checks[key]=raw[key]
            for key in ('title_matches','body_matches','topic_order_matches','schedule_mode_matches','schedule_time_matches'):
                if type(diagnostic.get(key)) is bool:checks[key]=diagnostic[key]
            count=diagnostic.get('topics_confirmed')
            if type(count) is int and 0<=count<=1000:checks['topics_confirmed']=count
        step={'baseline':'baseline_readback','prepare':'form_readback','result':'result_readback'}[s.phase]
        self._trace(s,'douyin.'+step,checks)

    def _account(self,s):
        a=self.service.get(s.payload['account_id']);uid=s.payload['platform_user_id']
        if a.platform!='douyin' or a.platform_user_id!=uid or not uid.isdecimal() or (self.expected_uid and self.expected_uid!=uid):raise ValueError('frozen account mismatch')
        return a

    def _open(self,s,*,query=False):
        if s.page is not None:return
        a=self._account(s)
        if not a.session_ref:raise ValueError('identity-bound session unavailable')
        lock=legacy.account_lock(a.id)
        if not s.locked:
            if not lock.acquire(blocking=False):raise AccountBusy()
            s.locked=True
        with session_scope(self.engine) as db:
            mode_row=db.get(Setting,'publish.browser_mode');mode=json.loads(mode_row.value_json) if mode_row else 'background'
        if getattr(s,'browser_login_visible',False):mode='visible'
        s.external_browser = mode == 'visible'
        options=self._inspection_browser_options(legacy.browser_options(mode))
        if self.driver is None:
            from .playwright_runtime import acquire_playwright
            self.driver=self._observe(s,'driver_start','page_open',acquire_playwright)
        profile=legacy.chromium_profile(self.store,a.platform_user_id)
        s.context=self._observe(s,'browser_launch','page_open',
            lambda:self.driver.chromium.launch_persistent_context(str(profile),**options))
        s.browser_login_context_headed=mode=='visible'
        self._install_publish_guard(s)
        s.context.set_default_timeout(15000)
        if not getattr(s,'browser_login_preserve_profile',False):
            self._observe(s,'session_restore','session_restore',
                lambda:legacy.restore_account_session(s.context,self.store,a,profile,a.platform_user_id))
        s.original_ref=a.session_ref
        s.page=s.context.pages[0] if s.context.pages else s.context.new_page()
        s.page.add_init_script(legacy.SUCCESS_OBSERVER.replace('new MutationObserver(check).observe(document.documentElement','new MutationObserver(check).observe(document'))
        self._navigate(s,'/creator-micro/content/manage')

    def _challenge(self,s):
        return self._login_challenge(s,lambda:legacy.challenge_kind(s.page.evaluate(legacy.CHALLENGE) or {}))

    @property
    def sms_cooldown(self):
        if not hasattr(self,'_sms_cooldown'):
            from .sms_cooldown import SMSCooldown
            self._sms_cooldown=SMSCooldown(self.engine)
        return self._sms_cooldown

    def _request_verification_sms(self,s):
        from . import douyin_sms
        from .sms_cooldown import SMSCooldownActive
        surface=self._verification_surface(s);account=self._account(s).id
        try:
            seconds=douyin_sms.countdown_seconds(surface)
            if seconds:
                self.sms_cooldown.observe(account,seconds)
                message='验证码已获取，请收到后输入并确认。'
            else:
                attempted=False
                def reserve():
                    nonlocal attempted
                    self.sms_cooldown.reserve(account);attempted=True
                try:douyin_sms.request_code(surface,before_send=reserve)
                finally:
                    # Normal clicks may wait for actionability. Start the full
                    # interval after that wait, including an uncertain timeout.
                    if attempted:self.sms_cooldown.observe(account,60)
                message='已请求短信验证码；收到后在此输入并确认。'
            s.sms_auto_due=False
            s.broker.sms_feedback(s.request_id,'sent',message)
        except SMSCooldownActive:
            s.broker.sms_feedback(s.request_id,'waiting','验证码获取需间隔60秒，倒计时结束后可再次获取。')
            raise

    def interventions(self):
        items=super().interventions()
        for item in items:
            if item['kind']=='sms':item['sms_retry_after']=self.sms_cooldown.remaining(item['account_id'])
        return items

    def respond(self,request_id,action,code=''):
        if action=='resend_code':
            request=next((r for r in self.interventions() if r['id']==request_id),None)
            if not request or request['kind']!='sms':raise ValueError('短信验证请求已结束')
            if request['sms_retry_after']:
                from .sms_cooldown import SMSCooldownActive
                raise SMSCooldownActive(request['sms_retry_after'])
        return super().respond(request_id,action,code)

    def _sms_request_pointer(self,s,points):
        from .douyin_sms import is_request_click
        if not is_request_click(self._verification_surface(s),points):return False
        try:self._request_verification_sms(s)
        except SMSCooldownActive:pass
        return True

    def refresh_interventions(self):
        refreshed=super().refresh_interventions()
        if self.thread_id is None or self.cancel.is_set():return refreshed
        with self._lock:
            for s in self.sessions.values():
                if not s.request_id or not getattr(s,'sms_auto_due',False):continue
                if self.sms_cooldown.remaining(self._account(s).id):continue
                s.sms_auto_due=False
                try:self._request_verification_sms(s)
                except Exception as error:
                    from .douyin_sms import safe_error
                    s.broker.sms_feedback(s.request_id,'failed',safe_error(error,'未能自动获取验证码，请点击再次获取或查看原页面。'))
        return refreshed

    def _wait_sms_before_submit(self,s):
        account=self._account(s).id
        if self.sms_cooldown.remaining(account):self._trace(s,'sms.cooldown',{'waiting':True})
        def cancelled():
            if self.cancel.is_set():return True
            from yijianjifa.features.publish.deliveries import PublishDeliveryRow
            with session_scope(self.engine) as db:
                row=db.get(PublishDeliveryRow,s.binding.get('delivery_id'))
                return row is not None and (row.cancel_requested or row.hidden)
        return self.sms_cooldown.wait(account,cancelled=cancelled,
            pause=lambda seconds:s.page.wait_for_timeout(seconds*1000),
            on_wait=lambda seconds:setattr(s,'stage',f'等待验证码间隔，还需{seconds}秒'))

    def _park(self,s,kind='sms',message=None,*,reason=None):
        result=super()._park(s,kind,message,reason=reason)
        if kind=='sms' and s.page is not None:
            from . import douyin_sms
            if not getattr(s,'sms_request_attempted',False):
                s.sms_request_attempted=True
                try:
                    self._request_verification_sms(s)
                except SMSCooldownActive:
                    s.sms_auto_due=True
                except Exception as error:
                    s.broker.sms_feedback(s.request_id,'failed',douyin_sms.safe_error(error,'未能自动获取短信验证码，可点击再次获取或查看原页面。'))
            if douyin_sms.rejection(self._verification_surface(s)):
                s.broker.code_feedback(s.request_id,'failed','平台提示验证码错误或已失效，请重新输入或获取。')
        return result

    def _submit_verification_code(self,s,code):
        from .douyin_sms import submit_code
        submit_code(self._verification_surface(s),code)

    def _after_intervention_challenge(self,s):
        kind=super()._after_intervention_challenge(s)
        if kind!='sms':s.sms_request_attempted=False;s.sms_auto_due=False
        return kind

    def _validate_session(self,s,payload,execution):
        self._preparation_failure=None
        if execution!='platform_schedule' or self.cancel.is_set():return AdapterValidation('',False)
        if not self._version(s):return self._park(s,'login','冻结后的账号会话已改变，请确认同一账号后明确继续。')
        self._open(s)
        kind=self._challenge(s)
        if kind:return self._park(s,kind)
        observed=self._read_identity(s)
        try:legacy.verified_identity(observed,expected_uid=payload['platform_user_id'])
        except ValueError:return AdapterValidation(str(observed.get('uid','')),False)
        if s.phase=='prepare' and s.uploaded and not s.final:self._form(s.payload)
        return AdapterValidation(str(observed['uid']),True)

    def _form(self,payload,*,query=False):
        fields=payload['fields'];time_text=None
        if payload['execution_mode']=='platform_schedule':
            time_text=datetime.fromisoformat(payload['scheduled_at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
        form=legacy.validate_form_request(expected_uid=payload['platform_user_id'],title=fields['title'],body=fields.get('body',''),tags=fields.get('tags',''),
            declaration=(fields.get('declaration') or {}).get('label',''),location=fields.get('location'),scheduled_time_text=None if query else time_text)
        if query:form['scheduled_time_text']=time_text or ''
        return form

    def plan_readiness(self,payload):
        # Host/schema inspection performs no browser or platform operations.
        try:
            account=self.service.get(payload['account_id'])
            if account.platform!='douyin' or account.login_state!='valid' or not account.session_ref:return 'account_verification_required'
            if payload['execution_mode']!='platform_schedule':return 'execution_mode_not_verified'
            when=datetime.fromisoformat(payload['scheduled_at'])
            if when.second or when.microsecond:return 'schedule_requires_whole_minute'
            self._form(payload)
        except (ValueError,KeyError,TypeError):return 'platform_fields_invalid'
        return None

    def _advance(self,s,*,before_final=None,query=False):
        self._preparation_failure=None
        self._thread();self._open(s,query=query);form=self._form(s.payload,query=query)
        normal_prepare=s.phase=='prepare' and not query and not s.final
        deadline=time.monotonic()+(self.preparation_timeout if normal_prepare else self.step_timeout)
        timeout_stage='submission_result' if s.final else 'page_preparation' if s.phase=='baseline' else 'form_preparation'
        while not self.cancel.is_set() and time.monotonic()<deadline:
            kind=self._challenge(s)
            if kind:return self._park(s,kind)
            if not self._version(s,query=query):return self._park(s,'login','冻结后的账号会话已改变，请明确确认同一账号后继续。')
            baseline_identity=s.phase=='baseline' and not query and not s.final
            if not baseline_identity:
                observed=self._read_identity(s,query=query)
                try:legacy.verified_identity(observed,expected_uid=s.payload['platform_user_id'])
                except ValueError:return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',error_code='account_mismatch')
                s.browser_login=False
            if not query and s.final:
                notice=submit_feedback.outcome(s,s.page,'douyin',self._key(s.payload))
                if notice and notice.state=='result_unknown':s.platform_message=notice.platform_message
                elif notice:
                    if notice.state=='platform_scheduled':
                        a=self._account(s)
                        self.refreshed[a.id]={'platform_user_id':a.platform_user_id,'nickname':a.nickname,
                            'session_blob':legacy.session_blob(s.context,a.platform_user_id),'original_ref':s.original_ref,
                            'login_observed_at':self._login_valid_at(s)}
                    return notice
            s.stage='正在核对结果' if s.phase=='result' else '正在核对已有安排' if s.phase=='baseline' else '正在上传并填写资料'
            if s.phase=='prepare' and s.page.url.endswith(legacy.UPLOAD_PATH) and not s.uploaded:
                path=Path(s.payload['video_path']);root=(self.paths.data_dir/'publish_resources').resolve()
                # Resource ownership is asserted by the coordinator. Standalone
                # adapters still bind exact frozen bytes before the file chooser.
                if not path.is_file() or sha256_file(path)!=s.payload['video_hash']:raise ValueError('frozen video changed')
                files=s.page.locator('input[type="file"]')
                timeout_stage='upload_preparation'
                count=files.count()
                self._trace(s,'douyin.upload_input',{'unique':count==1})
                if count!=1:s.page.wait_for_timeout(100);continue
                self._observe(s,'upload','upload_preparation',lambda:files.set_input_files(str(path)));s.uploaded=True
            step,stage=('form_read','form_preparation') if s.phase=='prepare' else ('result_read','submission_result') if s.phase=='result' else ('baseline_read','page_preparation')
            def read_form():
                return self._observe(s,step,stage,
                    lambda:legacy.evaluate_script(s.page,PREPARE_FORM_WITH_DIAGNOSTICS if s.phase=='prepare' else READ_RESULT_WITH_DIAGNOSTICS,form))
            raw=(self._login_read(s,read_form,classify=lambda value:douyin_login_state(
                value.get('identity') if type(value) is dict else None,s.payload['platform_user_id']))
                if baseline_identity else read_form())
            if baseline_identity:
                # READ_RESULT already brackets its profile read with two UID
                # observations. Verify that complete same-read identity before
                # using readiness/rows; a second three-GET round adds no proof.
                try:legacy.verified_identity(raw.get('identity') if type(raw) is dict else None,
                                            expected_uid=s.payload['platform_user_id'])
                except ValueError:return AdapterOutcome('pre_submit_failed',error_code='account_mismatch')
                s.browser_login=False
            self._readback(s,raw,form=form)
            timeout_stage=('submission_result' if s.final else 'page_preparation' if s.phase=='baseline' else
                           'upload_preparation' if type(raw) is dict and raw.get('upload_complete') is False else 'form_preparation')
            if type(raw) is not dict:s.page.wait_for_timeout(100);continue
            if s.final and not query and raw.get('posting_limit') is True:
                self._trace(s,'douyin.posting_limit',{'daily_limit_reached':True,'final_started':True})
                return AdapterOutcome('platform_rejected',error_code='douyin_daily_post_limit')
            if raw.get('failure_code'):return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',error_code='configuration_required')
            if raw.get('ready'):
                if s.phase=='prepare':
                    legacy.verify_prepared_form(raw,form)
                    if not s.uploaded or before_final is None:raise ValueError('upload/final authority unavailable')
                    if not query and not s.final and time.monotonic()>=deadline:break
                    waiting_started=time.monotonic()
                    if not self._wait_sms_before_submit(s):return AdapterOutcome('pre_submit_failed',error_code='cancelled')
                    deadline+=time.monotonic()-waiting_started
                    # Upload/readback can move a previously valid schedule
                    # outside the platform window. Revalidate frozen values at
                    # the actual final boundary, without changing the time.
                    try:current_form=self._form(s.payload)
                    except ValueError:return AdapterOutcome('pre_submit_failed',error_code='schedule_mismatch')
                    legacy.verify_prepared_form(raw,current_form)
                    # Coordinator re-observes UID and writes the durable marker.
                    try:before_final()
                    except ValueError:
                        kind=self._challenge(s) if s.page is not None else None
                        if s.request_id or kind:return self._park(s,kind or 'login')
                        raise
                    s.final=True;s.phase='result';s.stable=0;s.last_rows=None
                    if not query:deadline=time.monotonic()+self.step_timeout
                    try:self._observe(s,'final_click','submission_result',lambda:legacy.evaluate_script(s.page,FINAL_CLICK_WITH_LIMIT_WATCH,form))
                    except Exception:pass
                else:
                    rows=raw.get('matches',[]);s.stable=s.stable+1 if rows==s.last_rows else 1;s.last_rows=rows
                    if s.stable>=2:
                        if s.phase=='baseline':
                            existing=legacy.matching_schedule_rows(rows,form)
                            if existing:
                                self._trace(s,'douyin.existing_schedule',{'matching_count':len(existing),'final_started':False,'uploaded':False})
                                return AdapterOutcome('pre_submit_failed',error_code='adapter_failed',
                                                      error_stage='page_preparation',error_category='existing_scheduled_work')
                            s.phase='prepare';s.stable=0;s.last_rows=None
                            if not query and not s.final:deadline=time.monotonic()+self.preparation_timeout
                            self._navigate(s,legacy.UPLOAD_PATH)
                            timeout_stage='upload_preparation'
                        elif legacy.verified_scheduled_receipt(raw,s.payload['platform_user_id'],form):
                            a=self._account(s)
                            self.refreshed[a.id]={'platform_user_id':a.platform_user_id,'nickname':a.nickname,'session_blob':legacy.session_blob(s.context,a.platform_user_id),'original_ref':s.original_ref,
                                                 'login_observed_at':self._login_valid_at(s)}
                            s.stage='平台已接收定时安排'
                            return AdapterOutcome('platform_scheduled',scheduled_at=s.payload['scheduled_at'])
            s.page.wait_for_timeout(100)
        self._trace(s,'douyin.cancelled' if self.cancel.is_set() else 'douyin.timeout',
                    {'phase':s.phase,'uploaded':bool(s.uploaded),'final_started':bool(s.final),'stage':timeout_stage})
        if s.final or query or self.cancel.is_set():
            return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',error_code='timeout',
                platform_message=getattr(s,'platform_message',None) if s.final else None)
        return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',error_code='adapter_failed',
                              error_stage=timeout_stage,error_category='timeout')

    def validate_fields(self,payload):
        if payload['platform']!='douyin':return False
        from yijianjifa.app_services.publish_metadata_cache import PublishMetadataCache
        account=self.service.get(payload['account_id']);cache=PublishMetadataCache(self.engine)
        for key,kind in [('location','locations'),('declaration','declarations-ordinary')]:
            selected=payload['fields'].get(key)
            if selected is None:continue
            found=False
            for saved in cache.recent(account,kind):
                rows=saved['payload'].get('items',[]) if key=='declaration' else saved['payload']
                if selected in rows:found=True;break
            if not found:return False
        return True

    def _prepare_result_query(self,s):
        s.result_query_read_only=False
        if not s.page.url.endswith('/content/manage') and not self._challenge(s):
            self._navigate(s,'/creator-micro/content/manage')
        if s.page.url.endswith('/content/manage') and not self._challenge(s):
            s.result_query_read_only=True

    def _result_page_releasable(self,s):
        # 仅明确进入原结果查询后释放；发布后仍在原表单时保留现场，避免中断请求。
        return (s.final is True and s.request_id is None
                and getattr(s,'result_query_read_only',False) is True)
    def _capture_session_blob(self,context,uid):
        return legacy.session_blob(context,uid)
    def _persist_sessions(self):
        legacy.persist_refreshed_sessions(self.engine,self.paths,self.refreshed)
