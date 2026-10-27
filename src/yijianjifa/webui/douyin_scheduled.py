"""Mac scheduled transport. Called only after an explicit task execution command."""
import time
from pathlib import Path
from .douyin_capability import DouyinCapabilityWindow, READ_IDENTITY, HOST, DOMAINS, UPLOAD_PATH
from .douyin_auth import verified_identity
from .douyin_publish_form import PREPARE_FORM, verify_prepared_form, validate_form_request
from .cocoa_auth import CocoaAuthProvider
from .cocoa_session import decode_cookies, encode_cookies
from .douyin_auth import SessionObservation
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.executor import SubmissionEvidence

READ_RESULT = READ_IDENTITY + r'''
if(identity.uid!==expected_uid||identity.uid_after!==expected_uid)return {identity,ready:false};
const out={identity,ready:false,path:location.pathname};
if(location.pathname!=='/creator-micro/content/manage')return out;
const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
const search=Array.from(document.querySelectorAll('input[placeholder="搜索作品"]')).filter(visible);
if(search.length!==1)return out;
if(search[0].value!==title_text){
 Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(search[0],title_text);
 search[0].dispatchEvent(new Event('input',{bubbles:true}));
 window.p011ResultNeedsSearch=true;window.p011ResultWait=0;return out;
}
if(window.p011ResultNeedsSearch){
 search[0].dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));
 window.p011ResultNeedsSearch=false;window.p011ResultWait=Date.now();return out;
}
if(!window.p011ResultWait){window.p011ResultWait=Date.now();return out;}
if(Date.now()-window.p011ResultWait<2500||document.body.innerText.includes('加载中，请稍后'))return out;
const norm=s=>s.replace(/\u200b/g,'').replace(/\s+/g,' ').trim();
const expected=norm(title_text+' '+body_text);
const labels=Array.from(document.querySelectorAll('body *')).filter(e=>visible(e)&&norm(e.innerText||'')===expected);
const rows=new Set();
for(const label of labels){let row=label;for(let i=0;i<8&&row;i++,row=row.parentElement){
 const text=row.innerText||'';
 if(text.includes('删除作品')&&/\d{4}年\d{2}月\d{2}日 \d{2}:\d{2}/.test(text)){rows.add(row);break;}
}}
const matches=Array.from(rows).map(e=>{const text=e.innerText;return {time:text.match(/\d{4}年\d{2}月\d{2}日 \d{2}:\d{2}/)?.[0],review:text.includes('定时发布中')?'定时发布中':text.includes('审核中')?'审核中':text.includes('已发布')?'已发布':'未知'};});
return {...out,ready:true,matches,query:search[0].value};
'''

