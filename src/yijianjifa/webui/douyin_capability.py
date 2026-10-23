"""Explicitly consented one-file probe in the account's isolated WKWebView.

No submit actions. The user opens the upload/location controls in this window.
The only file the native chooser can return is the hash-checked bundled fixture.
"""
import time
from urllib.parse import urlsplit
from .douyin_auth import DouyinAuthWindow, HOST, DOMAINS, verified_identity
from .cocoa_auth import CocoaAuthProvider
from .cocoa_session import decode_cookies
from yijianjifa.features.accounts.capability_probe import fixture_path

UPLOAD_PATH='/creator-micro/content/upload'
PUBLISH_PATH='/creator-micro/content/post/video'
READ_IDENTITY='''
if(location.origin!=='https://creator.douyin.com') return null;
async function read(path){const r=await fetch(path,{credentials:'same-origin',cache:'no-store',redirect:'error',signal:AbortSignal.timeout(8000)});if(!r.ok)throw Error('identity unavailable');return await r.json();}
const a=await read('/aweme/v1/creator/pc/user/info/');
const b=await read('/aweme/v1/creator/user/info/');
const c=await read('/aweme/v1/creator/pc/user/info/');
const identity={uid:a.uid,uid_after:c.uid,status:a.status_code,profile_status:b.status_code,after_status:c.status_code,nickname:b.user_profile?.nick_name};
'''
# A success toast is transient; retain only its boolean evidence in the isolated
# client world. No cookies, page content or app bridge are exposed to the page.
SUCCESS_OBSERVER = """
(()=>{let seen=false;
 const check=()=>{if(location.origin!=='https://creator.douyin.com'||location.pathname!=='/creator-micro/content/post/video')return;
  if(Array.from(document.querySelectorAll('body *')).some(e=>e.getClientRects().length&&e.textContent.trim()==='上传成功'))seen=true;};
 Object.defineProperty(window,'p011UploadCompleted',{get:()=>seen});
 new MutationObserver(check).observe(document.documentElement,{subtree:true,childList:true,characterData:true});check();})();
"""
READ_PAGE=READ_IDENTITY+r'''
const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
const nodes=Array.from(document.querySelectorAll('body *')).filter(visible);
const exact=t=>nodes.some(e=>e.textContent.trim()===t);
const body=document.body.innerText;
return {identity,path:location.pathname,extension:exact('扩展信息'),commerce:exact('带货模式'),
 upload_success:window.p011UploadCompleted===true||exact('上传成功'),replace_upload:exact('重新上传'),
 upload_busy:nodes.some(e=>e.children.length===0&&/^(?:(?:正在上传|上传中)[.。\s\d%]*|上传失败)$/.test(e.textContent.trim())),
 scopes:nodes.filter(e=>['本地','国内'].includes(e.textContent.trim())&&e.children.length===0&&[e.parentElement,e.parentElement?.parentElement,e.parentElement?.parentElement?.parentElement].some(p=>p&&p.textContent.replace(/\s/g,'')==='本地国内')).map(e=>e.textContent.trim())};
'''


ADVANCE_PROBE=READ_IDENTITY+r'''
if(identity.uid!==expected_uid||identity.uid_after!==expected_uid||identity.status!==0||identity.profile_status!==0||identity.after_status!==0)return false;
const visible=e=>e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
const exact=t=>Array.from(document.querySelectorAll('body *')).find(e=>visible(e)&&e.children.length===0&&e.textContent.trim()===t);
const activate=e=>{e.scrollIntoView({block:'center'});e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));e.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));e.click();};
if(location.pathname==='/creator-micro/content/upload'&&!uploaded&&!window.p011CapabilityUploadStarted){
 const button=exact('上传视频');if(button){window.p011CapabilityUploadStarted=true;activate(button);}return true;
}
if(location.pathname==='/creator-micro/content/post/video'&&ready){
 const trigger=Array.from(document.querySelectorAll('body *')).reverse().find(e=>visible(e)&&e.textContent.replace(/\s/g,'')==='本地国内');
 if(trigger&&!visible(document.getElementById('local')))activate(trigger);
 else if(!trigger){const marker=exact('输入地理位置');if(marker)activate(marker);}
}
return true;
'''


