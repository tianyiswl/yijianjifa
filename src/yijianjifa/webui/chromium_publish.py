"""Account-isolated Chromium transport; no dependency on Cocoa window visibility.

No stealth flags, shared daily-browser profile, verification logging or automatic
retry after the final journal. Challenges remain human-controlled.
"""
import threading
import sys
import time
from pathlib import Path
from .publish_intervention import broker
from .publish_progress import progress
from .douyin_auth import HOST, DOMAINS, verified_identity
from .douyin_capability import READ_IDENTITY, SUCCESS_OBSERVER, UPLOAD_PATH, PUBLISH_PATH
from .douyin_publish_form import PREPARE_FORM, verify_prepared_form, validate_form_request
from .douyin_scheduled import READ_RESULT, FINAL_CLICK, matching_schedule_rows, verified_scheduled_receipt, persist_refreshed_sessions
from .cocoa_session import decode_cookies, encode_cookies
from yijianjifa.features.publish.executor import SubmissionEvidence
from yijianjifa.features.library.media_identity import sha256_file
from .login_observation import observe_login_read, challenge_login_state, douyin_login_state

_ACCOUNT_LOCKS={}
_LOCK=threading.Lock()

READ_LOGIN_IDENTITY=READ_IDENTITY+'''
const profile=b.user_profile||{};
const candidates=[profile.avatar_thumb,profile.avatar_url,profile.avatar,profile.avatar_medium,profile.avatar_larger]
 .flatMap(v=>typeof v==='string'?[v]:Array.isArray(v?.url_list)?v.url_list:[]).filter(v=>typeof v==='string');
const normalized=v=>{try{
 const u=new URL(v,location.origin),authority=String(v).trim().replace(/^https?:/i,'').split('/')[2]||'';
 const explicitPort=authority.match(/:([0-9]+)$/)?.[1];
 if(!['http:','https:'].includes(u.protocol)||u.username||u.password||
    (explicitPort&&Number(explicitPort)!==443)||(u.port&&u.port!=='443'))return null;
 // Chromium upgrades ordinary HTTP avatars on this HTTPS login page. Match
 // that same resource; credentials and alternate ports remain disallowed.
 u.protocol='https:';return u;
}catch(e){return null}};
const urls=candidates.map(normalized).filter(Boolean);
const loaded=Array.from(document.images||[]).filter(i=>i.complete&&i.naturalWidth>0).map(i=>normalized(i.currentSrc||i.src)).filter(Boolean);
identity.avatar_url=loaded.find(u=>urls.some(c=>u.host===c.host&&u.pathname===c.pathname))?.href||urls[0]?.href||'';
'''

# Individual request aborts do not bound the entire identity observation. Race
# the three reads against one timer and abort outstanding reads on completion.
READ_BOUNDED_LOGIN_IDENTITY='''
const loginAbort=new AbortController();let loginTimer;
try {
 return await Promise.race([
  (async()=>{'''+READ_LOGIN_IDENTITY.replace('AbortSignal.timeout(8000)','loginAbort.signal')+'''
   return identity;
  })(),
  new Promise((_,reject)=>{loginTimer=setTimeout(()=>{loginAbort.abort();reject(Error('login-identity-timeout'));},loginBudgetMs);})
 ]);
} finally {clearTimeout(loginTimer);loginAbort.abort();}
'''


def _same_login_avatar(before,after):
    """Only known Douyin image shards may alias one exact avatar resource."""
    from .loaded_avatar import _avatar_url
    before,after=_avatar_url(before),_avatar_url(after)
    if not before or not after:return False
    if before==after:return True
    import re
    from urllib.parse import urlsplit
    first,second=urlsplit(before),urlsplit(after)
    if any(part.username is not None or part.password is not None for part in (first,second)):return False
    shard=r'p[0-9]+(?:-pc)?\.douyinpic\.com'
    return (re.fullmatch(shard,first.hostname) is not None
            and re.fullmatch(shard,second.hostname) is not None
            and first.path==second.path and first.query==second.query)


