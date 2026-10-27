"""快手 creator browser delivery, isolated from ordinary Douyin publishing.

Reuse the selected account's identity-bound Chromium profile. Interaction is
through the current form; HTTP is only observed, never privately replayed.
Final actions are journalled once. A bound successful scheduled submit can
confirm platform acceptance without claiming a work ID or public visibility.
"""
from yijianjifa.features.publish.account_wait import AccountBusy
from pathlib import Path
import json
import re
import time
from urllib.parse import urlsplit
from sqlalchemy import text

from yijianjifa.features.accounts.service import AccountRow,AccountIdentityMismatch
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome,AdapterValidation
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.app_services.kuaishou_declaration_catalog import choices as declaration_choices
from .chromium_publish_session import ChromiumPublishSessionAdapter
from . import chromium_publish as browser
from . import kuaishou_auth as auth
from . import kuaishou_publish_form as form_contract
from . import kuaishou_publish_diagnostics as diagnostics


class KuaishouMultiPublishAdapter(ChromiumPublishSessionAdapter):
    platform_id='kuaishou'
    verified_capabilities=frozenset({'ordinary','publish_now','software_schedule','platform_schedule','result_query'})
    public_field_schema={'title':{'type':'string','max_length':500},'body':{'type':'string','max_length':500},
        'tags':{'type':'string','max_items':4},'cover':{'kinds':['first_frame'],'custom_ready':False},
        'content_declaration_choice':{'type':'string','choices':declaration_choices(),
            'supported_forms':['legacy','observed_account_statement']}}

    def __init__(self,*args,step_timeout=120,**kwargs):
        super().__init__(*args,step_timeout=step_timeout,**kwargs)

    def _account(self,s):
        a=self.service.get(s.payload['account_id']);uid=s.payload['platform_user_id']
        if a.platform!=self.platform_id or a.platform_user_id!=uid or (self.expected_uid and self.expected_uid!=uid):
            raise ValueError('frozen account mismatch')
        auth.verified_identity({'origin':'https://cp.kuaishou.com','logged_in':True,'uid':uid,'profile_uid':uid,'nickname':a.nickname})
        return a

    def _init_observations(self,s):
        s.upload_id=None;s.upload_finished=False;s.list_seen=False;s.list_rows=[];s.list_complete=False
        s.baseline_deadline=time.monotonic()+5
        s.submit_ok=False;s.submit_rejected=False;s.form_filled=False;s.schedule_accepted=False
        s.declaration_binding=None
        s.receipt=dict(s.payload.get('receipt_reference') or getattr(s,'receipt',{}))

    def _persisted_schedule(self,s,f):
        """Retain this attempt's durable ACK, not a fresh work-list finding."""
        if not s.final or not f['publish_ms'] or s.receipt:return False
        from sqlalchemy import select
        from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow,digest
        try:
            with session_scope(self.engine) as db:
                row=db.get(PublishDeliveryRow,s.binding['delivery_id'])
                if (row is None or row.attempt!=s.binding['attempt'] or row.task_id!=s.binding['task_id']
                        or row.request_digest!=s.binding['digest'] or row.request_digest!=self._key(s.payload)
                        or row.platform!=self.platform_id or row.account_id!=s.payload['account_id']
                        or row.platform_user_id!=s.payload['platform_user_id']
                        or row.execution_mode!='platform_schedule' or row.scheduled_at!=s.payload['scheduled_at']
                        or digest(json.loads(row.snapshot_json))!=row.request_digest):return False
                attempt=db.scalar(select(DeliveryAttemptRow).where(
                    DeliveryAttemptRow.delivery_id==row.id,DeliveryAttemptRow.number==row.attempt))
                if (attempt is None or attempt.request_digest!=row.request_digest
                        or attempt.snapshot_json!=row.snapshot_json or not attempt.final_json or not attempt.result_json):return False
                final=json.loads(attempt.final_json);result=json.loads(attempt.result_json)
                return (type(final) is dict and final.get('request_digest')==row.request_digest
                        and final.get('state')=='result_unknown' and type(result) is dict
                        and result.get('request_digest')==row.request_digest and result.get('state')=='platform_scheduled'
                        and result.get('scheduled_at')==s.payload['scheduled_at'])
        except (ValueError,TypeError,KeyError):return False

    def _open(self,s,*,query=False):
        if s.page is not None:return
        a=self._account(s)
        if not a.session_ref:raise ValueError('identity-bound session unavailable')
        lock=browser.account_lock(a.id)
        if not s.locked:
            if not lock.acquire(blocking=False):raise AccountBusy()
            s.locked=True
        with session_scope(self.engine) as db:
            row=db.get(Setting,'publish.browser_mode');mode=json.loads(row.value_json) if row else 'background'
        if getattr(s,'browser_login_visible',False):mode='visible'
        s.external_browser = mode == 'visible'
        options=self._inspection_browser_options(browser.browser_options(mode))
        if self.driver is None:
            from .playwright_runtime import acquire_playwright
            self.driver=acquire_playwright()
        profile=auth.chromium_profile(self.store,a.platform_user_id)
        s.context=self.driver.chromium.launch_persistent_context(str(profile),**options)
        s.browser_login_context_headed=mode=='visible'
        self._install_publish_guard(s)
        s.context.set_default_timeout(10000)
        if not getattr(s,'browser_login_preserve_profile',False):
            auth.restore_session(s.context,self.store,a,profile)
        s.original_ref=a.session_ref
        s.page=s.context.pages[0] if s.context.pages else s.context.new_page()
        self._init_observations(s)
        s.page.on('response',lambda response:self._response(s,response))
        s.page.goto(form_contract.MANAGE_URL,wait_until='domcontentloaded')

    def _frame(self,s):
        frames=[f for f in s.page.frames if urlsplit(f.url).scheme=='https' and urlsplit(f.url).netloc=='cp.kuaishou.com'
                and urlsplit(f.url).path=='/onvideo-application/index.html']
        if len(frames)>1:raise ValueError('ambiguous creator micro application')
        return frames[0] if frames else s.page

    def _challenge(self,s):
        for frame in [s.page,self._frame(s)]:
            kind=self._login_challenge(s,lambda:browser.challenge_kind(frame.evaluate(browser.CHALLENGE) or {}))
            if kind:return kind
        return None

    def _verification_surface(self,s):
        surfaces=[s.page];frame=self._frame(s)
        if frame is not s.page:surfaces.append(frame)
        matches=[]
        for surface in surfaces:
            observed=surface.evaluate(browser.CHALLENGE)
            if type(observed) is dict and observed.get('sms') is True:matches.append(surface)
        if len(matches)!=1:raise ValueError('verification input unavailable or ambiguous')
        return matches[0]

    def _identity(self,s):
        return self._login_read(s,lambda:auth.verified_identity(s.page.evaluate(auth.IDENTITY_SCRIPT),s.payload['platform_user_id']))

    def _validate_session(self,s,payload,execution):
        try:return self._validate_session_steps(s,payload,execution)
        except Exception as error:
            self._failure_diagnostic(s,error);raise

    def _failure_diagnostic(self,s,error):
        # Local bounded facts only. Never persist platform exception text,
        # credentials, live page contents, signed URLs or verification data.
        try:
            if s.page is not None and not s.page.is_closed():self._observed_form(s,self._frame(s))
        except Exception:pass
        value={**diagnostics.classify_exception(error),'phase':s.phase,'stage':s.stage,
               'upload_started':s.uploaded,'upload_finished':bool(getattr(s,'upload_finished',False)),
               'form_filled':bool(getattr(s,'form_filled',False)),
               'final_attempted':s.final,'submit_ok':bool(getattr(s,'submit_ok',False)),
               'submit_rejected':bool(getattr(s,'submit_rejected',False)),
               'has_work_reference':bool(getattr(s,'receipt',{})),
               'form':getattr(s,'form_facts',{})}
        key='publish.kuaishou.diagnostic:'+s.binding['delivery_id']+':'+str(s.binding['attempt'])
        try:
            with session_scope(self.engine) as db:
                row=db.get(Setting,key)
                if row is None:db.add(Setting(key=key,value_json=json.dumps(value,ensure_ascii=False)))
                else:row.value_json=json.dumps(value,ensure_ascii=False)
        except Exception:pass

    def _validate_session_steps(self,s,payload,execution):
        if execution not in {'publish_now','software_schedule','platform_schedule'} or self.cancel.is_set():return AdapterValidation('',False)
        if not self._version(s):return self._park(s,'login','该账号登录信息已更新，请确认仍为同一账号后继续。')
        self._open(s)
        kind=self._challenge(s)
        if kind:return self._park(s,kind)
        # Creator shell takes a moment to hydrate its confirmed user store.
        deadline=time.monotonic()+10
        while not self.cancel.is_set() and time.monotonic()<deadline:
            try:uid,_=self._identity(s);return AdapterValidation(uid,True)
            except AccountIdentityMismatch:return AdapterValidation('',False)
            except ValueError:
                kind=self._challenge(s)
                if kind:return self._park(s,kind)
                s.page.wait_for_timeout(200)
        return self._park(s,'login','未确认所选快手账号的有效登录，请完成登录后继续。')

    def plan_readiness(self,payload):
        try:
            a=self.service.get(payload['account_id'])
            if a.platform!=self.platform_id or a.login_state!='valid' or not a.session_ref:return 'account_verification_required'
            form_contract.validate_form(payload)
        except (ValueError,TypeError,KeyError):return 'platform_fields_invalid'
        return None

    def validate_fields(self,payload):
        try:form_contract.validate_form(payload)
        except (ValueError,TypeError,KeyError):return False
        return payload.get('platform')==self.platform_id

    def _response(self,s,response):
        """Extract bounded evidence from requests the creator page itself made."""
        try:
            url=urlsplit(response.url)
            if url.scheme!='https' or url.netloc!='cp.kuaishou.com':return
            path=url.path;data=response.json()
            if type(data) is not dict or type(data.get('result')) is not int:return
            body=data.get('data')
            if path.endswith('/video/pc/photo/list') and type(body) is dict and type(body.get('list')) is list:
                rows=body['list']
                if data.get('result')!=1 or len(rows)>100:return
                s.list_rows=[{k:r[k] for k in ('workId','publishId','title','publishStatus','publishType','uploadTime','durationSecond') if k in r}
                             for r in rows if type(r) is dict]
                s.list_seen=True;s.list_complete=body.get('nextCursor')=='no_more'
            elif '/upload/' in path and path.endswith(('/pre','/finish')) and not s.final and s.uploaded and data.get('result')==1 and type(body) is dict:
                upload_id=form_contract.safe_id(body.get('fileId'))
                if upload_id and (s.upload_id is None or s.upload_id==upload_id):
                    s.upload_id=upload_id
                    if path.endswith('/finish'):s.upload_finished=True
            elif path.endswith('/video/pc/submit') and s.final and response.request.method=='POST':
                request=response.request.post_data_json;f=form_contract.validate_form(s.payload,query=True)
                if (type(request) is not dict or not s.upload_id or form_contract.safe_id(request.get('fileId'))!=s.upload_id
                        or form_contract.compact(request.get('caption'))!=form_contract.compact(f['caption'])
                        or type(request.get('publishTime',0)) is not int
                        or request.get('publishTime',0)!=f['publish_ms']
                        or not form_contract.verify_submit_declaration(
                            request,f,declaration_binding=getattr(s,'declaration_binding',None))):return
                if data.get('result')==1:
                    s.submit_ok=True;s.receipt=form_contract.receipt_reference(body)
                    s.schedule_accepted=f['publish_ms']>0
                else:
                    s.submit_rejected=True
                    from .submit_feedback import safe_message
                    s.platform_message=safe_message(data.get('error_msg') or data.get('message'))
        except Exception:
            # A malformed/changed response is absence of evidence, never success.
            return

    def _upload(self,s,frame):
        raw=frame.evaluate(form_contract.READ_FORM)
        s.form_facts=diagnostics.formfacts(raw,form_contract.validate_form(s.payload))
        if raw.get('upload_complete') or raw.get('caption','').strip():
            raise ValueError('existing creator draft must be handled first')
        path=Path(s.payload['video_path'])
        if not path.is_file() or sha256_file(path)!=s.payload['video_hash']:raise ValueError('frozen video changed')
        files=frame.locator('#joyride-wrapper > input[type="file"], input[type="file"][accept*="video/"]')
        if files.count()!=1:return False
        # Mark before dispatch so synchronous upload responses bind to this file.
        s.uploaded=True;files.set_input_files(str(path));return True

    def _observed_form(self,s,frame):
        raw=frame.evaluate(form_contract.READ_FORM)
        # Current micro app replaces the file status with the video preview
        # when complete. Only the successful normal finish response for this
        # target's upload can supplement the positive DOM completion text.
        if s.uploaded and s.upload_finished and s.upload_id:raw={**raw,'upload_complete':True}
        s.form_facts=diagnostics.formfacts(raw,form_contract.validate_form(s.payload,query=True))
        return raw

    def failure_diagnostic(self,error):
        value=getattr(self,'_input_failure',None);self._input_failure=None
        return dict(value) if value else {}

    def _input_step(self,s,step,label,operation):
        self._preparation_stage=step;s.stage=label
        started=time.monotonic();self._trace(s,'kuaishou.'+step,{'state':'started','final_started':bool(s.final)})
        try:value=operation()
        except Exception as error:
            reason=diagnostics.classify_exception(error)['reason']
            self._input_failure={'error_stage':'schedule_preparation' if step.startswith('schedule_') else 'form_preparation',
                'error_category':'timeout' if reason=='timeout' else 'page_context_unavailable' if reason in {'execution_context_destroyed','navigation_interrupted'} else 'execution_failed'}
            self._trace(s,'kuaishou.'+step,{'state':'failed','error_kind':reason,'final_started':bool(s.final),
                'elapsed_ms':max(0,min(120000,int((time.monotonic()-started)*1000)))})
            raise
        self._trace(s,'kuaishou.'+step,{'state':'completed','final_started':bool(s.final),
            'elapsed_ms':max(0,min(120000,int((time.monotonic()-started)*1000)))})
        return value

    def _prepare_form(self,s,frame,f):
        self._input_failure=None;self._preparation_stage='form_read'
        raw=self._observed_form(s,frame)
        if not raw.get('upload_complete') or raw.get('editor_count')!=1:return False
        if not s.form_filled:
            s.declaration_binding=self._fill_declaration(frame,raw,f)
            editor=frame.locator(form_contract.EDITOR)
            # 标准fill支持可编辑内容，不依赖鼠标落点或页面全局焦点；最终仍完整回读。
            self._input_step(s,'caption_fill','正在填写快手文案',lambda:editor.fill(f['text'],timeout=20000))
            self._preparation_stage='topic_input'
            for tag in f['tags']:
                editor.press('Space')
                # The creator editor sets topic mode on Shift+Digit3. Typing
                # a literal '#' does not reliably produce that key event.
                editor.press('Shift+Digit3')
                s.page.keyboard.insert_text(tag)
                editor.press('Space')
            mode=frame.get_by_text(f['mode'],exact=True)
            if mode.count()!=1:raise ValueError('ambiguous publish mode')
            self._input_step(s,'publish_mode','正在设置快手发布方式',mode.click)
            if f['time_text']:
                date=frame.locator(form_contract.DATE_INPUT)
                if date.count()!=1:return False
                # rc-picker keeps the input read-only until a real click
                # enters typing mode. Its visible OK control commits value.
                self._input_step(s,'schedule_open','正在打开快手定时设置',date.click)
                self._input_step(s,'schedule_fill','正在填写快手定时时间',lambda:date.fill(f['time_text']))
                confirm=frame.locator(form_contract.DATE_CONFIRM)
                if confirm.count()!=1:raise ValueError('date confirmation unavailable')
                self._input_step(s,'schedule_confirm','正在确认快手定时时间',confirm.click)
                committed=date.input_value()==f['time_text']
                self._trace(s,'kuaishou.schedule_readback',{'time_matches':committed,'final_started':False})
                if not committed:
                    self._preparation_stage='schedule_readback'
                    self._input_failure={'error_stage':'schedule_preparation','error_category':'form_changed'}
                    raise ValueError('schedule input did not commit')
            s.form_filled=True
            return False
        return form_contract.verify_form(raw,f,declaration_binding=getattr(s,'declaration_binding',None))

    def _fill_declaration(self,frame,raw,f):
        binding=form_contract.resolve_declaration(raw,f)
        if form_contract.verify_declaration(raw,f,declaration_binding=binding):return binding
        row=frame.locator(form_contract.DECLARATION_ROWS).filter(
            has=frame.locator('label').filter(has_text=re.compile(r'^\s*\*?\s*作者声明\s*$')))
        if row.count()!=1:raise ValueError('kuaishou declaration control ambiguous')
        expected=f['declaration']
        if expected['choice']=='none':
            clear=row.locator(form_contract.DECLARATION_CLEAR)
            if clear.count()!=1:raise ValueError('kuaishou declaration clear unavailable')
            clear.click()
        else:
            selector=row.locator(form_contract.DECLARATION_SELECTOR)
            if selector.count()!=1:raise ValueError('kuaishou declaration control ambiguous')
            selector.click()
            options=row.locator(form_contract.DECLARATION_OPTION)
            if binding['kind']=='account_statement':
                # V2 renders an exact primary label and optional separate
                # subText. The whole option text need not equal the label.
                option=options.filter(has=frame.get_by_text(expected['name'],exact=True))
            else:option=options.filter(has_text=re.compile('^'+re.escape(expected['name'])+'$'))
            if option.count()!=1:raise ValueError('kuaishou declaration option not verified')
            option.click()
        return binding

    def _click_final(self,s,frame):
        if self.inspection_only:raise ValueError('inspection cannot publish')
        raw=self._observed_form(s,frame)
        if not form_contract.verify_form(raw,form_contract.validate_form(s.payload),
                declaration_binding=getattr(s,'declaration_binding',None)):raise ValueError('final form changed')
        button=frame.locator(form_contract.FINAL_AREA).locator(form_contract.FINAL_CONTROL).filter(has_text=re.compile(r'^发布$'))
        if button.count()!=1:raise ValueError('final button unavailable')
        from . import submit_feedback
        s.feedback_surface=frame;submit_feedback.install(frame)
        button.click()

    def _advance(self,s,*,before_final=None,query=False):
        self._input_failure=None;self._preparation_stage='page_preparation'
        try:return self._advance_steps(s,before_final=before_final,query=query)
        except Exception as error:
            self._failure_diagnostic(s,error)
            if type(error) is ValueError and not s.final:
                message=error.args[0] if len(error.args)==1 and type(error.args[0]) is str else None
                if message=='kuaishou author statement required':
                    return AdapterOutcome('pre_submit_failed',error_code='kuaishou_author_statement_required')
                if message in {
                        'kuaishou declaration control ambiguous','kuaishou declaration form not verified',
                        'kuaishou author statement options not verified','kuaishou declaration option not verified',
                        'kuaishou declaration clear unavailable'}:
                    return AdapterOutcome('pre_submit_failed',error_code='kuaishou_declaration_options_unverified')
            raise

    def _advance_steps(self,s,*,before_final=None,query=False):
        self._thread();self._open(s,query=query)
        f=form_contract.validate_form(s.payload,query=query)
        if query and self._persisted_schedule(s,f):
            # This is the original durable submit ACK. A transient login or
            # loading failure cannot disprove it; no new visibility is claimed.
            s.stage='快手已接收定时安排，作品编号待核对'
            return AdapterOutcome('platform_scheduled',scheduled_at=s.payload['scheduled_at'])
        deadline=time.monotonic()+self.step_timeout
        identity_deadline=None
        while not self.cancel.is_set() and time.monotonic()<deadline:
            if not query and s.final and s.schedule_accepted and not s.submit_rejected:
                # Guards protect submission. Once this exact final POST has a
                # successful ACK, later navigation/auth changes cannot undo it.
                s.stage='正在核对快手提交结果'
                self._refresh_after_receipt(s)
                return AdapterOutcome('platform_scheduled',scheduled_at=s.payload['scheduled_at'],receipt_reference=s.receipt)
            if s.page.is_closed():break
            kind=self._challenge(s)
            if kind:return self._park(s,kind)
            if not self._version(s,query=query):return self._park(s,'login')
            try:self._identity(s);identity_deadline=None
            except AccountIdentityMismatch:
                return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',error_code='account_mismatch')
            except ValueError:
                # domcontentloaded precedes the creator shell's identity store.
                # Missing identity is not evidence of a different account. Never
                # proceed on a cached UID; wait for the original live identity.
                if identity_deadline is None:identity_deadline=min(deadline,time.monotonic()+10)
                if time.monotonic()>=identity_deadline:
                    return self._park(s,'login','暂未读取到所选快手账号，请完成账号验证后继续原目标。')
                s.stage='正在等待快手页面确认原账号'
                s.page.wait_for_timeout(200);continue
            s.browser_login=False
            if not query and s.final and getattr(s,'feedback_surface',None) is not None:
                from . import submit_feedback
                notice=submit_feedback.outcome(s,s.feedback_surface,self.platform_id,self._key(s.payload))
                if notice and notice.state=='result_unknown':s.platform_message=notice.platform_message
                elif notice:
                    if notice.state in {'platform_scheduled','platform_accepted'}:self._refresh_after_receipt(s)
                    return notice
            if s.phase=='baseline':
                s.stage='正在检查快手已有作品'
                if not s.list_seen and time.monotonic()<s.baseline_deadline:
                    s.page.wait_for_timeout(200);continue
                # Caption equality cannot identify video bytes. Existing local
                # hash/account delivery history handles confirmed duplicates.
                s.phase='prepare';s.page.goto(form_contract.UPLOAD_URL,wait_until='domcontentloaded')
            elif s.phase=='prepare':
                frame=self._frame(s);s.stage='正在上传快手视频'
                if not s.uploaded:
                    if self._upload(s,frame) is False:s.page.wait_for_timeout(200);continue
                s.stage='正在填写并核对快手发布资料'
                if self._prepare_form(s,frame,f):
                    current=form_contract.validate_form(s.payload)
                    if current!=f:return AdapterOutcome('pre_submit_failed',error_code='schedule_mismatch')
                    if self.inspection_only:return self._inspection_complete(s)
                    if before_final is None:raise ValueError('final authority unavailable')
                    try:before_final()
                    except ValueError:
                        kind=self._challenge(s) if s.page is not None else None
                        if s.request_id or kind:return self._park(s,kind or 'login')
                        raise
                    s.final=True;s.phase='result';s.stage='正在等待快手提交回执'
                    self._click_final(s,frame)
            else:
                s.stage='正在核对快手提交结果'
                if s.submit_rejected:return AdapterOutcome('platform_rejected',error_code='platform_rejected',
                    platform_message=getattr(s,'platform_message',None) or None)
                verified=form_contract.verified_row(s.list_rows,s.receipt,f)
                if verified:
                    self._refresh_after_receipt(s)
                    return AdapterOutcome(verified['state'],receipt_reference=verified['reference'])
                if s.submit_ok and s.receipt:
                    self._refresh_after_receipt(s)
                    return AdapterOutcome('platform_accepted',receipt_reference=s.receipt)
            s.page.wait_for_timeout(200)
        # An exact current-attempt ACK can arrive inside the final callback as
        # its local deadline expires. A timeout cannot revoke that receipt.
        if not query and s.final and s.schedule_accepted and not s.submit_rejected:
            s.stage='正在核对快手提交结果'
            self._refresh_after_receipt(s)
            return AdapterOutcome('platform_scheduled',scheduled_at=s.payload['scheduled_at'],receipt_reference=s.receipt)
        # 已绑定当前提交的作品回执同样不能被本地截止时间撤销。
        if not query and s.final and s.submit_ok and s.receipt and not s.submit_rejected:
            self._refresh_after_receipt(s)
            return AdapterOutcome('platform_accepted',receipt_reference=s.receipt)
        self._failure_diagnostic(s,TimeoutError())
        return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',context={'handle':s.handle} if s.final else None,
                              error_code='result_unknown' if s.final else 'timeout',
                              platform_message=getattr(s,'platform_message',None) if s.final else None)

    def _prepare_result_query(self,s):
        if not self._challenge(s):s.page.goto(form_contract.MANAGE_URL,wait_until='domcontentloaded')

    def _refresh_after_receipt(self,s):
        # A post-navigation identity or storage failure cannot invalidate the
        # already bound submit ACK / work readback. Keep only bounded diagnostics.
        try:self._remember_session(s)
        except Exception as error:
            try:self._failure_diagnostic(s,error)
            except Exception:pass

    def _remember_session(self,s):
        a=self._account(s);uid,name=self._identity(s)
        self.refreshed[a.id]={'platform_user_id':uid,'nickname':name,'session_blob':self._capture_session_blob(s.context,uid),'original_ref':s.original_ref,
                            'login_observed_at':self._login_valid_at(s)}

    def _capture_session_blob(self,context,uid):return auth.encode_session(context.storage_state(),uid)

    def _persist_sessions(self):
        for account_id,value in list(self.refreshed.items()):
            auth.decode_session(value['session_blob'],value['platform_user_id'])
            with session_scope(self.engine) as db:
                db.execute(text('BEGIN IMMEDIATE'))
                row=db.get(AccountRow,account_id)
                if (row is not None and row.platform==self.platform_id and row.login_state!='removed'
                        and row.platform_user_id==value['platform_user_id'] and row.session_ref==value['original_ref']
                        and self._can_refresh_login_session(db,row,value.get('login_observed_at'))):
                    row.session_ref=self.store.put(row.id,value['session_blob'],versioned=True)
                    if value.get('login_observed_at') is not None:
                        self.service.record_login_check(db,row,'valid',observed_at=value['login_observed_at'])
                    else:row.login_state='valid'
            self.refreshed.pop(account_id,None)