# Read-only validation again in the same JavaScript turn as the final click.
FINAL_CLICK = READ_IDENTITY + r'''
if(identity.uid!==expected_uid||identity.uid_after!==expected_uid||identity.status!==0||identity.after_status!==0||identity.profile_status!==0)throw Error('identity changed');
if(location.pathname!=='/creator-micro/content/post/video')throw Error('page changed');
const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
const single=selector=>{const a=Array.from(document.querySelectorAll(selector)).filter(visible);if(a.length!==1)throw Error('ambiguous control');return a[0];};
const norm=s=>{const t=s.replace(/\u200b/g,'').replace(/\r\n/g,'\n').trim();return topic_names.length?t.replace(/\s+/g,' '):t;};
const actualTopics=Array.from(single('[contenteditable="true"]').querySelectorAll('[data-mention="#"]')).map(e=>e.textContent.trim().replace(/^#/,''));
if(JSON.stringify(actualTopics)!==JSON.stringify(topic_names))throw Error('topics changed');
if(single('input[placeholder="填写作品标题，为作品获得更多流量"]').value!==title_text||norm(single('[contenteditable="true"]').innerText)!==norm(body_text))throw Error('text changed');
if(single('input[placeholder="日期和时间"]').value!==scheduled_time_text)throw Error('time changed');
const mode=Array.from(document.querySelectorAll('input')).filter(e=>visible(e)&&e.closest('label')?.innerText.trim()==='定时发布');
if(mode.length!==1||!mode[0].checked)throw Error('mode changed');
const audience=Array.from(document.querySelectorAll('input')).filter(e=>visible(e)&&e.closest('label')?.innerText.trim()==='公开');
if(audience.length!==1||!audience[0].checked)throw Error('audience changed');
if(declaration_label&&!Array.from(document.querySelectorAll('body *')).some(e=>visible(e)&&e.children.length===0&&e.textContent.trim()===declaration_label))throw Error('declaration changed');
if(!declaration_label&&!Array.from(document.querySelectorAll('body *')).some(e=>visible(e)&&e.children.length===0&&e.textContent.trim()==='请选择自主声明'))throw Error('empty declaration not verified');

const targetLocation=typeof location_selection==='undefined'?null:location_selection;
const locationControls=Array.from(document.querySelectorAll('#douyin_creator_pc_anchor_jump .semi-select-filterable.semi-select-no-arrow')).filter(visible);
if(locationControls.length!==1)throw Error('location control unavailable');
if(targetLocation){
 const selected=Array.from(document.querySelectorAll('[role="option"].semi-select-option-selected')).filter(visible);
 if(selected.length!==1||selected[0].querySelector('[class^="name-"]')?.innerText.trim()!==targetLocation.name||selected[0].querySelector('[class^="address-"]')?.innerText.trim()!==targetLocation.address)throw Error('location changed');
}else{
 const selected=locationControls[0].querySelector('.semi-select-selection-text');
 if(selected&&selected.textContent.trim()&&selected.textContent.trim()!=='输入地理位置')throw Error('unexpected location');
}
const buttons=Array.from(document.querySelectorAll('button')).filter(e=>visible(e)&&e.textContent.trim()==='发布'&&!e.disabled);
if(buttons.length!==1||window.p011FinalClicked)throw Error('final unavailable');
window.p011FinalClicked=true;buttons[0].click();return {attempted:true};
'''


def matching_schedule_rows(rows,form):
    from datetime import datetime
    expected=datetime.strptime(form['scheduled_time_text'],'%Y-%m-%d %H:%M').strftime('%Y年%m月%d日 %H:%M')
    return [dict(row) for row in rows if row.get('time')==expected]


def verified_scheduled_receipt(raw,uid,form):
    from datetime import datetime
    verified_identity(dict(raw['identity']),expected_uid=uid)
    if raw.get('path')!='/creator-micro/content/manage' or raw.get('ready') is not True or raw.get('query')!=form['title_text']:
        return False
    rows=matching_schedule_rows(raw.get('matches',[]),form)
    expected=datetime.strptime(form['scheduled_time_text'],'%Y-%m-%d %H:%M').strftime('%Y年%m月%d日 %H:%M')
    return len(rows)==1 and rows[0].get('time')==expected and rows[0].get('review') in {'审核中','定时发布中'}


def restore_publishing_window(window,application=None):
    """Keep the live publisher visible while its web editor depends on painting.

    WKWebView stops animation-frame work when minimized. Do not fake page
    visibility or silently continue on a suspended editor. This is a visible
    window safeguard, not support for headless/background publishing.
    """
    if window is None or not hasattr(window,'isMiniaturized'):return False
    if application is None:
        import AppKit
        application=AppKit.NSApplication.sharedApplication()
    minimized=bool(window.isMiniaturized());hidden=bool(application.isHidden())
    # AppKit's public NSWindowOcclusionStateVisible flag is 1 << 1.
    occluded=hasattr(window,'occlusionState') and not (int(window.occlusionState()) & (1 << 1))
    if not minimized and not hidden and not occluded:return False
    if hidden:application.unhideWithoutActivation()
    if minimized:window.deminiaturize_(None)
    window.orderFront_(None)
    window.setTitle_('一剪即发 · 已恢复发布窗口 · 正在继续核对，完成后自动关闭')
    return True