def observe_login_session(page,context,expected_uid=None,*,avatar_reader=None,on_expired=None,identity_reader=None):
    def read():
        operation=lambda:evaluate_script(page,READ_BOUNDED_LOGIN_IDENTITY,{'loginBudgetMs':8000})
        return identity_reader(operation) if identity_reader else operation()
    value=read()
    if expected_uid and identity_session_expired(value or {}) and on_expired:on_expired()
    uid,name=verified_identity(value,expected_uid=expected_uid)
    avatar=avatar_reader(value.get('avatar_url','')) if avatar_reader else ''
    if avatar_reader is not None and not avatar:
        # A slow/cached image may not appear in requestfinished. This existing
        # public-CDN reader is bounded, carries no cookies and rejects redirects.
        from yijianjifa.features.accounts.avatar import fetch_avatar
        avatar=fetch_avatar(value.get('avatar_url',''))
    blob=session_blob(context,uid)
    again=read()
    current,current_name=verified_identity(again,expected_uid=uid)
    result=dict(platform_user_id=current,nickname=current_name,session_blob=blob,
                ordinary_available=page.get_by_text('发布高清视频',exact=True).count()>0)
    if avatar and _same_login_avatar(value.get('avatar_url'),again.get('avatar_url')):
        result['avatar_data']=avatar
    return result


def account_lock(account_id):
    with _LOCK:return _ACCOUNT_LOCKS.setdefault(account_id,threading.Lock())


def browser_options(mode='background'):
    if mode not in {'background','visible'}:raise ValueError('invalid browser mode')
    options=dict(headless=mode=='background',channel='chromium',viewport={'width':1440,'height':1000},accept_downloads=False)
    import sys
    if getattr(sys,'_MEIPASS',None):
        from yijianjifa.os_integration.resources import resource_path
        import os
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(resource_path('playwright-browsers'))
    return options


def evaluate_script(page, code, arguments):
    names=','.join(arguments)
    return page.evaluate('async (args)=>{const {'+names+'}=args;'+code+'}',arguments)


def challenge_kind(value):
    return next((kind for key,kind in [('sms','sms'),('complex','complex'),('login','login')] if value.get(key)),None)


CHALLENGE=r'''() => {
 const visible=e=>{
   if (!e?.isConnected || !Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0)) return false;
   for (let node=e;node;node=node.parentElement) {
     const style=getComputedStyle(node);
     if (style.display==='none' || style.visibility==='hidden' || style.visibility==='collapse' || Number(style.opacity)<=0) return false;
   }
   // Do not require viewport intersection: an offscreen verification control
   // still matters, including in the background browser.
   return true;
 };
 const sms=Array.from(document.querySelectorAll('input')).filter(e=>visible(e)&&/验证码/.test([e.placeholder,e.getAttribute('aria-label')].join(' ')));
 const complex=Array.from(document.querySelectorAll('iframe,[class*="captcha"],[id*="captcha"]')).some(e=>visible(e)&&/captcha|verify|验证/i.test([e.src,e.id,e.className].join(' ')));
 const login=/\/login(?:\/|$)/.test(location.pathname)||Array.from(document.querySelectorAll('body *')).some(e=>visible(e)&&e.children.length===0&&['扫码登录','手机号登录'].includes(e.textContent.trim()));
 return {sms:sms.length===1,complex,login};
}'''


def native_to_chromium(blob, uid):
    if sys.platform == "win32":
        from .portable_session import decode_session
        return decode_session(blob,uid,DOMAINS)
    rows=[]
    for c in decode_cookies(blob,identity=uid,domains=DOMAINS):
        r=dict(name=str(c.name()),value=str(c.value()),domain=str(c.domain()),path=str(c.path()),secure=bool(c.isSecure()),httpOnly=bool(c.isHTTPOnly()))
        expiry=c.expiresDate()
        if expiry is not None:r['expires']=float(expiry.timeIntervalSince1970())
        rows.append(r)
    return rows


