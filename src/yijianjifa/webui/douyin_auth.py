"""Opt-in native Douyin authentication. Never enables publishing.

Identity routes/fields observed in the authorized creator homepage on 2026-09-27.
No page-supplied URL, script, cookie or identity is accepted from the app bridge.
"""
import re
import time
from urllib.parse import urlsplit
from .cocoa_auth import CocoaAuthProvider, CocoaAuthWindow
from .cocoa_session import encode_cookies, decode_cookies

HOST = 'creator.douyin.com'
DOMAINS = frozenset({'douyin.com', HOST})
IDENTITY_SCRIPT = '''
if (location.origin !== 'https://creator.douyin.com') return null;
async function read(path) {
  const r = await fetch(path, {credentials:'same-origin',cache:'no-store',redirect:'error',signal:AbortSignal.timeout(8000)});
  if (!r.ok) throw new Error('identity unavailable');
  return await r.json();
}
const a = await read('/aweme/v1/creator/pc/user/info/');
const b = await read('/aweme/v1/creator/user/info/');
const ordinaryEntry = () => Array.from(document.querySelectorAll('body *')).some(e=>e.getClientRects().length && e.textContent.trim()==='发布高清视频');
for (let i=0;i<30 && !ordinaryEntry();i++) await new Promise(resolve=>setTimeout(resolve,100));
const c = await read('/aweme/v1/creator/pc/user/info/');
return {uid:a.uid,uid_after:c.uid,status:a.status_code,
        profile_status:b.status_code,nickname:b.user_profile?.nick_name,
        handle:b.user_profile?.unique_id,after_status:c.status_code,
        avatar_url:b.user_profile?.avatar_url || b.user_profile?.avatar?.url_list?.[0] || b.user_profile?.avatar || b.user_profile?.avatar_thumb?.url_list?.[0] || '',
        ordinary_available:ordinaryEntry()};
'''


def verified_identity(value, *, expected_uid):
    if not isinstance(value,dict) or value.get('status')!=0 or value.get('profile_status')!=0 or value.get('after_status')!=0:
        raise ValueError('identity not verified')
    uid=value.get('uid');nickname=value.get('nickname')
    if not isinstance(uid,str) or not re.fullmatch(r'[0-9]{1,32}',uid) or value.get('uid_after')!=uid:
        raise ValueError('identity changed or unavailable')
    if (expected_uid is not None and uid!=expected_uid) or not isinstance(nickname,str) or not 1<=len(nickname.strip())<=255:
        raise ValueError('unexpected account identity')
    return str(uid),str(nickname).strip()


class SessionObservation:
    """Callback sequencing isolated for tests; never returns raw provider errors."""
    def __init__(self, read_identity, read_cookies, encode, *, expected_uid, progress=lambda stage:None):
        self.read_identity=read_identity;self.read_cookies=read_cookies;self.encode=encode
        self.expected_uid=expected_uid;self.busy=False;self.closed=False
        self.progress=progress;self.attempt_started=0

    def close(self): self.closed=True

    def poll(self, complete):
        if self.closed or self.busy:return
        self.busy=True;self.attempt_started=time.monotonic();self.progress("正在读取账号身份")
        def observed(value):
            if self.closed:return
            try: identity,nickname=verified_identity(value,expected_uid=self.expected_uid)
            except ValueError:self.busy=False;self.progress("等待有效账号身份 · 请保持此页打开");return
            self.progress("身份已识别 · 正在保存登录会话")
            def captured(cookies):
                if self.closed:return
                try: blob=self.encode(cookies,identity)
                except Exception:self.busy=False;self.progress("登录会话读取失败 · 正在重试");return
                self.progress("会话已读取 · 正在再次核对账号")
                def verified(again):
                    if self.closed:return
                    try: current,current_name=verified_identity(again,expected_uid=identity)
                    except ValueError:self.busy=False;return
                    self.closed=True
                    result=dict(platform_user_id=current,nickname=current_name,session_blob=blob)
                    if 'ordinary_available' in again:result['ordinary_available']=again.get('ordinary_available') is True
                    if isinstance(again.get('avatar_url'),str):result['avatar_url']=str(again['avatar_url'])
                    complete(result)
                try:self.read_identity(verified)
                except Exception:self.busy=False
            try:self.read_cookies(captured)
            except Exception:self.busy=False
        try:self.read_identity(observed)
        except Exception:self.busy=False