def accepted_path(url):
    try:
        p=urlsplit(url)
        return p.scheme=='https' and p.hostname==HOST and p.path in {UPLOAD_PATH,PUBLISH_PATH} and not p.username and not p.password and p.port in (None,443)
    except ValueError:return False


class ProbeObservation:
    """Host-owned evidence, invalidated by identity change; one upload per attempt."""
    def __init__(self,uid):
        self.uid=uid;self.uploaded=False;self.failed=False;self.ready=False;self.commerce=False;self.scopes=set();self.last_verified=0

    def consume_upload(self,value):
        if self.uploaded or self.failed:raise ValueError('upload already consumed')
        verified_identity(value['identity'],expected_uid=self.uid)
        if value.get('path')!=UPLOAD_PATH or value.get('replace_upload') or value.get('extension'):
            raise ValueError('not an empty upload page')
        fixture_path()
        self.uploaded=True

    def observe(self,value):
        try:verified_identity(value['identity'],expected_uid=self.uid)
        except (KeyError,TypeError,ValueError):
            self.failed=True;raise ValueError('probe identity unavailable')
        self.last_verified=time.monotonic()
        if not self.uploaded or self.failed:return
        if value.get('path') not in {UPLOAD_PATH,PUBLISH_PATH}:return
        complete=value.get('upload_success') is True
        if value.get('path')==PUBLISH_PATH and complete and value.get('extension') is True and not value.get('upload_busy'):
            self.ready=True;self.commerce=value.get('commerce') is True
        # Scope observations only belong to the same verified, uploaded form.
        if self.ready and value.get('extension') is True:
            self.scopes.update({'本地':'local','国内':'national'}[s] for s in value.get('scopes',[]) if s in {'本地','国内'})

    def result(self):
        if not self.uploaded or self.failed or time.monotonic()-self.last_verified>5:return None
        return dict(uid_before=self.uid,uid_after=self.uid,upload_complete=self.ready,form_ready=self.ready,
                    commerce_visible=self.commerce,scope_options=sorted(self.scopes))