def session_blob(context,uid):
    if sys.platform == "win32":
        from .portable_session import encode_session
        return encode_session(context.cookies(),uid,DOMAINS)
    import Foundation as F
    rows=[]
    for c in context.cookies():
        if c['domain'].lstrip('.').lower() not in DOMAINS:continue
        p={F.NSHTTPCookieName:c['name'],F.NSHTTPCookieValue:c['value'],F.NSHTTPCookieDomain:c['domain'],F.NSHTTPCookiePath:c['path']}
        if c.get('secure'):p[F.NSHTTPCookieSecure]='TRUE'
        if c.get('httpOnly'):p['HttpOnly']='TRUE'
        if c.get('expires',-1)>0:p[F.NSHTTPCookieExpires]=F.NSDate.dateWithTimeIntervalSince1970_(c['expires'])
        rows.append(F.NSHTTPCookie.cookieWithProperties_(p))
    return encode_cookies(rows,identity=uid,domains=DOMAINS)


def identity_session_expired(identity):
    # A feature/profile endpoint denial is not proof of account logout.
    return identity.get('status') == 8 and identity.get('after_status') == 8


def restore_account_session(context, store, account, profile, uid):
    """One source rule for login and publishing; never replay an unchanged archive."""
    marker = profile / '.source-ref'
    imported = marker.read_text() if marker.is_file() else None
    if account.session_ref:
        blob = store.get(account.id, ref=account.session_ref)
        if blob:
            rows = native_to_chromium(blob, uid)
            if imported == account.session_ref:
                # Chromium may discard session-only cookies across process exits.
                # Restore missing entries, but never overwrite rotated live values.
                present={(c['name'],c['domain'],c['path']) for c in context.cookies()}
                rows=[c for c in rows if (c['name'],c['domain'],c['path']) not in present]
            if rows:context.add_cookies(rows)
            marker.write_text(account.session_ref)
            marker.chmod(0o600)


