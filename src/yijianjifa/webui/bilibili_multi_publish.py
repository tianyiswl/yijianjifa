"""Identity-bound B站 creator delivery through normal Chromium page actions.

No Cookie API replay, guessed originality, background keyboard on the desktop,
or retry of a final click. The shared lifecycle retains the original challenge
page and enforces the host's exact-attempt authority.
"""
from yijianjifa.features.publish.account_wait import AccountBusy
from pathlib import Path
from datetime import datetime,timezone
import json
import logging
import os
import re
from yijianjifa.os_integration.background_process import run_hidden
import subprocess
import time
from urllib.parse import urlsplit
from sqlalchemy import text

from yijianjifa.features.accounts.service import AccountRow,AccountIdentityMismatch
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome,AdapterValidation,ERROR_STAGES
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from .chromium_publish_session import ChromiumPublishSessionAdapter,LiveSession,fingerprint
from . import chromium_publish as browser
from . import bilibili_auth as auth
from . import creator_auth
from . import bilibili_publish_form as form_contract
from yijianjifa.app_services import bilibili_static_catalog as catalog


BILIBILI_CHALLENGE=r'''() => {
 const visible=e=>{
   if(!e?.isConnected||!Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0))return false;
   for(let n=e;n;n=n.parentElement){const c=getComputedStyle(n);
     if(c.display==='none'||c.visibility==='hidden'||c.visibility==='collapse'||Number(c.opacity)<=0)return false;}
   return true;
 };
 // The captcha SDK can leave empty containers and tracking-sized frames on
 // ordinary management pages. A name alone is not an interactive challenge.
 const sized=e=>visible(e)&&Array.from(e.getClientRects()).some(r=>r.width>=40&&r.height>=20);
 const prompt=/安全验证|图形验证|滑块验证|完成验证|请.{0,8}(?:完成|进行).{0,8}验证|拖动.{0,12}(?:滑块|拼图)|点击按钮进行验证|请.{0,8}(?:依次点击|按顺序点击|点击图中)|security verification|complete.{0,12}captcha/i;
 const named=e=>/captcha|geetest|verify|验证/i.test([e.id,e.className].join(' '));
 const pending=e=>{
   const text=(e.innerText||'').trim();
   return prompt.test(text)&&! /^(?:(?:账号|身份|实名|安全|图形|滑块).{0,6})?(?:认证|验证)(?:已)?(?:完成|成功|通过)|^已(?:完成|通过).{0,4}验证/.test(text);
 };
 const panels=Array.from(document.querySelectorAll('[role="dialog"],[class*="captcha"],[id*="captcha"],[class*="geetest"],[class*="verify"],[id*="verify"]'));
 const interaction=e=>sized(e)&&(e.matches('canvas,img,iframe,[role="slider"],input[type="range"]')||
   /^(?:点击按钮进行验证|(?:开始|点击|进行|完成|重新)(?:安全|图形|滑块)?验证)$/.test((e.innerText||'').trim()));
 const slider=e=>sized(e)||(e.matches('input[type="range"]')&&visible(e)&&
   Array.from(e.getClientRects()).some(r=>r.width>=40&&r.height>=8));
 const visualPuzzle=e=>/captcha|geetest/i.test([e.id,e.className].join(' '))&&
   Array.from(e.querySelectorAll('canvas')).some(sized)&&
   Array.from(e.querySelectorAll('[role="slider"],input[type="range"]')).some(slider);
 const complex=panels.some(e=>visible(e)&&e.tagName!=='IFRAME'&&
   ((named(e)||e.getAttribute('role')==='dialog')&&pending(e)&&
    Array.from(e.querySelectorAll('canvas,img,iframe,[role="slider"],input[type="range"],button,[role="button"]')).some(interaction)||
    visualPuzzle(e)))||
   Array.from(document.querySelectorAll('iframe')).some(e=>sized(e)&&prompt.test([e.title,e.getAttribute('aria-label')].join(' ')));
 const sms=Array.from(document.querySelectorAll('input')).filter(e=>visible(e)&&/验证码/.test([e.placeholder,e.getAttribute('aria-label')].join(' ')));
 const login=/\/login(?:\/|$)/.test(location.pathname)||Array.from(document.querySelectorAll('body *')).some(e=>visible(e)&&e.children.length===0&&['扫码登录','手机号登录'].includes(e.textContent.trim()));
 return {sms:sms.length===1,complex,login};
}'''