class DouyinCapabilityWindow(DouyinAuthWindow):
    def __init__(self,*args,expected_uid,restored_cookies=(),automatic=False):
        super().__init__(*args,expected_uid=expected_uid,restored_cookies=restored_cookies)
        self.automatic=automatic;self.ready_since=None;self.stable_result=None;self.stable_count=0
        self.probe=ProbeObservation(expected_uid);self.busy=False;self.pending_reply=None;self.allow_close=False;self.close_requested=False

    def configure(self,config):
        import WebKit
        script=WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_inContentWorld_(
            SUCCESS_OBSERVER,WebKit.WKUserScriptInjectionTimeAtDocumentEnd,True,WebKit.WKContentWorld.defaultClientWorld())
        config.userContentController().addUserScript_(script)

    def show(self):
        super().show()
        self.window.setTitle_('一剪即发 · 能力检查 · 仅上传测试片，不发布')

    def read_page(self,reply):
        import WebKit
        generation=self.generation
        def completed(value,error):
            if self.finished or generation!=self.generation:reply(None);return
            try:
                result=dict(value) if error is None and value is not None else None
            except (TypeError, ValueError):
                result=None  # WKWebView can return NSNull while the page is loading.
            reply(result)
        self.view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(
            READ_PAGE,{},None,WebKit.WKContentWorld.defaultClientWorld(),completed)

    def open_panel(self,parameters,frame,reply):
        if self.finished or self.pending_reply or self.probe.uploaded or not frame.isMainFrame() or parameters.allowsMultipleSelection() or not accepted_path(str(self.view.URL() or '')):
            reply(None);return
        self.pending_reply=reply
        def checked(value):
            callback=self.pending_reply;self.pending_reply=None
            if callback is None:return
            try:
                if self.finished or value is None:raise ValueError('unverified page')
                value['identity']=dict(value['identity'])
                self.probe.consume_upload(value)
                from Foundation import NSURL
                callback([NSURL.fileURLWithPath_(str(fixture_path()))])
                self.window.setTitle_('一剪即发 · 上传检测中 · 请展开位置/带货模式，勿发布')
            except Exception:callback(None)
        self.read_page(checked)

    def observe(self):
        if self.finished or self.view is None or self.busy:return
        if time.monotonic()-self.started>600:self.fail();return
        if not accepted_path(str(self.view.URL() or '')):self.value=None;return
        self.busy=True
        def read(value):
            self.busy=False
            if self.finished or value is None:return
            try:
                value['identity']=dict(value['identity']);value['path']=str(value.get('path',''))
                value['scopes']=[str(s) for s in value.get('scopes',[])]
                self.probe.observe(value)
                self.value=self.probe.result()
                if self.automatic:
                    self.advance_probe()
                elif self.probe.ready:self.window.setTitle_('一剪即发 · 带货入口已检查 · 展开位置查看范围，关闭窗口保存')
            except Exception:self.value=None
        self.read_page(read)

    def advance_probe(self):
        if self.probe.ready:
            if self.ready_since is None:self.ready_since=time.monotonic()
            self.stable_count=self.stable_count+1 if self.value==self.stable_result else 1
            self.stable_result=self.value
            if self.stable_count>=2 and (self.probe.scopes or time.monotonic()-self.ready_since>20):
                self.window.setTitle_('一剪即发 · 检查完成 · 正在关闭')
                self.finish(self.value,None);return
        import WebKit
        self.window.setTitle_('一剪即发 · '+('正在读取地点范围' if self.probe.ready else '正在上传并检查内置测试片'))
        self.view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(
            ADVANCE_PROBE,dict(expected_uid=self.expected_uid,uploaded=self.probe.uploaded,ready=self.probe.ready),
            None,WebKit.WKContentWorld.defaultClientWorld(),lambda value,error:None)

    def request_close(self):
        if self.allow_close or self.finished:return True
        if self.close_requested:return False
        self.close_requested=True
        def verified(value):
            result=None
            try:
                if value is not None and accepted_path(str(self.view.URL() or '')):
                    value['identity']=dict(value['identity']);value['path']=str(value.get('path',''))
                    value['scopes']=[str(s) for s in value.get('scopes',[])]
                    self.probe.observe(value);result=self.probe.result()
            except Exception:pass
            self.finish(result,None)
        self.read_page(verified)
        return False

    def finish(self,value,error):
        self.allow_close=True
        super().finish(value,error)

    def closed(self):
        if self.finished:return
        if self.pending_reply:
            callback=self.pending_reply;self.pending_reply=None;callback(None)
        if self.error or not self.view or not accepted_path(str(self.view.URL() or '')):self.value=None
        super().closed()


def make_capability_provider(engine,paths,expected_uid):
    def probe(account_id,cancel,path):
        from yijianjifa.features.accounts.service import AccountService
        from yijianjifa.features.accounts.session_store import SessionStore
        if path!=fixture_path():raise ValueError('fixed probe file required')
        account=AccountService(engine).get(account_id)
        if account.platform!='douyin' or not account.session_ref:
            raise ValueError('approved account with saved session required')
        account_uid=account.platform_user_id
        blob=SessionStore(paths.data_dir/'profiles').get(account.id,ref=account.session_ref)
        cookies=decode_cookies(blob,identity=account_uid,domains=DOMAINS)
        def factory(*args):return DouyinCapabilityWindow(*args,expected_uid=account_uid,restored_cookies=cookies,automatic=True)
        return CocoaAuthProvider('https://'+HOST+UPLOAD_PATH,{HOST},lambda *args:None,window_factory=factory)(account_id,cancel)
    return probe