def make_chromium_transport(engine,paths,expected_uid=None):
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.accounts.session_store import SessionStore
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    service=AccountService(engine);store=SessionStore(paths.data_dir/'profiles');refreshed={}
    driver=None;contexts={};held_locks={}
    from contextlib import contextmanager
    @contextmanager
    def batch_browser():
        nonlocal driver
        if driver is None:
            from .playwright_runtime import acquire_playwright
            driver=acquire_playwright()
        yield driver

    configured_uid=expected_uid
    def submit(account,video,form,scheduled_iso,*,before_final,cancel):
        current=service.get(account['id'])
        expected_uid=current.platform_user_id
        if (current.platform!='douyin' or not isinstance(expected_uid,str) or not expected_uid.isdecimal()
                or account.get('platform_user_id')!=expected_uid or form['expected_uid']!=expected_uid
                or (configured_uid is not None and configured_uid!=expected_uid)):
            raise ValueError('approved account mismatch')
        lock=account_lock(current.id)
        if current.id not in held_locks:
            from yijianjifa.features.publish.account_wait import wait_for_account
            if not wait_for_account(lock,cancel,lambda:progress.update(form.get('_task_id'),stage='等待账号空闲，前一操作结束后自动继续',title=form.get('title_text',''))):
                return SubmissionEvidence(False,error_code='submission_verification_cancelled')
            held_locks[current.id]=lock
        final_attempted=False;context=None;request_id=None;login_observation={}
        def remember_login(state,observed_at):
            login_observation.update(state=state,observed_at=observed_at)
        def login_read(read,*,identity=lambda value:value,challenge=False):
            classify=challenge_login_state if challenge else lambda value:douyin_login_state(identity(value),expected_uid)
            return observe_login_read(service,current.id,expected_uid,current.session_ref,read,
                                      classify=classify,on_observed=remember_login)
        def read_challenge():
            return login_read(lambda:challenge_kind(page.evaluate(CHALLENGE)),challenge=True)
        def valid_login_time():
            return login_observation.get('observed_at') if login_observation.get('state')=='valid' else None
        try:
            import json
            with session_scope(engine) as s:
                row=s.get(Setting,'publish.browser_mode');mode=json.loads(row.value_json) if row else 'background'
            # Configure bundled runtime paths before the first SDK starts.
            initial_options=browser_options(mode)
            profile=chromium_profile(store,expected_uid)
            with batch_browser() as pw:
                def launch(visible=False):
                    nonlocal context
                    selected_mode='visible' if visible else mode
                    cached=contexts.get(current.id)
                    if cached and cached[0]==selected_mode:context=cached[1]
                    else:
                        if cached:
                            cached[1].close();contexts.pop(current.id,None);context=None
                        context=pw.chromium.launch_persistent_context(str(profile),**(initial_options if selected_mode==mode else browser_options(selected_mode)))
                        contexts[current.id]=(selected_mode,context)
                    context.set_default_timeout(15000)
                    page=context.pages[0] if context.pages else context.new_page()
                    page.add_init_script(SUCCESS_OBSERVER.replace('new MutationObserver(check).observe(document.documentElement', 'new MutationObserver(check).observe(document'))
                    return page
                progress.update(form.get('_task_id'),stage='正在打开发布浏览器',title=form['title_text'])
                page=launch();headed=mode=='visible'
                # Import only this app's identity-bound legacy archive. Do not
                # inspect or import any daily Chrome profile. Native relogin
                # can refresh cookies, without deleting persistent localStorage.
                restore_account_session(context,store,current,profile,expected_uid)
                page.goto('https://'+HOST+'/creator-micro/content/manage',wait_until='domcontentloaded')
                phase='baseline';last=None;stable=0;upload=False;started=time.monotonic();error='submission_timeout'
                def show_login_browser():
                    nonlocal page,headed,phase,last,stable
                    if upload or final_attempted:return False
                    if not headed or page.is_closed():
                        # An untouched login page can move to a real window.
                        # Never recreate a filled form or replay a final click.
                        if page.is_closed():
                            cached=contexts.get(current.id)
                            if cached:
                                cached[1].close();contexts.pop(current.id,None)
                        page=launch(True)
                        page.goto('https://'+HOST+'/creator-micro/content/manage',wait_until='domcontentloaded')
                        phase='baseline';last=None;stable=0
                    page.bring_to_front();headed=True
                    return True
                while not cancel.is_set() and time.monotonic()-started<300:
                    kind=read_challenge()
                    if kind:
                        # Same live headless page handles text verification.
                        # Complex post-submit challenges cannot be recreated:
                        # stop with unknown result instead of clicking again.
                        progress.update(form.get('_task_id'),stage='等待账号验证（提交后仅核对结果）' if final_attempted else '等待账号验证')
                        native_login=kind=='login' and not upload and not final_attempted
                        opened=False
                        if native_login:
                            try:opened=show_login_browser()
                            except Exception:pass
                        request_id=broker.request(current.id,current.nickname,kind,'已触发提交，请恢复所选账号登录；恢复后仅核对结果，不会再次发布。' if final_attempted else '请完成所选账号验证；当前任务已暂停，验证码不会保存。',external_browser=headed,
                            platform='douyin',browser_login=native_login,final_started=final_attempted)
                        if native_login:
                            if opened:broker.open_browser(request_id)
                            else:broker.browser_failure(request_id,'未能打开登录浏览器，请点击“打开登录页面”重试。')
                        native_open_failed=native_login and not opened
                        continue_requested=not native_login
                        response=None
                        end=time.monotonic()+600
                        while not cancel.is_set() and time.monotonic()<end:
                            if native_login and not native_open_failed and page.is_closed():
                                error='submission_verification_cancelled';break
                            if not native_login and (kind in {'complex','login'} or (broker.status()['request'] or {}).get('view_mode')=='page'):
                                import base64
                                broker.frame(request_id,base64.b64encode(page.screenshot()).decode('ascii'))
                            response=broker.consume(request_id)
                            if response:
                                action,code=response
                                if action=='pointer' and not native_login:
                                    if len(code)==2:page.mouse.click(*code)
                                    else:
                                        page.mouse.move(*code[:2]);page.mouse.down();page.mouse.move(*code[2:],steps=20);page.mouse.up()
                                if action=='cancel':error='submission_verification_cancelled';break
                                if action=='done' and native_login:continue_requested=True
                                if action=='visible':
                                    if native_login:
                                        try:
                                            if not show_login_browser():raise ValueError('login unavailable')
                                            broker.open_browser(request_id)
                                            native_open_failed=False
                                        except Exception:
                                            native_open_failed=True
                                            broker.browser_failure(request_id,'未能打开登录浏览器，请点击“打开登录页面”重试。')
                                        continue
                                    # Show the same live page; recreating a headed browser
                                    # discards the upload and may replay a submission.
                                    broker.open_view(request_id)
                                    import base64
                                    broker.frame(request_id,base64.b64encode(page.screenshot()).decode('ascii'))
                                elif action=='code':
                                    inputs=page.locator('input[placeholder*="验证码"]:visible, input[aria-label*="验证码"]:visible')
                                    if inputs.count()==1:
                                        inputs.fill(code);code=''
                                        buttons=page.get_by_role('button',name='确认',exact=True)
                                        if buttons.count()!=1:buttons=page.get_by_role('button',name='验证',exact=True)
                                        if buttons.count()==1 and buttons.is_enabled():buttons.click()
                            if continue_requested and not native_open_failed and not read_challenge():
                                try:
                                    identity=login_read(lambda:evaluate_script(page,READ_IDENTITY+'return identity;',{}))
                                    verified_identity(identity,expected_uid=expected_uid)
                                    break
                                except Exception:pass
                            if native_open_failed:cancel.wait(.2)
                            else:page.wait_for_timeout(500)
                        else:error='submission_verification_cancelled'
                        broker.finish(request_id);request_id=None
                        if cancel.is_set() or (response and response[0]=='cancel') or native_open_failed or native_login and page.is_closed() or read_challenge():break
                        started=time.monotonic()
                        if final_attempted:
                            phase='result'
                            page.goto('https://'+HOST+'/creator-micro/content/manage',wait_until='domcontentloaded')
                        elif phase!='prepare' or not page.url.split('?')[0].endswith((UPLOAD_PATH,PUBLISH_PATH)):
                            # Login may move off the form; only then rebuild it.
                            phase='baseline';upload=False
                            page.goto('https://'+HOST+'/creator-micro/content/manage',wait_until='domcontentloaded')
                        # A challenge on the existing form preserves the upload.
                        stable=0;last=None
                        continue
                    progress.update(form.get('_task_id'),stage='正在核对提交结果' if phase=='result' else '正在核对账号和已有安排' if phase=='baseline' else '正在上传并填写发布资料')
                    code=PREPARE_FORM if phase=='prepare' else READ_RESULT
                    if phase=='prepare' and page.url.endswith(UPLOAD_PATH) and not upload:
                        identity=login_read(lambda:evaluate_script(page,READ_IDENTITY+'return identity;',{}))
                        verified_identity(identity,expected_uid=expected_uid)
                        path=Path(video['path'])
                        if not path.is_file() or sha256_file(path)!=video['content_hash']:raise ValueError('file changed')
                        files=page.locator('input[type="file"]')
                        if files.count()!=1:page.wait_for_timeout(500);continue
                        files.set_input_files(str(path));upload=True
                    raw=login_read(lambda:evaluate_script(page,code,form),
                                   identity=lambda value:value.get('identity') if isinstance(value,dict) else None)
                    if not isinstance(raw,dict):page.wait_for_timeout(500);continue
                    if phase=='prepare' and raw.get('stage'):progress.update(form.get('_task_id'),stage=raw['stage'])
                    identity=raw.get('identity') or {}
                    if identity_session_expired(identity):
                        # Explicit login route triggers the verification prompt next turn.
                        page.goto('https://'+HOST+'/creator-micro/login',wait_until='domcontentloaded');continue
                    try:verified_identity(identity,expected_uid=expected_uid)
                    except ValueError:
                        if identity.get('uid') and identity.get('uid')!=expected_uid:error='submission_account_mismatch';break
                        page.wait_for_timeout(500);continue
                    if current.id not in refreshed:
                        refreshed[current.id]=dict(platform_user_id=expected_uid,nickname=current.nickname,session_blob=session_blob(context,expected_uid),original_ref=current.session_ref,
                                                   login_observed_at=valid_login_time())
                    if raw.get('failure_code'):error=raw['failure_code'];break
                    if raw.get('ready'):
                        if phase=='prepare':
                            verify_prepared_form(raw,form)
                            validate_form_request(expected_uid=expected_uid,title=form['title_text'],body=form['plain_body_text'],tags=' '.join('#'+n for n in form['topic_names']),declaration=form['declaration_label'],scheduled_time_text=form['scheduled_time_text'],location=form.get('location_selection'))
                            if not upload:raise ValueError('upload not bound')
                            progress.update(form.get('_task_id'),stage='正在提交平台定时安排')
                            before_final();final_attempted=True;phase='result'
                            try:evaluate_script(page,FINAL_CLICK,form)
                            except Exception:pass
                        else:
                            rows=raw.get('matches',[]);stable=stable+1 if rows==last else 1;last=rows
                            if stable>=2:
                                if phase=='baseline':
                                    if matching_schedule_rows(rows,form):error='submission_existing_work';break
                                    phase='prepare';stable=0;last=None
                                    page.goto('https://'+HOST+UPLOAD_PATH,wait_until='domcontentloaded')
                                elif verified_scheduled_receipt(raw,expected_uid,form):
                                    refreshed[current.id]=dict(platform_user_id=expected_uid,nickname=current.nickname,session_blob=session_blob(context,expected_uid),original_ref=current.session_ref,
                                                               login_observed_at=valid_login_time())
                                    return SubmissionEvidence(True,platform_schedule_confirmation=scheduled_iso)
                    page.wait_for_timeout(500)
                return SubmissionEvidence(final_attempted,error_code=error)
        except Exception:
            return SubmissionEvidence(final_attempted,error_code='submission_transport_closed')
        finally:
            if request_id:broker.finish(request_id)
    def flush(*,persist=True,release_locks=True):
        nonlocal driver
        from .playwright_runtime import can_stop_exclusively
        errors=[]
        # Capture rotations made during upload, verification and submission.
        for account_id,value in refreshed.items():
            cached=contexts.get(account_id)
            if cached:
                try:value['session_blob']=session_blob(cached[1],value['platform_user_id'])
                except Exception:value['needs_persist']=False
        for account_id,(_,context) in list(contexts.items()):
            try:
                context.close();del contexts[account_id]
                if release_locks:held_locks.pop(account_id).release()
            except Exception as error:errors.append(error)
        if driver and (not contexts or can_stop_exclusively(driver)):
            exclusive=can_stop_exclusively(driver)
            try:
                driver.stop();driver=None
                if exclusive:contexts.clear()
            except Exception as error:errors.append(error)
        # A still-live context retains its profile lock and cleanup reference.
        for account_id in list(held_locks):
            if release_locks and account_id not in contexts:held_locks.pop(account_id).release()
        if persist:persist_refreshed_sessions(engine,paths,refreshed)
        if errors:raise ValueError('publish browser close failed') from errors[0]
    submit.release_sessions=lambda:flush(persist=False,release_locks=False)
    submit.flush_sessions=flush
    submit.expected_uid=expected_uid;submit.supports_topics=True;submit.supports_location=True
    return submit