class ScheduledWindow(DouyinCapabilityWindow):
    def __init__(self,*args,video,form,scheduled_iso,before_final,on_session=None,on_expired=None,account_name='所选账号',**kwargs):
        super().__init__(*args,**kwargs)
        self.video=video;self.form=form;self.scheduled_iso=scheduled_iso;self.before_final=before_final
        self.phase='baseline';self.upload_consumed=False;self.final_attempted=False
        self.previous=None;self.matches=0
        self.on_session=on_session;self.on_expired=on_expired;self.account_name=account_name
        self.session_saved=False;self.reauth_needed=False;self.receipt_confirmed=False

    def show(self):
        super().show()
        self.window.setTitle_('一剪即发 · 正在核对定时任务')

    def open_panel(self,parameters,frame,reply):
        if self.finished or self.phase!='prepare' or self.upload_consumed or self.pending_reply or not frame.isMainFrame() or parameters.allowsMultipleSelection():reply(None);return
        self.pending_reply=reply
        def checked(raw):
            callback=self.pending_reply;self.pending_reply=None
            if callback is None:return
            try:
                if self.finished or raw is None or raw.get('path')!=UPLOAD_PATH:raise ValueError('not empty upload')
                verified_identity(dict(raw['identity']),expected_uid=self.expected_uid)
                path=Path(self.video['path'])
                if not path.is_file() or sha256_file(path)!=self.video['content_hash']:raise ValueError('file changed')
                from Foundation import NSURL
                self.upload_consumed=True
                callback([NSURL.fileURLWithPath_(str(path))])
            except Exception:callback(None)
        self.read_page(checked)

    def observe(self):
        if self.finished or self.view is None:return
        # A pending WebKit callback must not bypass the native deadline.
        if time.monotonic()-self.started>300:
            self.complete(confirmed=self.receipt_confirmed,error=None if self.receipt_confirmed else 'submission_timeout');return
        if restore_publishing_window(getattr(self,'window',None)):return
        if self.busy:return
        if self.session_observer and not self.session_observer.closed and self.session_observer.busy:
            if time.monotonic()-self.session_observer.attempt_started<35:return
            self.session_observer.close();self.session_observer=None
        if self.receipt_confirmed:
            self.capture_session(on_saved=lambda:self.complete(confirmed=True));return
        import WebKit
        self.busy=True;generation=self.generation
        code=PREPARE_FORM if self.phase=='prepare' else READ_RESULT
        def done(value,error):
            self.busy=False
            if self.finished or generation!=self.generation:return
            if error is not None:return
            try:
                raw=dict(value)
                identity=dict(raw.get('identity') or {})
                if identity.get('status')==8 or identity.get('profile_status')==8 or identity.get('after_status')==8:
                    if not self.reauth_needed:
                        self.reauth_needed=True;self.session_saved=False
                        if self.session_observer:self.session_observer.close();self.session_observer=None
                        if self.on_expired:self.on_expired()
                    self.window.setTitle_('一剪即发 · '+self.account_name+'登录已失效，请扫码恢复此账号 · '+('仅核对结果，不重复提交' if self.final_attempted else '尚未提交'))
                    return
                # Network observations can be incomplete while navigation/login
                # settles. Wait without uploading or claiming a final action.
                if any(identity.get(k)!=0 for k in ('status','profile_status','after_status')):
                    self.window.setTitle_('一剪即发 · 等待账号状态读取 · 尚未提交' if not self.final_attempted else '一剪即发 · 正在核对账号及提交结果')
                    return
                verified_identity(identity,expected_uid=self.expected_uid)
                if not self.session_saved:
                    self.capture_session();return
                if self.phase=='prepare' and raw.get('failure_code')=='submission_topic_unavailable':
                    self.complete(error='submission_topic_unavailable');return
                if not raw.get('ready'):
                    if self.phase=='prepare':self.window.setTitle_('一剪即发 · '+str(raw.get('stage','等待表单'))+' · 尚未提交')
                    return
                if self.phase=='prepare':
                    verify_prepared_form(raw,self.form)
                    # Re-evaluate the moving 2.5-hour/13-day window immediately
                    # before claiming the final action.
                    validate_form_request(expected_uid=self.expected_uid,title=self.form['title_text'],body=self.form['plain_body_text'],tags=' '.join('#'+n for n in self.form['topic_names']),
                        declaration=self.form['declaration_label'],scheduled_time_text=self.form['scheduled_time_text'])
                    if not self.upload_consumed:raise ValueError('upload not bound')
                    self.before_final();self.final_attempted=True;self.phase='result'
                    self.window.setTitle_('一剪即发 · 已提交动作，正在核对定时结果')
                    self.busy=True
                    def clicked(value,error):
                        self.busy=False
                        if error is not None and not self.finished:self.window.setTitle_('一剪即发 · 提交结果待核实，正在回读后台')
                    self.view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(FINAL_CLICK,self.form,None,WebKit.WKContentWorld.defaultClientWorld(),clicked)
                    return
                rows=[dict(r) for r in raw.get('matches',[])]
                self.matches=self.matches+1 if rows==self.previous else 1;self.previous=rows
                if self.matches<2:return
                if self.phase=='baseline':
                    if matching_schedule_rows(rows,self.form):raise ValueError('existing matching platform work')
                    self.phase='prepare';self.previous=None;self.matches=0
                    from Foundation import NSURL,NSURLRequest
                    self.view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_('https://'+HOST+UPLOAD_PATH)))
                    self.window.setTitle_('一剪即发 · 上传视频并填写定时安排')
                    return
                if verified_scheduled_receipt(raw,self.expected_uid,self.form):
                    self.receipt_confirmed=True
                    self.session_observer=None
                    self.capture_session(on_saved=lambda:self.complete(confirmed=True))
            except (KeyError,TypeError,ValueError) as exc:
                code={'identity not verified':'submission_identity_unavailable','identity changed or unavailable':'submission_identity_unavailable','unexpected account identity':'submission_account_mismatch','topic readback mismatch':'submission_topic_mismatch','form readback mismatch':'submission_form_mismatch','existing matching platform work':'submission_existing_work','schedule must be 2.5 hours to 13 days ahead':'submission_schedule_invalid'}.get(str(exc),'submission_check_failed')
                self.complete(error=code)
            except Exception:self.complete(error='submission_transport_failed')
        self.view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(code,self.form,None,WebKit.WKContentWorld.defaultClientWorld(),done)

    def capture_session(self,on_saved=None):
        """Read the window's own session, with identity checks on both sides."""
        import WebKit
        if self.session_observer is None:
            def identity(reply):
                def done(value,error):
                    try:reply(dict(value) if error is None else None)
                    except (TypeError,ValueError):reply(None)
                self.view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(
                    READ_IDENTITY+'return identity;',{},None,WebKit.WKContentWorld.defaultClientWorld(),done)
            def cookies(reply):self.view.configuration().websiteDataStore().httpCookieStore().getAllCookies_(reply)
            def encode(rows,uid):
                return encode_cookies([c for c in rows if str(c.domain()).lstrip('.').lower() in DOMAINS],identity=uid,domains=DOMAINS)
            self.session_observer=SessionObservation(identity,cookies,encode,expected_uid=self.expected_uid)
        def captured(value):
            if self.finished:return
            try:
                if self.on_session:self.on_session({**value,'needs_persist':True})
            except Exception:
                self.complete(confirmed=self.receipt_confirmed,error=None if self.receipt_confirmed else 'session_refresh_failed');return
            self.session_saved=True
            if on_saved:
                on_saved();return
            if self.reauth_needed:
                self.reauth_needed=False
                if self.phase=='prepare' and self.upload_consumed:
                    self.complete(error='session_expired_during_prepare');return
                from Foundation import NSURL,NSURLRequest
                path=UPLOAD_PATH if self.phase=='prepare' else '/creator-micro/content/manage'
                self.view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_('https://'+HOST+path)))
                self.window.setTitle_('一剪即发 · '+self.account_name+'登录已恢复 · '+('正在核对结果，不重复提交' if self.final_attempted else '继续检查安排'))
        self.session_observer.poll(captured)

    def complete(self,*,confirmed=False,error=None):
        evidence=SubmissionEvidence(self.final_attempted,
            platform_schedule_confirmation=self.scheduled_iso if confirmed else None,error_code=error)
        self.allow_close=True
        self.finish(evidence,None)

    def closed(self):
        # Capability checks accept only upload/form URLs and discard results
        # elsewhere. Scheduled receipts deliberately finish on content/manage.
        # Keep auth timer/session cleanup without that capability-only gate.
        if self.finished:return
        if self.pending_reply:
            callback=self.pending_reply;self.pending_reply=None;callback(None)
        from .douyin_auth import DouyinAuthWindow
        DouyinAuthWindow.closed(self)

    def request_close(self):
        if self.allow_close or self.finished:return True
        self.complete(error='submission_window_closed');return False