class BilibiliMultiPublishAdapter(ChromiumPublishSessionAdapter):
    platform_id='bilibili'
    verified_capabilities=frozenset({'ordinary','publish_now','software_schedule','platform_schedule','result_query'})
    public_field_schema={
        'title':{'type':'string','max_length':80},'body':{'type':'string','max_length':2000},
        'tags':{'type':'string','max_items':10,'max_item_length':20},
        'category_choice':{'type':'string','choices_source':'builtin','choices':catalog.category_choices(),'default':catalog.DEFAULT_FIELDS['category_choice']},
        'creation_statement_choice':{'type':'string','choices_source':'builtin','choices':catalog.builtin_options()['creation_statement_choices'],'default':catalog.DEFAULT_FIELDS['creation_statement_choice']},
        'category_id':{'type':'integer','required':False,'choices_source':'platform_options'},
        'category_name':{'type':'string','required':False},'category_mode':{'type':'string','choices':['legacy','human'],'required':False},
        'creation_statement_id':{'type':'integer','required':False,'choices_source':'platform_options'},
        'creation_statement_name':{'type':'string','required':False,'max_length':255},
        'content_authorization':{'type':'boolean','default':False},'reprint_source':{'type':'string','max_length':200},
        'cover':{'kinds':['first_frame'],'custom_ready':False}}

    def __init__(self,*args,step_timeout=180,**kwargs):
        super().__init__(*args,step_timeout=step_timeout,**kwargs)
        # Configuration sessions have no delivery/attempt authority and must
        # never enter the publishing intervention/resume registry.
        self.options_sessions={}
        self._preparation_stage='page_preparation'
        self._preparation_failure=None
        self._preparation_diagnostic=None
        self._upload_entry_kind='unobserved'
        self._statement_diagnostic=None
        self._schedule_diagnostic=None
        self._upload_diagnostic=None
        self._preparation_deadline=None

    def failure_diagnostic(self,error):
        # The host accepts only these two finite enums. Diagnostic source
        # frames are opt-in local metadata, never part of a delivery DTO.
        result=self._preparation_failure;self._preparation_failure=None
        return dict(result) if result else {}

    @staticmethod
    def _preparation_category(error,stage):
        from yijianjifa.features.publish.coordinator import failure_category
        message=error.args[0] if len(error.args)==1 and type(error.args[0]) is str else ''
        playwright=type(error).__module__.startswith('playwright.')
        if playwright and any(marker in message for marker in ('TypeError:','ReferenceError:','SyntaxError:')):
            return 'javascript_read_failed'
        if stage=='page_open' and playwright:
            if any(marker in message for marker in ('ProcessSingleton','profile appears to be in use',
                                                    'user data directory is already in use')):return 'profile_in_use'
            return 'browser_launch_failed'
        if stage=='session_restore':return 'session_unavailable'
        if type(error) is ValueError:
            if message=='existing B站 draft must be handled first':return 'existing_draft'
            if message in {'frozen video changed','frozen video changed before cover','frozen video changed during cover'}:
                return 'resource_unavailable'
            if message in {'ambiguous B站投稿页','B站标题输入框不唯一','B站简介输入框不唯一','B站分区控件不唯一',
                'B站一级分区不唯一','B站分区选项不唯一','B站声明控件不唯一','B站声明选项不存在或不唯一',
                'B站内容授权不可选','B站转载来源输入框不唯一','B站已有活动标签，请先取消活动','B站标签输入框不唯一',
                'B站定时开关不唯一','未读取到当前B站账号定时范围','排期超出当前B站账号允许的定时范围',
                'B站日期控件不唯一','B站日历标题不唯一','B站日历年月无法识别','B站日历月份不可选',
                'B站日历目标日期不在范围内','B站日期不唯一或不可选','B站时间控件不唯一','B站时分控件发生变化',
                'B站时分不可选','B站主封面入口不唯一','B站封面上传输入框不唯一','final B站 form changed',
                'B站最终投稿控件不唯一','B站普通新投稿入口不可用','B站声明选择结果不一致',
                'B站定时容器不唯一','B站定时开关不可选'}:return 'form_changed'
        return failure_category(error)

    def _record_preparation_failure(self,error):
        """Retain only finite metadata, without exception text or page values."""
        known_types={'Error','TimeoutError','TargetClosedError','ValueError','RuntimeError','AttributeError',
                     'TypeError','KeyError','AccountIdentityMismatch','FileNotFoundError','CalledProcessError'}
        error_type=type(error).__name__ if type(error).__name__ in known_types else 'other'
        message=error.args[0] if len(error.args)==1 and type(error.args[0]) is str else ''
        js='none'
        if type(error).__module__.startswith('playwright.'):
            if 'getClientRects is not a function' in message:js='node_geometry_unavailable'
            elif 'getComputedStyle' in message and ('not of type' in message or 'not an Element' in message):js='non_element_style_read'
            elif 'ReferenceError:' in message:js='reference_error'
            elif 'SyntaxError:' in message:js='syntax_error'
            elif 'TypeError:' in message:js='type_error'
            else:js='other_browser_error'
        methods={'_advance_steps','_open','_account','_frame','_challenge','_identity','_upload','_one',
                 '_select_fields','_select_statement_option','_schedule_candidates','_schedule_click','_set_schedule','_prepare_cover','_prepare_form','_click_final'}
        frames=[];frame=error.__traceback__
        while frame:
            code=frame.tb_frame.f_code
            if frame.tb_frame.f_globals.get('__name__')==__name__ and code.co_name in methods:
                frames.append({'source':'bilibili_multi_publish.py','method':code.co_name,'line':frame.tb_lineno})
            frame=frame.tb_next
        detail={'error_type':error_type,'js_kind':js,'frames':frames[-8:],
                'error_stage':self._preparation_stage,'error_category':self._preparation_category(error,self._preparation_stage),
                'upload_entry_kind':self._upload_entry_kind}
        if self._statement_diagnostic is not None:detail['statement_observation']=dict(self._statement_diagnostic)
        if self._schedule_diagnostic is not None:detail['schedule_observation']=dict(self._schedule_diagnostic)
        if self._upload_diagnostic is not None:detail['upload_observation']=dict(self._upload_diagnostic)
        self._preparation_diagnostic=detail
        if os.environ.get('P011_PUBLISH_DIAGNOSTICS')=='1':
            logging.getLogger(__name__).warning('bilibili preparation diagnostic: %s',json.dumps(detail,sort_keys=True))

    def preparation_diagnostic(self):
        return json.loads(json.dumps(self._preparation_diagnostic)) if self._preparation_diagnostic else {}

    def _account(self,s):
        account=self.service.get(s.payload['account_id']);uid=s.payload['platform_user_id']
        if account.platform!=self.platform_id or account.platform_user_id!=uid or (self.expected_uid and self.expected_uid!=uid):raise ValueError('frozen account mismatch')
        auth.verified_identity(dict(origin='https://member.bilibili.com',logged_in=True,uid=uid,profile_uid=uid,nickname=account.nickname))
        return account

    def _init_observations(self,s):
        s.pre=None;s.human=None;s.options=None;s.options_at=None;s.form_filled=False;s.cover_uploaded=False
        s.file_selection_returned=False
        s.cover_ready=False;s.cover_path=None;s.final_form=None;s.resolved_form=None;s.final_spec=None;s.list_rows=[];s.submit_ok=False;s.submit_rejected=False
        s.receipt=dict(s.payload.get('receipt_reference') or getattr(s,'receipt',{}))
        s.identity_nav=None

    def _open(self,s,*,query=False):
        if s.page is not None:return
        account=self._account(s)
        if not account.session_ref:raise ValueError('identity-bound session unavailable')
        if not s.locked:
            if not browser.account_lock(account.id).acquire(blocking=False):raise AccountBusy()
            s.locked=True
        if getattr(s,'options_only',False):mode='visible'
        else:
            with session_scope(self.engine) as db:
                setting=db.get(Setting,'publish.browser_mode');mode=json.loads(setting.value_json) if setting else 'background'
        # The driver inherits bundled-browser environment at start. Preparing
        # options inside launch arguments would update only the parent process.
        s.external_browser = mode == 'visible'
        options=self._inspection_browser_options(browser.browser_options(mode))
        if self.driver is None:
            from .playwright_runtime import acquire_playwright
            self.driver=acquire_playwright()
        profile=creator_auth.chromium_profile(auth.SPEC,self.store,account.platform_user_id)
        s.context=self.driver.chromium.launch_persistent_context(str(profile),**options)
        if getattr(s,'options_only',False):self._install_write_guard(s.context)
        else:self._install_publish_guard(s)
        s.context.set_default_timeout(10000)
        self._preparation_stage='session_restore'
        creator_auth.restore_session(auth.SPEC,s.context,self.store,account,profile);s.original_ref=account.session_ref
        s.page=s.context.pages[0] if s.context.pages else s.context.new_page()
        self._init_observations(s);s.page.on('response',lambda response:self._response(s,response))
        self._preparation_stage='page_navigation'
        s.page.goto(form_contract.MANAGE_URL,wait_until='domcontentloaded')

    def _frame(self,s):
        frames=[f for f in s.page.frames if urlsplit(f.url).scheme=='https' and urlsplit(f.url).netloc=='member.bilibili.com' and urlsplit(f.url).path=='/york/videoup']
        if len(frames)>1:raise ValueError('ambiguous B站投稿页')
        return frames[0] if frames else s.page

    @staticmethod
    def _navigation_observation_error(error):
        from playwright.sync_api import Error
        return isinstance(error,Error) and any(text in str(error) for text in (
            'Execution context was destroyed','Cannot find context with specified id',
            'Frame was detached','Frame has been detached','because of a navigation'))

    def _identity(self,s):
        try:value=s.page.evaluate(auth.IDENTITY_SCRIPT)
        except Exception as error:
            if not self._navigation_observation_error(error):raise
            raise ValueError('identity observation unavailable during navigation') from None
        uid,name=auth.verified_identity(value,s.payload['platform_user_id'])
        nav=getattr(s,'identity_nav',None)
        if nav is not None:
            if nav.get('logged_in') is not True:raise ValueError('normal nav identity unavailable')
            if nav.get('uid')!=uid:raise AccountIdentityMismatch('normal nav account identity mismatch')
        return uid,name

    def _challenge(self,s):
        surfaces=[s.page];frame=self._frame(s)
        if frame is not s.page:surfaces.append(frame)
        for surface in surfaces:
            try:observation=surface.evaluate(BILIBILI_CHALLENGE) or {}
            except Exception as error:
                if not self._navigation_observation_error(error):raise
                # An in-flight route change is not a login/captcha observation.
                continue
            kind=browser.challenge_kind(observation)
            if kind:return kind
        return None

    def _verification_surface(self,s):
        surfaces=[s.page];frame=self._frame(s)
        if frame is not s.page:surfaces.append(frame)
        matches=[x for x in surfaces if (x.evaluate(BILIBILI_CHALLENGE) or {}).get('sms') is True]
        if len(matches)!=1:raise ValueError('verification input unavailable or ambiguous')
        return matches[0]

    def _validate_session(self,s,payload,execution):
        if execution not in {'publish_now','software_schedule','platform_schedule'} or self.cancel.is_set():return AdapterValidation('',False)
        if not self._version(s):return self._park(s,'login','所选B站账号登录资料已更新，请确认仍使用原账号继续。',reason='session_changed')
        self._open(s);deadline=time.monotonic()+10
        while not self.cancel.is_set() and time.monotonic()<deadline:
            self._trace(s,'bilibili.'+s.phase,{'final_started':bool(s.final)})
            kind=self._challenge(s)
            if kind:return self._park(s,kind)
            try:uid,_=self._identity(s);return AdapterValidation(uid,True)
            except AccountIdentityMismatch:return AdapterValidation('',False)
            except ValueError:s.page.wait_for_timeout(200)
        return self._park(s,'login','程序暂未读取到所选B站账号身份。请重新核对原账号；当前页面未必需要重新登录。',reason='identity_unavailable')

    def validate_fields(self,payload):
        try:form_contract.validate_form(payload,local_choices=True)
        except (ValueError,TypeError,KeyError):return False
        return payload.get('platform')==self.platform_id

    def plan_readiness(self,payload):
        try:
            a=self.service.get(payload['account_id'])
            if a.platform!=self.platform_id or a.login_state!='valid' or not a.session_ref:return 'account_verification_required'
            f=payload.get('fields',{})
            if not f.get('category_id') and not f.get('category_choice'):return 'category_required'
            if f.get('creation_statement_id') is None and not f.get('creation_statement_choice'):return 'creation_statement_required'
            form_contract.validate_form(payload,local_choices=True)
        except ValueError as error:
            if str(error)=='B站定时需带时区并按5分钟刻度选择':return 'bilibili_schedule_requires_five_minutes'
            return 'platform_fields_invalid'
        except (TypeError,KeyError):return 'platform_fields_invalid'
        return None

    def _response(self,s,response):
        """Observe requests made normally by this page; never generate an API call."""
        try:
            url=urlsplit(response.url)
            if url.scheme!='https' or url.netloc not in {'member.bilibili.com','api.bilibili.com'}:return
            data=response.json()
            if type(data) is not dict or type(data.get('code')) is not int:return
            body=data.get('data');path=url.path
            if path=='/x/web-interface/nav':
                # Observe only the original top page's natural GET. Never use
                # this response alone, nor let an unrelated frame grant identity.
                if response.request.method!='GET' or response.request.frame is not s.page.main_frame:return
                uid=body.get('mid') if type(body) is dict else None
                if type(uid) is int and 0<uid<=9007199254740991:uid=str(uid)
                if not (type(uid) is str and re.fullmatch(r'[1-9][0-9]{0,31}',uid)):uid=None
                s.identity_nav=dict(logged_in=data['code']==0 and type(body) is dict and body.get('isLogin') is True and uid is not None,uid=uid)
            elif path=='/x/vupre/web/archive/pre' and data['code']==0 and type(body) is dict:
                s.pre={k:body[k] for k in ('type_mode','typelist','neutral_mark','myinfo') if k in body}
                s.options=form_contract.safe_options(s.pre,s.human);s.options_at=datetime.now(timezone.utc).isoformat()
            elif path.endswith('/human/type2/list') and data['code']==0 and type(body) is dict and type(body.get('type_list')) is list:
                s.human=body['type_list'][:500];s.options=form_contract.safe_options(s.pre,s.human)
            elif path=='/x/web/archives' and data['code']==0 and type(body) is dict and type(body.get('arc_audits')) is list:
                s.list_rows=[{k:item['Archive'][k] for k in ('aid','bvid','title','state','dtime') if k in item['Archive']}
                    for item in body['arc_audits'][:100] if type(item) is dict and type(item.get('Archive')) is dict]
            elif path=='/x/vu/web/cover/up' and s.final and s.final_form and data['code']==0 and response.request.method=='POST':
                remote=form_contract.uploaded_cover_reference(response.request.header_value('content-type'),response.request.post_data_buffer,
                    s.final_form.get('cover_reference'),body)
                if remote:s.final_form={**s.final_form,'cover_reference':remote}
            elif path=='/x/vu/web/add/v3' and s.final and response.request.method=='POST':
                # The final response binds the exact resolved form recorded
                # before that one click, never a later changed option list.
                f=s.final_spec or form_contract.validate_form(s.payload,query=True)
                if not form_contract.matches_submit(response.request.post_data_json,f,s.final_form):return
                if data['code']==0:
                    reference=form_contract.receipt_reference(body)
                    if reference:s.submit_ok=True;s.receipt=reference
                else:s.submit_rejected=True
        except Exception:return

    def _options_key(self,account):return 'publish.bilibili.options:'+account.platform_user_id

    def get_cached_platform_options(self,account_id):
        account=self.service.get(account_id)
        if account.platform!=self.platform_id:raise ValueError('account platform mismatch')
        with session_scope(self.engine) as db:
            row=db.get(Setting,self._options_key(account));value=json.loads(row.value_json) if row else None
        if type(value) is not dict or value.get('version')!=1:return {'status':'empty','categories':[],'statements':[]}
        # Account limits expire and are never promoted to fresh login evidence.
        try:fresh=0<=(datetime.now(timezone.utc)-datetime.fromisoformat(value['fetched_at'])).total_seconds()<300
        except (ValueError,KeyError,TypeError):fresh=False
        return {**{k:value[k] for k in ('categories','statements','category_mode','fetched_at') if k in value},
                'schedule':value.get('schedule') if fresh else None,'status':'ready'}

    def read_platform_options(self,account_id):
        """Legacy finite no-upload read; callers without a ticket auto-close."""
        self._thread();account=self.service.get(account_id)
        if account.platform!=self.platform_id:raise ValueError('account platform mismatch')
        if account.login_state!='valid' or not account.session_ref:
            return {'status':'verification_required','categories':[],'statements':[],
                    'verification_kind':'login','verification_stage':'local'}
        payload=dict(account_id=account.id,platform_user_id=account.platform_user_id,session_fingerprint=fingerprint(account))
        s=LiveSession(payload,dict(delivery_id='options',task_id='options',attempt=0,digest='options'))
        s.options_only=True;s.options_entered=False
        try:
            return self._read_options(s)
        finally:self._release_page(s)

    @staticmethod
    def _empty_options(status='error',**extra):return dict(status=status,categories=[],statements=[],**extra)

    def _options_account(self,s):
        account=self._account(s)
        if account.login_state!='valid' or not account.session_ref or fingerprint(account)!=s.payload['session_fingerprint']:
            raise ValueError('configuration account session changed')
        return account

    def _read_options(self,s):
        """Only navigate ordinary pages and observe their option responses."""
        self._options_account(s);self._open(s);deadline=time.monotonic()+20
        while not self.cancel.is_set() and time.monotonic()<deadline and not s.page.is_closed():
            self._options_account(s)
            kind=self._challenge(s)
            if kind:
                if kind not in {'login','sms','complex'}:return self._empty_options()
                return self._empty_options('verification_required',verification_kind=kind,
                    verification_stage='upload' if s.options_entered else 'manager')
            try:self._identity(s)
            except AccountIdentityMismatch:return self._empty_options()
            except ValueError:s.page.wait_for_timeout(200);continue
            if not s.options_entered:
                s.page.goto(form_contract.UPLOAD_URL,wait_until='domcontentloaded');s.options_entered=True;continue
            if s.options and s.options['categories'] and s.options['statements']:
                # Reconfirm both the live page and the frozen local binding at
                # the cache boundary, including after a human login switch.
                self._identity(s);account=self._options_account(s)
                value={**s.options,'fetched_at':s.options_at,'version':1}
                with session_scope(self.engine) as db:
                    key=self._options_key(account);row=db.get(Setting,key)
                    if row is None:db.add(Setting(key=key,value_json=json.dumps(value,ensure_ascii=False)))
                    else:row.value_json=json.dumps(value,ensure_ascii=False)
                return {**s.options,'fetched_at':s.options_at,'status':'ready'}
            s.page.wait_for_timeout(200)
        return self._empty_options()

    def _close_options(self,ticket,s):
        # A failed close retains the original context and its account lock for
        # a later cancel/shutdown retry; it must not advertise successful cleanup.
        if s.context:s.context.close()
        s.context=None;s.page=None
        if s.locked:browser.account_lock(s.payload['account_id']).release();s.locked=False
        if self.options_sessions.get(ticket) is s:del self.options_sessions[ticket]

    def _finish_options(self,ticket,s,result):
        if result['status']=='verification_required':
            s.options_pending_result={**result,'verification_available':True}
            return dict(s.options_pending_result)
        try:self._close_options(ticket,s)
        except Exception:
            s.options_cleanup_failed=True
            return self._empty_options(cleanup_complete=False)
        return {**result,'cleanup_complete':True}

    def begin_platform_options(self,account_id,ticket):
        """Start a dedicated visible, five-minute configuration session."""
        self._thread()
        if type(ticket) is not str or not ticket or len(ticket)>200:return self._empty_options(cleanup_complete=True)
        if ticket in self.options_sessions:
            s=self.options_sessions[ticket]
            if s.payload['account_id']!=account_id:return self._empty_options(cleanup_complete=True)
            if s.options_cleanup_failed or time.monotonic()>=s.options_expires_at:
                return self._finish_options(ticket,s,self._empty_options())
            try:self._options_account(s)
            except Exception:return self._finish_options(ticket,s,self._empty_options())
            return dict(s.options_pending_result)
        if any(s.payload['account_id']==account_id for s in self.options_sessions.values()):
            return self._empty_options('account_busy',cleanup_complete=True)
        account=self.service.get(account_id)
        if account.platform!=self.platform_id:raise ValueError('account platform mismatch')
        if account.login_state!='valid' or not account.session_ref:
            return self._empty_options('verification_required',verification_kind='login',verification_stage='local',
                                       verification_available=False,cleanup_complete=True)
        payload=dict(account_id=account.id,platform_user_id=account.platform_user_id,session_fingerprint=fingerprint(account))
        s=LiveSession(payload,dict(delivery_id='options',task_id='options',attempt=0,digest='options'))
        s.options_only=True;s.options_entered=False;s.options_expires_at=time.monotonic()+300;s.options_cleanup_failed=False
        self.options_sessions[ticket]=s
        try:result=self._read_options(s)
        except Exception as error:result=self._empty_options('account_busy' if str(error)=='account busy' else 'error')
        return self._finish_options(ticket,s,result)

    def continue_platform_options(self,account_id,ticket,action):
        """Human controls only this ticket's original page, without resume grants."""
        self._thread();s=self.options_sessions.get(ticket)
        if s is None:return self._empty_options('cancelled' if action=='cancel' else 'error',cleanup_complete=True)
        if s.payload['account_id']!=account_id:return self._empty_options(cleanup_complete=True)
        if action=='cancel':return self._finish_options(ticket,s,self._empty_options('cancelled'))
        if action not in {'show','continue'}:return self._empty_options(cleanup_complete=False)
        if s.options_cleanup_failed or time.monotonic()>=s.options_expires_at or self.cancel.is_set():
            return self._finish_options(ticket,s,self._empty_options())
        try:self._options_account(s)
        except Exception:return self._finish_options(ticket,s,self._empty_options())
        if s.page is None or s.page.is_closed():return self._finish_options(ticket,s,self._empty_options())
        if action=='show':
            result=dict(s.options_pending_result)
            try:
                s.page.bring_to_front()
                return {**result,'verification_view_state':'opened'}
            except Exception:return {**result,'verification_view_state':'failed'}
        try:result=self._read_options(s)
        except Exception:result=self._empty_options()
        return self._finish_options(ticket,s,result)

    def expire_platform_options(self):
        """Worker-only cleanup fallback; jobs own their visible expiry state."""
        self._thread();closed=0;now=time.monotonic()
        for ticket,s in list(self.options_sessions.items()):
            if now<s.options_expires_at:continue
            try:self._close_options(ticket,s);closed+=1
            except Exception:s.options_cleanup_failed=True
        return closed

    def _owned_sessions(self):
        return super()._owned_sessions()+list(self.options_sessions.values())

    def close(self):
        if self.thread_id is None:return
        self._thread();errors=[];base_error=None
        for ticket,s in list(self.options_sessions.items()):
            try:self._close_options(ticket,s)
            except Exception as error:s.options_cleanup_failed=True;errors.append(error)
        try:super().close()
        except Exception as error:base_error=error
        # Only a successful context close or exclusive actual SDK shutdown in
        # the base clears these handles. Shared lease release is not proof.
        for ticket,s in list(self.options_sessions.items()):
            if s.context is None:
                self._forget_page(s);del self.options_sessions[ticket]
        if errors:raise ValueError('B站 configuration browser close failed') from errors[0]
        if base_error:raise base_error

    def _observe_upload(self,s,raw):
        """Record native acknowledgement, never names, media IDs or page text."""
        bounded=lambda key,maximum:raw[key] if type(raw.get(key)) is int and -1<=raw[key]<=maximum else -1
        detail=dict(file_selection_started=bool(s.uploaded),file_selected=bool(getattr(s,'file_selection_returned',False)),native_upload_enabled_known=raw.get('native_upload_enabled_known') is True,
            native_upload_enabled=raw.get('native_upload_enabled') is True,native_task_present=raw.get('native_task_present') is True,
            native_task_count=bounded('native_task_count',2),native_video_count=bounded('native_video_count',2),
            native_video_status=bounded('native_video_status',10),title_count=bounded('title_count',2),
            body_count=bounded('body_count',2),button_count=bounded('button_count',2),upload_complete=raw.get('upload_complete') is True)
        self._upload_diagnostic=detail
        self._trace(s,'bilibili.upload_observation',detail)

    def _upload(self,s,frame):
        raw=frame.evaluate(form_contract.READ_FORM)
        self._observe_upload(s,raw)
        self._upload_entry_kind=raw.get('new_upload_state') if raw.get('new_upload_state') in {'ready','active_form','unsupported_mode','unavailable'} else 'unavailable'
        if raw.get('videos') or raw.get('title'):raise ValueError('existing B站 draft must be handled first')
        if self._upload_entry_kind in {'active_form','unsupported_mode'}:
            raise ValueError('existing B站 draft must be handled first' if raw.get('existing_draft') else 'B站普通新投稿入口不可用')
        if raw.get('new_upload_ready') is not True or self._upload_entry_kind!='ready':return False
        # Do not continue, discard or clear a cached record. This is the
        # official ordinary entrance's independent native file selection.
        path=Path(s.payload['video_path'])
        if not path.is_file() or sha256_file(path)!=s.payload['video_hash']:raise ValueError('frozen video changed')
        files=frame.locator(form_contract.VIDEO_FILE)
        if files.count()!=1:return False
        s.uploaded=True;files.set_input_files(str(path));s.file_selection_returned=True;return True

    @staticmethod
    def _one(locator,message):
        if locator.count()!=1:raise ValueError(message)
        return locator

    def _select_statement_option(self,s,frame,f,observed):
        """Wait for the native menu's transition, then select one exact item."""
        known=type(observed.get('creation_statement_id')) is int
        matches=known and observed['creation_statement_id']==f['creation_statement_id']
        text_matches=observed.get('statement_text')==f['creation_statement_name']
        detail={'state_id_known':known,'state_id_matches':matches,'statement_text_matches':text_matches,
                'choice_path':'menu','expected_label_present':False,'visible_match_count':0}
        self._statement_diagnostic=detail
        # The official default NO_MARK is -1. Skip a redundant menu mutation
        # only when both the observed state and native display already agree.
        if f['creation_statement_id']==-1 and matches and text_matches:
            detail['choice_path']='already_selected';return
        deadline=time.monotonic()+min(10,self.step_timeout)
        if self._preparation_deadline is not None:deadline=min(deadline,self._preparation_deadline)
        control=self._one(frame.locator('.creation-statement-container .bcc-select-input-inner'),'B站声明控件不唯一')
        remaining=deadline-time.monotonic()
        if self.cancel.is_set() or remaining<=0:raise ValueError('B站声明选项不存在或不唯一')
        control.click(timeout=max(1,remaining*1000))
        expected=frame.locator('.creation-statement-container .bcc-option').filter(has=frame.get_by_text(f['creation_statement_name'],exact=True))
        visible=frame.locator('.creation-statement-container .bcc-option:visible:not(.is-disabled)').filter(has=frame.get_by_text(f['creation_statement_name'],exact=True))
        while not self.cancel.is_set() and time.monotonic()<deadline:
            self._trace(s,'bilibili.'+s.phase,{'final_started':bool(s.final)})
            count=visible.count();detail['visible_match_count']=min(count,2)
            detail['expected_label_present']=expected.count()>0
            if count>1:raise ValueError('B站声明选项不存在或不唯一')
            if count==1:
                self._one(visible,'B站声明选项不存在或不唯一').click(timeout=max(1,(deadline-time.monotonic())*1000))
                selected=frame.evaluate(form_contract.READ_FORM)
                detail['selected_state_id_known']=type(selected.get('creation_statement_id')) is int
                detail['selected_state_id_matches']=detail['selected_state_id_known'] and selected['creation_statement_id']==f['creation_statement_id']
                detail['selected_statement_text_matches']=selected.get('statement_text')==f['creation_statement_name']
                if not detail['selected_state_id_matches']:
                    raise ValueError('B站声明选择结果不一致')
                return
            s.page.wait_for_timeout(min(50,max(1,(deadline-time.monotonic())*1000)))
        raise ValueError('B站声明选项不存在或不唯一')

    def _select_fields(self,s,frame,f):
        self._one(frame.locator(form_contract.TITLE),'B站标题输入框不唯一').fill(f['title'])
        editor=self._one(frame.locator(form_contract.BODY),'B站简介输入框不唯一')
        editor.click();editor.press('ControlOrMeta+A');s.page.keyboard.insert_text(f['body'])
        scope='.video-human-type' if f['category_mode']=='human' else '.video-type'
        self._one(frame.locator(scope+' .select-controller'),'B站分区控件不唯一').click()
        if f['category_mode']=='legacy':
            chosen=next(x for x in s.options['categories'] if x['id']==f['category_id'] and x['name']==f['category_name'])
            parent=frame.locator(scope+' .drop-f-item').filter(has=frame.get_by_text(chosen['parent_name'],exact=True))
            self._one(parent,'B站一级分区不唯一').click();item=frame.locator(scope+' .drop-t-item').filter(has=frame.get_by_text(f['category_name'],exact=True))
        else:item=frame.locator(scope+' .human-type-list .drop-list-v2-item').filter(has=frame.get_by_text(f['category_name'],exact=True))
        self._one(item,'B站分区选项不唯一').click()
        self._select_statement_option(s,frame,f,frame.evaluate(form_contract.READ_FORM))
        selected=frame.evaluate(form_contract.READ_FORM)
        if selected.get('content_authorization') is not f['content_authorization']:
            self._one(frame.locator('.creation-statement-container .bcc-select-input-inner'),'B站声明控件不唯一').click()
            self._one(frame.locator('.creation-statement-container .auth-content:not(.is-disabled)'),'B站内容授权不可选').click()
            frame.locator(form_contract.TITLE).click()
        if f['source']:self._one(frame.locator('.statement-source input'),'B站转载来源输入框不唯一').fill(f['source'])
        existing=frame.locator('#tag-container .tag-pre-wrp .label-item-v2-container')
        for _ in range(min(existing.count(),10)):
            chip=existing.first
            if chip.locator('.close').count()!=1:raise ValueError('B站已有活动标签，请先取消活动')
            chip.click()
        field=self._one(frame.locator(form_contract.TAGS),'B站标签输入框不唯一')
        for tag in f['tags']:
            field.fill(tag);field.press('Enter')
            # B站 validates each tag asynchronously; do not overwrite the
            # input with the next tag while its previous validation is pending.
            deadline=time.monotonic()+min(15,self.step_timeout)
            while time.monotonic()<deadline:
                observed=frame.evaluate(form_contract.READ_FORM)
                if tag in observed.get('tags',[]):break
                if self.cancel.is_set():raise ValueError('tag preparation cancelled')
                s.page.wait_for_timeout(100)
            else:raise ValueError('B站标签未完成确认')

    def _schedule_candidates(self,s,locator,expected,message,control):
        deadline=time.monotonic()+min(10,self.step_timeout)
        if self._preparation_deadline is not None:deadline=min(deadline,self._preparation_deadline)
        while not self.cancel.is_set() and time.monotonic()<deadline:
            self._trace(s,'bilibili.'+s.phase,{'final_started':bool(s.final)})
            count=locator.count()
            self._schedule_diagnostic.update(control=control,control_count=min(count,expected+1),expected_count=expected)
            if count>expected:raise ValueError(message)
            if count==expected:return locator,deadline
            s.page.wait_for_timeout(min(50,max(1,(deadline-time.monotonic())*1000)))
        raise ValueError(message)

    def _schedule_click(self,s,locator,message,control):
        locator,deadline=self._schedule_candidates(s,locator,1,message,control)
        remaining=deadline-time.monotonic()
        if self.cancel.is_set() or remaining<=0:raise ValueError(message)
        locator.click(timeout=max(1,remaining*1000))

    def _set_schedule(self,s,frame,f):
        containers=frame.locator('.time-container:visible');regular=frame.locator(form_contract.SCHEDULE_CONTAINER+':visible')
        count=containers.count();regular_count=regular.count()
        detail={'visible_container_count':min(count,2),'regular_container_count':min(regular_count,2),
                'all_switch_count':min(frame.locator('.time-container .switch-container').count(),2),
                'visible_switch_count':min(frame.locator('.time-container .switch-container:visible').count(),2)}
        self._schedule_diagnostic=detail
        if count!=1 or regular_count!=1:raise ValueError('B站定时容器不唯一')
        scope=self._one(regular,'B站定时容器不唯一')
        switch=self._one(scope.locator('.switch-container:visible'),'B站定时开关不唯一')
        detail['switch_disabled']=switch.evaluate("element=>element.classList.contains('switch-container-disabled')")
        if detail['switch_disabled']:raise ValueError('B站定时开关不可选')
        raw=frame.evaluate(form_contract.READ_FORM)
        if raw.get('time_enabled') is not bool(f['dtime']):self._schedule_click(s,switch,'B站定时开关不唯一','switch')
        if not f['dtime']:return
        limits=(s.options or {}).get('schedule')
        if not limits:raise ValueError('未读取到当前B站账号定时范围')
        now=int(time.time())
        if not now+limits['min_lead_seconds']<=f['dtime']<=now+limits['max_lead_seconds']:raise ValueError('排期超出当前B站账号允许的定时范围')
        self._schedule_click(s,scope.locator('.date-picker-date:visible'),'B站日期控件不唯一','date')
        desired=f['date'][:7]
        for _ in range(24):
            title_locator,deadline=self._schedule_candidates(s,scope.locator('.date-picker-nav-title:visible'),1,'B站日历标题不唯一','calendar_title')
            title=title_locator.inner_text(timeout=max(1,(deadline-time.monotonic())*1000))
            numbers=re.findall(r'\d+',title)
            if len(numbers)!=2:raise ValueError('B站日历年月无法识别')
            shown=f'{int(numbers[0]):04}-{int(numbers[1]):02}'
            if shown==desired:break
            selector='.next-btn-month' if shown<desired else '.prev-btn-month'
            self._schedule_click(s,scope.locator(selector+':visible:not(.date-select-disabled)'),'B站日历月份不可选','calendar_nav')
        else:raise ValueError('B站日历目标日期不在范围内')
        day=scope.locator('.date-wrp .date-picker-body-item:visible:not(.date-item-disabled)').filter(has_text=re.compile(r'^\s*'+str(int(f['date'][-2:]))+r'\s*$'))
        self._schedule_click(s,day,'B站日期不唯一或不可选','day')
        self._schedule_click(s,scope.locator('.date-picker-timer:visible'),'B站时间控件不唯一','time')
        panels,_=self._schedule_candidates(s,scope.locator('.time-picker-panel-select-wrp:visible'),2,'B站时分控件发生变化','panels')
        for index,value in enumerate(f['clock'].split(':')):
            panel=panels.nth(index)
            self._schedule_click(s,panel.locator('.time-picker-panel-select-item:visible:not(.time-select-disabled)').filter(has_text=re.compile(r'^'+value+r'$')),'B站时分不可选','hour' if index==0 else 'minute')
        self._schedule_click(s,frame.locator(form_contract.TITLE+':visible'),'B站标题输入框不唯一','close')

    def _prepare_cover(self,s,frame):
        if s.cover_ready:return True
        if not s.cover_uploaded:
            from yijianjifa.os_integration.resources import ffmpeg_path
            video=Path(s.payload['video_path'])
            if not video.is_file() or sha256_file(video)!=s.payload['video_hash']:raise ValueError('frozen video changed before cover')
            directory=self.paths.cache_dir/'publish-covers';directory.mkdir(parents=True,exist_ok=True)
            path=directory/('bilibili-'+s.handle+'-first.jpg')
            run_hidden([str(ffmpeg_path()),'-nostdin','-v','error','-y','-i',str(video),'-vf','scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2','-frames:v','1','-q:v','2',str(path)],check=True,capture_output=True,timeout=30)
            if sha256_file(video)!=s.payload['video_hash']:raise ValueError('frozen video changed during cover')
            s.cover_path=path
            self._one(frame.locator(form_contract.COVER_OPEN),'B站主封面入口不唯一').click()
            self._one(frame.locator(form_contract.COVER_FILE),'B站封面上传输入框不唯一').set_input_files(str(path));s.cover_uploaded=True
            return False
        done=frame.locator(form_contract.COVER_DONE)
        if done.count()!=1:return False
        done.click();s.cover_ready=True;return False

    def _prepare_form(self,s,frame,f):
        raw=frame.evaluate(form_contract.READ_FORM)
        self._observe_upload(s,raw)
        checks=form_contract.form_diagnostic(raw,f)
        self._trace(s,'bilibili.form_readback',checks)
        checks.update(form_filled=s.form_filled,cover_uploaded=s.cover_uploaded,cover_ready=s.cover_ready)
        if checks!=getattr(self,'_last_form_checks',None):
            self._last_form_checks=checks
            if os.environ.get('P011_PUBLISH_DIAGNOSTICS')=='1':
                logging.getLogger(__name__).warning('bilibili form checks: %s',json.dumps(checks,sort_keys=True))
        if not raw.get('upload_complete'):
            self._preparation_stage='upload_preparation'
            s.stage='等待B站接收所选视频' if not raw.get('native_task_present') else '等待B站视频上传完成'
            return False
        if raw.get('title_count')!=1:return False
        if not s.form_filled:
            self._select_fields(s,frame,f);self._preparation_stage='schedule_preparation'
            self._set_schedule(s,frame,f);s.form_filled=True;return False
        if not self._prepare_cover(s,frame):return False
        raw=frame.evaluate(form_contract.READ_FORM)
        checks=form_contract.form_diagnostic(raw,f)
        self._trace(s,'bilibili.form_readback',checks)
        if checks!=getattr(self,'_last_form_checks',None):
            self._last_form_checks=checks
            if os.environ.get('P011_PUBLISH_DIAGNOSTICS')=='1':
                logging.getLogger(__name__).warning('bilibili form checks: %s',json.dumps(checks,sort_keys=True))
        return form_contract.verify_form(raw,f)

    def _click_final(self,s,frame):
        if self.inspection_only:raise ValueError('inspection cannot publish')
        self._identity(s)
        raw=frame.evaluate(form_contract.READ_FORM)
        f=form_contract.validate_form(s.payload,options=s.options)
        if f!=s.resolved_form or not form_contract.verify_form(raw,f):raise ValueError('final B站 form changed')
        button=self._one(frame.locator(form_contract.FINAL).filter(has_text=re.compile(r'^立即投稿$')),'B站最终投稿控件不唯一')
        s.final_form=raw;s.final_spec=f;button.click()

    def _advance(self,s,*,before_final=None,query=False):
        self._preparation_stage='page_preparation';self._preparation_failure=None;self._preparation_diagnostic=None;self._upload_entry_kind='unobserved'
        self._statement_diagnostic=None;self._schedule_diagnostic=None;self._upload_diagnostic=None;self._preparation_deadline=None
        try:return self._advance_steps(s,before_final=before_final,query=query)
        except Exception as error:
            if not s.final and not query:
                self._preparation_failure={'error_stage':self._preparation_stage,
                    'error_category':self._preparation_category(error,self._preparation_stage)}
                self._record_preparation_failure(error)
            raise

    def _advance_steps(self,s,*,before_final=None,query=False):
        self._preparation_stage='adapter_setup'
        self._thread();self._preparation_stage='page_open';self._open(s,query=query)
        self._preparation_stage='form_preparation';f=form_contract.validate_form(s.payload,query=query,local_choices=True)
        deadline=time.monotonic()+self.step_timeout;identity_deadline=None
        self._preparation_deadline=deadline
        while not self.cancel.is_set() and time.monotonic()<deadline:
            self._trace(s,'bilibili.'+s.phase,{'final_started':bool(s.final)})
            # A bound normal receipt cannot be undone by a subsequent logout.
            if not query and s.submit_ok and s.receipt:
                self._refresh_after_receipt(s)
                return AdapterOutcome('platform_scheduled' if f['dtime'] else 'platform_accepted',receipt_reference=s.receipt,
                    scheduled_at=s.payload.get('scheduled_at') if f['dtime'] else None)
            if s.page.is_closed():break
            self._preparation_stage='verification_observation'
            kind=self._challenge(s)
            if kind:return self._park(s,kind)
            if not self._version(s,query=query):return self._park(s,'login','所选B站账号登录资料已更新，请确认仍使用原账号继续。',reason='session_changed')
            try:self._identity(s);identity_deadline=None
            except AccountIdentityMismatch:return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',error_code='account_mismatch')
            except ValueError:
                if identity_deadline is None:identity_deadline=min(deadline,time.monotonic()+10)
                if time.monotonic()>=identity_deadline:return self._park(s,'login','程序暂未读取到所选B站账号身份。请重新核对原账号，再继续原任务。',reason='identity_unavailable')
                s.page.wait_for_timeout(200);continue
            if s.phase=='baseline':
                s.phase='prepare';s.stage='正在读取B站分区和创作声明';self._preparation_stage='page_navigation'
                s.page.goto(form_contract.UPLOAD_URL,wait_until='domcontentloaded')
            elif s.phase=='prepare':
                if not s.options or not s.options['categories'] or not s.options['statements']:s.page.wait_for_timeout(200);continue
                try:actual=form_contract.validate_form(s.payload,options=s.options)
                except (ValueError,TypeError,KeyError):return AdapterOutcome('pre_submit_failed',error_code='configuration_required')
                # Freeze the user's exact current mapping before upload. A
                # later response cannot silently remap an already filled form.
                if s.resolved_form is not None and s.resolved_form!=actual:return AdapterOutcome('pre_submit_failed',error_code='configuration_required')
                s.resolved_form=actual;f=actual
                if not form_contract.verify_options(s.options,f):return AdapterOutcome('pre_submit_failed',error_code='configuration_required')
                self._preparation_stage='upload_preparation';frame=self._frame(s);s.stage='正在上传B站视频'
                if not s.uploaded and self._upload(s,frame) is False:s.page.wait_for_timeout(200);continue
                s.stage='正在填写并核对B站资料与封面';self._preparation_stage='form_preparation'
                if self._prepare_form(s,frame,f):
                    try:current=form_contract.validate_form(s.payload,options=s.options)
                    except (ValueError,TypeError,KeyError):return AdapterOutcome('pre_submit_failed',error_code='configuration_required')
                    if current!=f:return AdapterOutcome('pre_submit_failed',error_code='configuration_required')
                    # Repeat live identity and all readbacks immediately before
                    # the host journal; its exact-attempt guard remains required.
                    try:self._identity(s)
                    except AccountIdentityMismatch:return AdapterOutcome('pre_submit_failed',error_code='account_mismatch')
                    except ValueError:return self._park(s,'login','发布前暂未读取到所选B站账号身份。原页面及已上传视频已保留，请重新核对原账号后继续。',reason='identity_unavailable')
                    if self.inspection_only:return self._inspection_complete(s)
                    if before_final is None:raise ValueError('final authority unavailable')
                    try:before_final()
                    except ValueError:
                        kind=self._challenge(s)
                        if s.request_id or kind:return self._park(s,kind or 'login')
                        raise
                    s.final_spec=f;s.final=True;s.phase='result';s.stage='正在等待B站投稿回执';self._click_final(s,frame)
            else:
                s.stage='正在核对B站原稿件结果'
                if s.submit_rejected:return AdapterOutcome('platform_rejected',error_code='platform_rejected')
                found=form_contract.verified_row(s.list_rows,s.receipt,f)
                if found:
                    self._refresh_after_receipt(s)
                    return AdapterOutcome(found['state'],receipt_reference=found['reference'],scheduled_at=s.payload.get('scheduled_at') if found['state']=='platform_scheduled' else None)
            s.page.wait_for_timeout(200)
        if not query and not s.final and not self.cancel.is_set():
            self._record_preparation_failure(TimeoutError())
            self._trace(s,'bilibili.preparation_timeout',{'stage':self._preparation_stage,'file_selected':bool(getattr(s,'file_selection_returned',False)),
                'form_filled':bool(s.form_filled),'cover_ready':bool(s.cover_ready),'final_started':False})
            stage=self._preparation_stage if self._preparation_stage in ERROR_STAGES else 'page_preparation'
            return AdapterOutcome('pre_submit_failed',error_code='adapter_failed',error_stage=stage,error_category='timeout')
        return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',context={'handle':s.handle} if s.final else None,error_code='result_unknown' if s.final else 'timeout')

    def _prepare_result_query(self,s):
        if not self._challenge(s):s.page.goto(form_contract.MANAGE_URL,wait_until='domcontentloaded')

    def _refresh_after_receipt(self,s):
        try:
            a=self._account(s);uid,name=self._identity(s)
            self.refreshed[a.id]=dict(platform_user_id=uid,nickname=name,session_blob=self._capture_session_blob(s.context,uid),original_ref=s.original_ref)
        except Exception:pass

    def _capture_session_blob(self,context,uid):
        return creator_auth.encode_session(auth.SPEC,creator_auth.capture_session_state(auth.SPEC,context),uid)

    def _persist_sessions(self):
        for account_id,value in list(self.refreshed.items()):
            creator_auth.decode_session(auth.SPEC,value['session_blob'],value['platform_user_id'])
            with session_scope(self.engine) as db:
                db.execute(text('BEGIN IMMEDIATE'));row=db.get(AccountRow,account_id)
                if row is not None and row.platform==self.platform_id and row.login_state!='removed' and row.platform_user_id==value['platform_user_id'] and row.session_ref==value['original_ref']:
                    row.session_ref=self.store.put(row.id,value['session_blob'],versioned=True);row.login_state='valid'
            self.refreshed.pop(account_id,None)