def make_chromium_login(engine,paths,*,backend=False):
    """Visible login, same private Chromium profile as subsequent publishing."""
    # Set the frozen browser directory before any login/publishing driver starts.
    # Playwright's frozen transport copies the environment on __enter__, so
    # setting it inside launch_persistent_context's argument evaluation is late.
    login_options=browser_options('visible')
    login_options['reduced_motion']='reduce'
    def login(account_id,cancel):
        import tempfile
        import shutil
        from playwright.sync_api import sync_playwright, TimeoutError as NavigationTimeout
        from .douyin_login_bootstrap import DouyinLoginBootstrap, HOME
        from yijianjifa.features.accounts.service import AccountService
        from yijianjifa.features.accounts.session_store import SessionStore
        store=SessionStore(paths.data_dir/'profiles');service=AccountService(engine)
        account=service.get(account_id) if account_id else None
        expected=account.platform_user_id if account else None
        lock=account_lock(account_id or 'new-account')
        if not lock.acquire(blocking=False):
            from yijianjifa.features.publish.account_wait import AccountBusy
            raise AccountBusy()
        temp=None;context=None;result=None
        try:
            if account:profile=chromium_profile(store,expected)
            else:
                temp=Path(tempfile.mkdtemp(prefix='.chromium-login-',dir=store.root));temp.chmod(0o700);profile=temp
            with sync_playwright() as pw:
                context=pw.chromium.launch_persistent_context(str(profile),**login_options)
                context.set_default_timeout(15000)
                if account:restore_account_session(context,store,account,profile,expected)
                page=context.pages[0] if context.pages else context.new_page()
                from .loaded_avatar import LoadedAvatarCache,_avatar_url
                avatars=LoadedAvatarCache(page.main_frame)
                page.on('requestfinished',avatars.capture)
                def read_avatar(url):
                    if not _avatar_url(url):return ''
                    stop=time.monotonic()+2
                    while not cancel.is_set() and not page.is_closed() and time.monotonic()<stop:
                        data=avatars.data_for(url)
                        if data is not None:return data
                        page.wait_for_timeout(100)
                    return ''
                bootstrap=DouyinLoginBootstrap(time.monotonic())
                try:page.goto(HOME,wait_until='commit',timeout=10000)
                except NavigationTimeout:pass  # The widget can recover after a slow document commit.
                deadline=time.monotonic()+600
                while not cancel.is_set() and time.monotonic()<deadline and not page.is_closed():
                    try:
                        if not bootstrap.poll(page,time.monotonic()):
                            page.wait_for_timeout(500)
                            continue
                        def read_account_identity(operation):
                            return observe_login_read(service,account.id,expected,account.session_ref,operation,
                                classify=lambda value:douyin_login_state(value,expected))
                        result=observe_login_session(page,context,expected,avatar_reader=read_avatar,
                            identity_reader=read_account_identity if account else None)
                        uid=result['platform_user_id']
                        if backend:
                            while not cancel.is_set() and not page.is_closed():page.wait_for_timeout(500)
                            result['session_blob']=session_blob(context,uid)
                        break
                    except Exception:pass
                    page.wait_for_timeout(1000)
                context.close();context=None
            if result and temp:
                destination=chromium_profile(store,result['platform_user_id'])
                # Never overwrite an existing profile from another active login.
                if not any(destination.iterdir()):
                    destination.rmdir();shutil.move(str(temp),str(destination));temp=None
            return result
        finally:
            if context:
                try:context.close()
                except Exception:pass
            if temp:shutil.rmtree(temp,ignore_errors=True)
            lock.release()
    from .session_check import make_session_checker
    login.check_session = make_session_checker(engine, paths, platform='douyin',
        home='https://' + HOST + '/creator-micro/home', origins={'https://' + HOST},
        profile_for=chromium_profile, restore=lambda context, store, account, profile: restore_account_session(context, store, account, profile, account.platform_user_id),
        expression='async () => {' + READ_IDENTITY + 'return identity;}',
        verify=lambda value: verified_identity(value, expected_uid=None),
        identity_timeout_ms=8000,  # Three sequential identity GETs share this bound and the 25s check deadline.
        expired=lambda value: type(value) is dict and identity_session_expired(value))
    return login


def chromium_profile(store,uid):
    directory=store.ensure_profile_dir('chromium-uid:'+uid)/'browser'
    store._private_directory(directory)
    return directory