def make_scheduled_transport(engine,paths,expected_uid):
    # Keep refreshed cookies in this batch. Commit after execution has stopped,
    # so frozen session-reference checks cannot be invalidated mid-submission.
    refreshed={}
    def submit(account,video,form,scheduled_iso,*,before_final,cancel):
        from yijianjifa.features.accounts.service import AccountService
        from yijianjifa.features.accounts.session_store import SessionStore
        current=AccountService(engine).get(account['id'])
        if current.platform_user_id!=expected_uid or form['expected_uid']!=expected_uid:raise ValueError('approved account mismatch')
        cached=refreshed.get(current.id)
        blob=cached['session_blob'] if cached and cached['original_ref']==current.session_ref else SessionStore(paths.data_dir/'profiles').get(current.id,ref=current.session_ref)
        cookies=decode_cookies(blob,identity=expected_uid,domains=DOMAINS)
        def capture(value):
            AccountService(engine).assert_same_identity(current.id,value['platform_user_id'])
            AccountService(engine).set_login_state(current.id,'valid')
            refreshed[current.id]={**value,'original_ref':current.session_ref,'needs_persist':value.get('needs_persist',False) or refreshed.get(current.id,{}).get('needs_persist',False)}
        def expired():
            refreshed.pop(current.id,None)
            AccountService(engine).set_login_state(current.id,'expired')
        holder={}
        def factory(*args):
            window=ScheduledWindow(*args,expected_uid=expected_uid,restored_cookies=cookies,video=video,form=form,scheduled_iso=scheduled_iso,before_final=before_final,on_session=capture,on_expired=expired,account_name=current.nickname)
            holder['window']=window;return window
        try:
            result=CocoaAuthProvider('https://'+HOST+'/creator-micro/content/manage',{HOST},lambda *a:None,window_factory=factory)(account['id'],cancel)
            if isinstance(result,SubmissionEvidence):return result
        except Exception:pass
        return SubmissionEvidence(bool(holder.get('window') and holder['window'].final_attempted),error_code='submission_transport_closed')
    def flush_sessions():
        persist_refreshed_sessions(engine,paths,refreshed)
    submit.flush_sessions=flush_sessions
    submit.expected_uid=expected_uid
    submit.supports_topics=True
    return submit