class DouyinAuthWindow(CocoaAuthWindow):
    def __init__(self,*args,expected_uid,restored_cookies=(),keep_open=False):
        super().__init__(*args)
        self.keep_open=keep_open
        self.expected_uid=expected_uid;self.restored_cookies=tuple(restored_cookies)
        self.timer=None;self.started=time.monotonic();self.session_observer=None

    def show(self):
        super().show()
        if self.keep_open:self.window.setTitle_('一剪即发 · 抖音后台')

    def verified(self,value):
        if self.finished:return
        if self.keep_open:
            self.value=value
            if self.timer:self.timer.invalidate();self.timer=None
        else:self.finish(value,None)

    def load_initial(self):
        def load():
            if self.finished:return
            super(DouyinAuthWindow,self).load_initial()
            import Foundation
            self.timer=Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(2.0,True,lambda timer:self.observe())
        store=self.view.configuration().websiteDataStore().httpCookieStore()
        pending=list(self.restored_cookies)
        def next_cookie():
            if self.finished:return
            if pending:store.setCookie_completionHandler_(pending.pop(),next_cookie)
            else:load()
        next_cookie()

    def observe(self):
        if self.finished or self.view is None:return
        if time.monotonic()-self.started>600:self.fail();return
        url=urlsplit(str(self.view.URL() or ''))
        if url.scheme!='https' or url.hostname!=HOST or not url.path.startswith('/creator-micro/'):return
        observer=self.session_observer
        generation=getattr(self,'generation',0)
        if observer is not None and (getattr(self,'observer_generation',generation)!=generation or
                (observer.busy and time.monotonic()-observer.attempt_started>35)):
            observer.close();self.session_observer=None
        if self.session_observer is None:
            self.observer_generation=generation
            def progress(stage):
                if self.finished:return
                if self.window is not None:self.window.setTitle_('一剪即发 · '+stage)
            def read_identity(reply):
                import WebKit
                def completed(value,error):
                    if self.finished:return
                    # Foundation dictionaries are normalized without logging them.
                    try:
                        result=dict(value) if error is None and value is not None else None
                    except (TypeError, ValueError):
                        # JavaScript null becomes Foundation.NSNull during navigation.
                        # Never let conversion errors escape into the Cocoa event loop.
                        result=None
                    if result is None:
                        progress('身份接口暂未返回 · 自动重试中')
                    reply(result)
                self.view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(
                    IDENTITY_SCRIPT,{},None,WebKit.WKContentWorld.defaultClientWorld(),completed)
            def read_cookies(reply):
                self.view.configuration().websiteDataStore().httpCookieStore().getAllCookies_(reply)
            def encode(rows,identity):
                selected=[c for c in rows if str(c.domain()).lstrip('.').lower() in DOMAINS]
                return encode_cookies(selected,identity=identity,domains=DOMAINS)
            self.session_observer=SessionObservation(read_identity,read_cookies,encode,expected_uid=self.expected_uid,progress=progress)
        self.session_observer.poll(self.verified)

    def closed(self):
        if self.session_observer:self.session_observer.close()
        if self.timer:self.timer.invalidate();self.timer=None
        super().closed()


def make_douyin_provider(engine,paths,*,expected_uid=None):
    if expected_uid is not None and (type(expected_uid) is not str or not re.fullmatch(r'[0-9]{1,32}',expected_uid)):
        raise ValueError('approved account identity required')
    def run(account_id,cancel,keep_open=False):
        from yijianjifa.features.accounts.service import AccountService
        from yijianjifa.features.accounts.session_store import SessionStore
        cookies=()
        login_uid=None  # Adding an account discovers its identity; publishing remains separately gated.
        if account_id:
            account=AccountService(engine).get(account_id)
            if account.platform!='douyin' or (keep_open and expected_uid is not None and account.platform_user_id!=expected_uid):
                raise ValueError('approved account mismatch')
            login_uid=account.platform_user_id
            blob=SessionStore(paths.data_dir/'profiles').get(account.id,ref=account.session_ref) if account.session_ref else None
            if blob:cookies=decode_cookies(blob,identity=login_uid,domains=DOMAINS)
        def factory(*args):return DouyinAuthWindow(*args,expected_uid=login_uid,restored_cookies=cookies,keep_open=keep_open)
        result=CocoaAuthProvider('https://creator.douyin.com/creator-micro/home',{HOST},lambda *args:None,
                                 window_factory=factory)(account_id,cancel)
        if result is not None:
            from yijianjifa.features.accounts.avatar import fetch_avatar
            url=result.pop('avatar_url','')
            result['avatar_data']=fetch_avatar(url)
        return result
    from .chromium_publish import make_chromium_login
    provider=make_chromium_login(engine,paths)
    from .douyin_metadata import make_metadata_provider
    provider.metadata=make_metadata_provider(engine,paths,expected_uid)
    from .chromium_publish import make_chromium_transport
    provider.scheduled_publish=make_chromium_transport(engine,paths,expected_uid)
    from .chromium_multi_publish import ChromiumMultiPublishAdapter
    from .douyin_image_publish import DouyinImagePostAdapter
    from .creator_providers import make_creator_publish_adapters
    provider.publish_adapters={'douyin':ChromiumMultiPublishAdapter(engine,paths,expected_uid=expected_uid),
                               'douyin:image_post':DouyinImagePostAdapter(engine,paths,expected_uid=expected_uid),
                               **make_creator_publish_adapters(engine,paths)}
    def open_backend(account_id,cancel):
        from yijianjifa.features.accounts.service import AccountService
        account=AccountService(engine).get(account_id)
        if account.platform!='douyin' or (expected_uid is not None and account.platform_user_id!=expected_uid):
            raise ValueError('approved account mismatch')
        return make_chromium_login(engine,paths,backend=True)(account_id,cancel)
    provider.open_backend=open_backend
    from .douyin_capability import make_capability_provider
    provider.check_capabilities=make_capability_provider(engine,paths,expected_uid)
    return provider