def persist_refreshed_sessions(engine,paths,refreshed):
    from sqlalchemy import text
    from yijianjifa.features.accounts.service import AccountService, AccountRow
    from yijianjifa.features.accounts.session_store import SessionStore
    from yijianjifa.storage.db import session_scope
    from .login_observation import can_refresh_login_session
    service=AccountService(engine);store=SessionStore(paths.data_dir/'profiles')
    for account_id,value in list(refreshed.items()):
        current=service.get(account_id)
        service.assert_same_identity(account_id,value['platform_user_id'])
        if value.get('needs_persist',True) and current.session_ref==value['original_ref']:
            with session_scope(engine) as db:
                db.execute(text('BEGIN IMMEDIATE'))
                row=db.get(AccountRow,account_id)
                if (row is not None and row.login_state!='removed' and row.platform_user_id==value['platform_user_id']
                        and row.session_ref==value['original_ref']
                        and can_refresh_login_session(db,row,value.get('login_observed_at'))):
                    previous_reference = row.session_ref
                    row.session_ref=store.put(account_id,value['session_blob'],versioned=True)
                    from yijianjifa.features.accounts.session_rotation import record_rotation
                    record_rotation(db,row,previous_reference,row.session_ref)
                    if value.get('login_observed_at') is not None:
                        service.record_login_check(db,row,'valid',observed_at=value['login_observed_at'])
                    else:row.login_state='valid'
        refreshed.pop(account_id,None)
