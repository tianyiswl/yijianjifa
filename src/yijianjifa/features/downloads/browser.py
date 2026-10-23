"""Isolated read-only consumer Chromium and a pinned public CONNECT relay.

Read-only managed cookie archives; no publisher profile or challenge solver.
"""
import hashlib
import json
import re
import secrets
from dataclasses import replace
import select
import socket
import socketserver
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, unquote
from .contracts import DownloadError, VerificationEvent
from .network import PublicConnector, _OperationGuard, _active_guard

NAV_HOSTS = {'douyin.com', 'www.douyin.com', 'v.douyin.com', 'www.iesdouyin.com', 'iesdouyin.com'}


def navigation_allowed(url):
    try:
        p = urlsplit(url)
        return p.scheme == 'https' and p.hostname in NAV_HOSTS and p.port in (None,443) and not p.username and not p.password
    except ValueError: return False


AUTH_HOSTS = {'sso.douyin.com','passport.douyin.com','verify.snssdk.com','verify.zijieapi.com'}
AUTH_PATHS = {
    '/passport/web/get_qrcode','/passport/web/check_qrconnect',
    '/passport/web/sms/send_code','/passport/web/sms/login','/passport/web/sms/check_code',
    '/passport/web/user/login','/passport/web/login','/passport/web/auth/login',
    '/passport/sso/login','/passport/sso/sync','/passport/web/auth/qrlogin',
    '/captcha/get','/captcha/verify','/captcha/refresh',
}
MUTATION_SEGMENTS = {'publish','upload','draft','delete','change','bind','unbind','logout','follow','digg','comment','commit','create','edit','update'}


def auth_endpoint(url):
    try:
        p=urlsplit(url)
        return (p.scheme=='https' and p.port in (None,443) and not p.username and not p.password
                and p.hostname in AUTH_HOSTS | NAV_HOSTS and p.path.rstrip('/') in AUTH_PATHS)
    except ValueError: return False


def request_allowed(method, url, *, resource_type, navigation, frame_url,
                    anonymous_probe_initializers=False, interactive=False, authenticated=False, preflight_method=None):
    try:
        p=urlsplit(url)
        if any(part in MUTATION_SEGMENTS for part in p.path.split('/')): return False
        origin_ok=navigation_allowed(frame_url) or (interactive and auth_endpoint(frame_url))
        if navigation:
            return method in ('GET','HEAD') and (navigation_allowed(url) or (interactive and auth_endpoint(url)))
        if resource_type == 'media': return False
        if method in ('GET','HEAD'): return True  # Proxy validates every destination.
        method_ok = method=='POST' or (method=='OPTIONS' and preflight_method=='POST')
        if not method_ok or not origin_ok: return False
        if interactive and auth_endpoint(url): return True
        return (anonymous_probe_initializers and not interactive and not authenticated
                and p.scheme == 'https' and p.port in (None,443) and not p.username and not p.password
                and (p.hostname,p.path) in {('mcs.zijieapi.com','/webid'),
                    ('mssdk.bytedance.com','/web/r/token'),
                    ('www.douyin.com','/service/2/abtest_config/')})
    except ValueError: return False


DISMISS_OPTIONAL_LOGIN = r"""() => {
  const visible=e=>e.getClientRects().length>0;
  const labels=[...document.querySelectorAll('h1,h2,h3,p,span,div')];
  const heading=labels.find(e=>visible(e)&&e.textContent.trim()==='登录后免费畅享高清视频');
  if(!heading) return false;
  let panel=heading;
  for(let i=0;i<7&&panel.parentElement;i++) {
    const r=panel.getBoundingClientRect();
    if(r.width>400&&r.height>250&&panel.querySelector('input')) break;
    panel=panel.parentElement;
  }
  if(panel===document.body||panel===document.documentElement) return false;
  const box=panel.getBoundingClientRect();
  const candidates=[...panel.querySelectorAll('[class*="close"],[class*="Close"],[aria-label="关闭"],[aria-label="Close"],[data-e2e*="close"],svg')];
  const close=candidates.find(e=>{
    if(!visible(e)||e.tagName==='INPUT') return false;
    const r=e.getBoundingClientRect();
    return r.width>=4&&r.width<=80&&r.height>=4&&r.height<=80&&r.x>box.x+box.width*.65&&r.y<box.y+box.height*.35;
  });
  if(!close) return false;
  close.click();  // The site's visible, standard dismiss button; no form submit.
  return true;
}"""


class PublicConnectProxy:
    def __init__(self, connector=None):
        self.connector = connector or PublicConnector()
        self.sockets = set()
        self.failure_counts = {}
        self.traffic = {"tunnels":0,"up_bytes":0,"down_bytes":0}
        self.lock = threading.Lock()
        self.connections = threading.BoundedSemaphore(32)
        self.stopped = threading.Event()
        self.close_lock = threading.Lock()
        self.closed = False
        owner = self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if not owner.connections.acquire(blocking=False): return
                remote = None
                with owner.lock: owner.sockets.add(self.request)
                try:
                    self.request.settimeout(10)
                    data = b''
                    while b'\r\n\r\n' not in data:
                        chunk = self.request.recv(1024)
                        if not chunk: return
                        data += chunk
                        if len(data) > 16384: return
                    line = data.split(b'\r\n',1)[0].decode('ascii')
                    method, target, version = line.split(' ')
                    if method != 'CONNECT' or version != 'HTTP/1.1': return
                    host, port = target.rsplit(':',1)
                    if port != '443' or not host or any(c in host for c in '/@\\[]'): return
                    class Stopped:
                        def cancelled(self): return owner.stopped.is_set()
                    guard = _OperationGuard(Stopped())
                    _active_guard.value = guard
                    try:
                        remote = owner.connector.open_tcp(host,443)
                    finally:
                        _active_guard.value = None
                        guard.close()
                    with owner.lock:
                        if owner.stopped.is_set(): return
                        owner.sockets.add(remote)
                    self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    with owner.lock: owner.traffic['tunnels'] += 1
                    remote.settimeout(10)
                    deadline = time.monotonic() + 120
                    while not owner.stopped.is_set() and time.monotonic() < deadline:
                        readable, _, _ = select.select([self.request,remote],[],[],.2)
                        for sock in readable:
                            block = sock.recv(65536)
                            if not block: return
                            with owner.lock: owner.traffic['up_bytes' if sock is self.request else 'down_bytes'] += len(block)
                            (remote if sock is self.request else self.request).sendall(block)
                except DownloadError as error:
                    with owner.lock:
                        owner.failure_counts[error.code] = owner.failure_counts.get(error.code, 0) + 1
                except Exception as error:
                    code='timeout' if isinstance(error,TimeoutError) else 'network'
                    with owner.lock: owner.failure_counts[code]=owner.failure_counts.get(code,0)+1
                    # Never log targets or exception strings containing private URLs.
                finally:
                    with owner.lock:
                        owner.sockets.discard(self.request)
                        if remote: owner.sockets.discard(remote)
                    if remote: remote.close()
                    owner.connections.release()
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False
        self.server = Server(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.05),daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def close(self):
        with self.close_lock:
            if self.closed: return
            self.closed = True
        self.stopped.set()
        with self.lock:
            for sock in self.sockets:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                sock.close()
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


class BrowserLease:
    def __init__(self, profile, lock, *, launch=None, visible=False, connector=None, anonymous_probe_initializers=False, session_snapshot=None, _on_cleanup_pending=None):
        self.lock, self.driver, self.context, self.proxy = lock, None, None, None
        self.closed = False
        self._cleaned_resources = set()
        self.cleanup_errors = ()
        self._anonymous_initializers = anonymous_probe_initializers
        self.verification_required = False
        self.verification_event = None
        self.interactive = False
        self.visible = visible
        self._verification_check = False
        self._identity = None
        self._expected_uid = session_snapshot.expected_uid if session_snapshot else None
        self._identity_source = None
        self._verified_snapshot = None
        self.last_phase = None
        self.last_failure = None
        self.blocked_requests = set()
        self.observed_statuses = []
        self.read_evidence = {"detail_requested":0,"bootstrap_responses":[],"failed_codes":{},"page_errors":0}
        try:
            self.proxy = PublicConnectProxy(connector)
            if launch is None:
                from playwright.sync_api import sync_playwright
                self.driver = sync_playwright().start()
                launch = self.driver.chromium.launch_persistent_context
            self.context = launch(str(profile), channel='chrome', headless=not visible,
                accept_downloads=False, service_workers='block',
                proxy={'server':self.proxy.url, 'bypass':'<-loopback>'},
                args=['--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                      '--disable-background-networking','--disable-component-update'])
            def route(request_route):
                request = request_route.request
                try: frame_url=request.frame.url
                except Exception: frame_url=''
                if not request_allowed(request.method,request.url,resource_type=request.resource_type,navigation=request.is_navigation_request(),frame_url=frame_url,anonymous_probe_initializers=self._anonymous_initializers,interactive=self.interactive,authenticated=bool(self._identity),preflight_method=request.headers.get('access-control-request-method')):
                    if len(self.blocked_requests)<20:
                        p=urlsplit(request.url)
                        segments={'web','common','report','service','2','abtest_config','list','websdk','v1','getInfo','device','register','aweme','detail','post','passport','account','login','captcha','verify','v2','mssdk','webid','r','token','monitor_browser','collect','batch','security','config','im','online','offline','status','client','get','info','setting','settings','user','permission','pc','browser','recommend','feed','log','action','stats','self','report','collection','check','update','register','sdk','watch','create','submit','publish','upload','draft'}
                        safe_path='/'.join(part if not part or part in segments else '<opaque>' for part in p.path.split('/'))[:160]
                        self.blocked_requests.add((request.method,p.hostname,safe_path,request.resource_type))
                    request_route.abort()
                else: request_route.continue_()
            self.context.route('**/*',route)
            if hasattr(self.context, 'route_web_socket'):
                self.context.route_web_socket('**/*',lambda ws: None)  # Routed sockets never connect_to_server.
            if session_snapshot is not None:
                from .account_session import restore_consumer_session
                restore_consumer_session(self.context, profile, session_snapshot)
            self.page = self.context.new_page()
        except Exception as error:
            self.close()
            if self.cleanup_errors and _on_cleanup_pending is not None:
                _on_cleanup_pending(self)
            if isinstance(error, DownloadError):raise
            raise DownloadError('network') from None

    def read_next(self, url, *, cancel):
        if self.closed: raise DownloadError('source_missing')
        if not same_profile_page(self.page.url, url): raise DownloadError('source_missing')
        return self.read(url,cancel=cancel,_next=True)

    def read(self, url, *, cancel, _next=False):
        if self.closed: raise DownloadError('source_missing')
        if self.verification_required: raise DownloadError('verification_required')
        if not navigation_allowed(url): raise DownloadError('unsafe_source')
        if cancel.cancelled(): self.close(); raise DownloadError('cancelled')
        if self._verified_snapshot is not None:
            cached_url,payload=self._verified_snapshot
            self._verified_snapshot=None
            if cached_url==url: return payload
        if self._expected_uid is not None:
            reuse_identity = (_next and self._identity == self._expected_uid
                              and self._identity_source == url and same_profile_page(self.page.url, url))
            if not reuse_identity:
                self._identity = None
                self._identity_source = None
        def accepted(payload):
            if not profile_read:
                payload=select_video_metadata(payload,self.page.url,url)
                if payload is None: raise DownloadError('metadata_missing')
            if self._expected_uid is not None:
                self._identity_source = url
            return payload
        payloads = []
        optional_login_seen = False
        profile_read = urlsplit(url).path.startswith('/user/')
        expected_api = 'aweme/v1/web/aweme/post' if profile_read else 'aweme/v1/web/aweme/detail'
        completed_responses = []
        def response(response):
            pass  # Headers alone do not prove the body is complete.
        def request_finished(request):
            p=urlsplit(request.url)
            allowed = p.hostname in NAV_HOSTS and (p.path.rstrip('/') in {'/aweme/v1/web/aweme/post', '/aweme/v1/web/aweme/detail', '/aweme/v1/web/user/profile/self'})
            if allowed and len(completed_responses)<8:
                completed_responses.append(request)
        def consume_response(response):
            try:
                p=urlsplit(response.url)
                if (p.hostname,p.path) in {('mcs.zijieapi.com','/webid'),('mssdk.bytedance.com','/web/r/token')}:
                    if len(self.read_evidence['bootstrap_responses'])<10:
                        self.read_evidence['bootstrap_responses'].append({'kind':'visitor' if p.path=='/webid' else 'security','status':response.status})
                    return
                if (self._verification_check or self._expected_uid is not None) and p.hostname=='www.douyin.com' and p.path.rstrip('/')=='/aweme/v1/web/user/profile/self':
                    length=response.headers.get('content-length')
                    if length is not None and int(length)>1024*1024: return
                    raw=response.body()
                    if len(raw)>1024*1024: return
                    data=json.loads(raw)
                    user=data.get('user') if isinstance(data,dict) else None
                    if isinstance(user,dict) and data.get('status_code')==0:
                        uid=str(user.get('uid',''))
                        if re.fullmatch(r'[1-9][0-9]{0,31}',uid) and user.get('sec_uid') and user.get('nickname'):
                            self._identity=uid  # Backend memory only; never included in events/reports.
                    return
                # Only ordinary public detail/post responses; no direct API construction.
                if profile_read:
                    if p.hostname not in NAV_HOSTS or p.path.rstrip('/')!='/'+expected_api: return
                elif p.hostname not in NAV_HOSTS or '/aweme/' not in p.path or any(part in p.path.split('/') for part in ('user','account','passport','token','self')):
                    return
                json_response='application/json' in response.headers.get('content-type','')
                if len(self.observed_statuses)<20: self.observed_statuses.append({'status':response.status,'json':json_response})
                if len(payloads)>=2 or not json_response: return
                length = response.headers.get('content-length')
                if length is not None and int(length)>4*1024*1024: return
                body = response.body()
                if profile_read: self.read_evidence['profile_body_bytes']=len(body)
                if len(body)<=4*1024*1024:
                    try: parsed=json.loads(body)
                    except (ValueError,TypeError):
                        if profile_read: self.read_evidence['profile_json_parse']='invalid_or_empty'
                        return
                    if profile_read and isinstance(parsed,dict):
                        self.read_evidence['profile_response']={'status_code':parsed.get('status_code') if isinstance(parsed.get('status_code'),int) else None,'aweme_list_present':'aweme_list' in parsed,'aweme_list_count':len(parsed['aweme_list']) if isinstance(parsed.get('aweme_list'),list) else None,'has_more':parsed.get('has_more') if isinstance(parsed.get('has_more'),(int,bool)) else None}
                    selected=select_profile_metadata(parsed, response.url, url) if profile_read else select_video_metadata(parsed,self.page.url,url)
                    if selected: payloads.append(selected)
            except Exception: pass
        def requested(request):
            if expected_api in urlsplit(request.url).path: self.read_evidence['detail_requested'] += 1
        def failed(request):
            import re
            value=request.failure or ''
            found=re.fullmatch(r'net::ERR_[A-Z_]+',value)
            code=value if found else 'other_network_failure'
            counts=self.read_evidence['failed_codes']
            if len(counts)<20 or code in counts: counts[code]=counts.get(code,0)+1
        def page_error(error): self.read_evidence['page_errors'] += 1
        self.page.on('request',requested)
        self.page.on('requestfailed',failed)
        self.page.on('pageerror',page_error)
        self.page.on('response',response)
        self.page.on('requestfinished',request_finished)
        finished = threading.Event()
        read_deadline = min(time.monotonic()+90,self._verification_deadline) if self._verification_check else time.monotonic()+90
        def watch_cancel():
            while not finished.wait(.05):
                if cancel.cancelled() or time.monotonic()>=read_deadline:
                    self.proxy.close()
                    return
        watcher = threading.Thread(target=watch_cancel,daemon=True)
        watcher.start()
        try:
            self.last_phase = 'navigation'
            if _next: self.page.mouse.wheel(0,1800)
            else: self.page.goto(url, wait_until='commit', timeout=max(1,min(20000,int((read_deadline-time.monotonic())*1000))))
            for _ in range(360):
                if time.monotonic()>=read_deadline: break
                if cancel.cancelled(): self.close(); raise DownloadError('cancelled')
                while completed_responses:
                    request=completed_responses.pop(0)
                    result=request.response()
                    if result is not None: consume_response(result)
                if self._expected_uid is not None and self._identity is not None and self._identity != self._expected_uid:
                    self._identity_source = None
                    raise DownloadError('identity_mismatch')
                if payloads and (not (self._verification_check or self._expected_uid is not None) or self._identity): return accepted(payloads[0])
                try:
                    self.last_phase = 'metadata_snapshot'
                    router = self.page.evaluate('() => window._ROUTER_DATA || null')
                    if router and not _next and not profile_read:
                        found = _find_metadata(router) if profile_read else select_video_metadata(router,self.page.url,url)
                        if found and (not profile_read or 'aweme_list' in found) and (not (self._verification_check or self._expected_uid is not None) or self._identity): return accepted(found)
                    raw = self.page.locator('script#RENDER_DATA').text_content(timeout=500) if self.page.locator('script#RENDER_DATA').count() else None
                    if raw and len(raw)<=4*1024*1024 and not _next and not profile_read:
                        payload = json.loads(unquote(raw))
                        found = _find_metadata(payload) if profile_read else select_video_metadata(payload,self.page.url,url)
                        if found and (not profile_read or 'aweme_list' in found) and (not (self._verification_check or self._expected_uid is not None) or self._identity): return accepted(found)
                    if not optional_login_seen and profile_read:
                        dismissed=self.page.evaluate(DISMISS_OPTIONAL_LOGIN)
                        self.read_evidence['marketing_prompt_closed']=bool(dismissed)
                        optional_login_seen=bool(dismissed)
                    challenge = self.page.evaluate('''() => {
                      const visible=e=>e.getClientRects().length>0;
                      if([...document.querySelectorAll('input')].some(e=>visible(e)&&/验证码/.test(e.placeholder))) return /扫码登录|验证码登录/.test(document.body.innerText) ? 'login' : 'captcha';
                      if([...document.querySelectorAll('[id*="captcha"],[class*="captcha"],iframe')].some(e=>visible(e)&&/captcha|verify/.test(e.id+e.className+e.src))) return 'captcha';
                      return null;
                    }''')
                    if challenge=='login' and not self._verification_check:
                        if not optional_login_seen:
                            dismissed=self.page.evaluate(DISMISS_OPTIONAL_LOGIN)
                            self.read_evidence['marketing_prompt_closed']=bool(dismissed)
                            optional_login_seen=bool(dismissed)
                        # A marketing modal does not prove public metadata is inaccessible.
                        # Continue the same finite read; never submit credentials automatically.
                    elif challenge:
                        self.verification_required = True
                        self.verification_event = VerificationEvent(secrets.token_urlsafe(24),'login' if challenge=='login' else 'captcha')
                        raise DownloadError('verification_required')
                except DownloadError:
                    raise
                except Exception as error:
                    self.last_failure = 'navigation_changed' if 'Execution context was destroyed' in str(error) else ('metadata_format' if isinstance(error, (ValueError, TypeError)) else 'browser_error')
                self.page.wait_for_timeout(250)
            if self._expected_uid is not None and self._identity != self._expected_uid:
                raise DownloadError('identity_mismatch')
            raise DownloadError('metadata_missing')
        except DownloadError: raise
        except Exception as error:
            self.last_failure = 'timeout' if type(error).__name__=='TimeoutError' else 'browser_error'
            if cancel.cancelled(): raise DownloadError('cancelled') from None
            raise DownloadError('network') from None
        finally:
            finished.set(); watcher.join(timeout=2)
            if cancel.cancelled(): self.close()
            if not self.closed:
                for name,handler in [('response',response),('requestfinished',request_finished),('request',requested),('requestfailed',failed),('pageerror',page_error)]:
                    self.page.remove_listener(name,handler)

    def begin_verification(self, url, *, timeout_seconds=600):
        """Only the explicit user/controller action calls this; never called by read()."""
        if self.closed or not self.visible or not navigation_allowed(url): raise DownloadError('verification_required')
        self.interactive=True
        self._anonymous_initializers=False  # Never restore anonymous exceptions after auth begins.
        self.verification_required=True
        self.verification_event=VerificationEvent(secrets.token_urlsafe(24),'login')
        self._verification_url=url
        self._verification_deadline=time.monotonic()+min(600,max(1,timeout_seconds))
        title_script="""(() => {
          const apply=()=>{if(document.title!=='视频下载 · 主页读取验证') document.title='视频下载 · 主页读取验证';};
          document.addEventListener('DOMContentLoaded',apply,{once:true});
          setInterval(apply,2000);
        })();"""
        self.context.add_init_script(title_script)
        try:
            self.page.goto(url,wait_until='commit',timeout=20000)
        except Exception:
            if self.page.is_closed(): self.close(); raise DownloadError('cancelled') from None
        return self.verification_event

    def _check_verification(self, cancel):
        if self.closed: raise DownloadError('cancelled')
        if cancel.cancelled() or self.page.is_closed():
            self.close(); raise DownloadError('cancelled')
        if time.monotonic()>=self._verification_deadline:
            self.close(); raise DownloadError('timeout')

    def poll_verification(self, *, cancel):
        self._check_verification(cancel)
        try: self.page.wait_for_timeout(250)
        except Exception:
            self.close(); raise DownloadError('cancelled') from None
        return self.verification_event

    def complete_verification(self, *, cancel):
        if self.closed or not self.interactive or self.verification_event is None:
            raise DownloadError('verification_required')
        self._check_verification(cancel)
        pending=self.verification_event
        self.interactive=False  # Authentication writes stop before backend page reading resumes.
        self.verification_required=False
        self._verification_check=True
        self._identity=None
        try:
            payload=self.read(self._verification_url,cancel=cancel)
            self._check_verification(cancel)
            if not self._identity or not isinstance(payload.get('aweme_list'),list) or not payload['aweme_list']:
                raise DownloadError('verification_required')
            self.verification_event=replace(pending,state='resolved')
            self._verified_snapshot=(self._verification_url,payload)
            return self.verification_event
        except DownloadError:
            if not self.closed: self._check_verification(cancel)
            if not self.closed:
                self.verification_event=pending
                self.verification_required=True
                self.interactive=True
            raise
        finally:
            self._verification_check=False

    def close(self):
        # closed forbids further use; each resource tracks cleanup separately for retry.
        self.closed = True
        if self.verification_event is not None and self.verification_event.state=='required':
            self.verification_event=replace(self.verification_event,state='cancelled')
        failures=[]
        for name, method in (('proxy','close'),('context','close'),('driver','stop'),('lock','release')):
            if name in self._cleaned_resources: continue
            # A failed API cleanup cannot establish browser/driver exit. Keep
            # the shared account unavailable until both resources succeed.
            if name=='lock' and not {'context','driver'} <= self._cleaned_resources: continue
            resource=getattr(self,name,None)
            try:
                if resource is not None: getattr(resource,method)()
                self._cleaned_resources.add(name)
            except Exception:
                failures.append(name+'_cleanup_failed')
        self.cleanup_errors=tuple(failures)  # Finite diagnostics; preserve any original business error.


def public_video_work_id(url):
    """Only normal official public detail page paths establish video identity."""
    if not navigation_allowed(url) or '\\' in url or any(ord(c)<32 or ord(c)==127 for c in url): return None
    parts=urlsplit(url)
    if parts.hostname not in {'douyin.com','www.douyin.com','iesdouyin.com','www.iesdouyin.com'}: return None
    match=re.fullmatch(r'/(?:video|share/video)/(\d{1,32})/?',parts.path)
    return match.group(1) if match else None


def same_profile_page(page_url, source_url):
    if not navigation_allowed(page_url) or not navigation_allowed(source_url): return False
    page, source=urlsplit(page_url),urlsplit(source_url)
    return (page.hostname in {'douyin.com','www.douyin.com'} and source.hostname in {'douyin.com','www.douyin.com'}
            and re.fullmatch(r'/user/[A-Za-z0-9_-]{1,200}/?',source.path) is not None
            and re.fullmatch(r'/user/[A-Za-z0-9_-]{1,200}/?',page.path) is not None
            and page.path.rstrip('/')==source.path.rstrip('/'))


def select_video_metadata(payload, page_url, source_url):
    work_id=public_video_work_id(page_url)
    selected=select_work_metadata(payload,work_id)
    if selected is None: return None
    return dict(selected,_video_source=source_url,_video_work_id=work_id)


def select_profile_metadata(payload, response_url, source_url):
    """Bind a naturally observed native post response to its request and homepage."""
    from urllib.parse import parse_qs
    source, response = urlsplit(source_url), urlsplit(response_url)
    if source.hostname != 'www.douyin.com' or not re.fullmatch(r'/user/[A-Za-z0-9_-]{1,200}/?', source.path): return None
    if response.hostname != 'www.douyin.com' or response.path.rstrip('/') != '/aweme/v1/web/aweme/post': return None
    query = parse_qs(response.query, keep_blank_values=True)
    target = source.path.rstrip('/').rsplit('/', 1)[-1]
    if query.get('sec_user_id') != [target]: return None
    request_cursor = query.get('max_cursor')
    if not request_cursor or len(request_cursor) != 1 or not re.fullmatch(r'\d{1,32}', request_cursor[0]): return None
    if not isinstance(payload, dict) or 'aweme_list' not in payload: return None
    return dict(payload, _catalog_source=target, _catalog_request_cursor=request_cursor[0])


def select_work_metadata(value, work_id, depth=0):
    """Schema-tolerant public JSON, but never substitute an unrelated feed work."""
    if not work_id or depth>20: return None
    if isinstance(value,dict):
        if str(value.get('aweme_id','')) == work_id and isinstance(value.get('video'),dict):
            return {'aweme_detail':value}
        for child in list(value.values())[:500]:
            result=select_work_metadata(child,work_id,depth+1)
            if result: return result
    elif isinstance(value,list):
        for child in value[:500]:
            result=select_work_metadata(child,work_id,depth+1)
            if result: return result
    return None


def _find_metadata(value, depth=0):
    if depth>20: return None
    if isinstance(value,dict):
        if 'aweme_detail' in value or 'aweme_list' in value: return value
        if isinstance(value.get('item_list'),list) and value['item_list']:
            return {'aweme_list':value['item_list']}
        for child in value.values():
            result = _find_metadata(child,depth+1)
            if result: return result
    elif isinstance(value,list):
        for child in value[:500]:
            result = _find_metadata(child,depth+1)
            if result: return result
    return None


class DownloadBrowserPool:
    def __init__(self, data_dir: Path, *, launch=None, connector=None, visible=False, anonymous_probe_initializers=False, managed_sessions=None):
        self.managed_sessions = managed_sessions
        self.root = Path(data_dir)/'consumers'
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.launch = launch
        self.connector = connector
        self.visible = visible
        self.anonymous_probe_initializers = anonymous_probe_initializers
        self.anonymous_lock = threading.Lock()
        self._construction_lock = threading.RLock()
        self._pending_lease = None
        self._pending_owner = None

    def _retain_pending_cleanup(self, lease):
        # acquire is serialized; at most one failed construction remains owned.
        self._pending_lease = lease
        self._pending_owner = threading.get_ident()

    def retry_pending_cleanup(self) -> bool:
        """Retry only the retained lease, on its owner thread; never launch."""
        with self._construction_lock:
            if self._pending_lease is None:return True
            if self._pending_owner != threading.get_ident():return False
            self._pending_lease.close()
            if self._pending_lease.cleanup_errors:return False
            self._pending_lease = None
            self._pending_owner = None
            return True

    def acquire(self, account_id: str | None) -> BrowserLease:
        with self._construction_lock:
            return self._acquire(account_id)

    def _acquire(self, account_id: str | None) -> BrowserLease:
        if not self.retry_pending_cleanup():raise DownloadError('account_busy')
        if account_id is not None and self.anonymous_probe_initializers: raise DownloadError('unsupported_source')
        if account_id is None and self.managed_sessions is not None and not self.anonymous_probe_initializers:
            account_id = self.managed_sessions.default_account_id()
        if account_id is None: lock = self.anonymous_lock
        else:
            from yijianjifa.webui.chromium_publish import account_lock
            lock = account_lock(account_id)
        if not lock.acquire(blocking=False): raise DownloadError('account_busy')
        profile = (self.root/'probes'/secrets.token_hex(16) if self.anonymous_probe_initializers else
                   self.root/hashlib.sha256((account_id or 'anonymous').encode()).hexdigest())
        try:
            snapshot = self.managed_sessions.load(account_id) if account_id is not None and self.managed_sessions is not None else None
        except Exception:
            lock.release()
            raise
        return BrowserLease(profile,lock,launch=self.launch,connector=self.connector,visible=self.visible,anonymous_probe_initializers=self.anonymous_probe_initializers,session_snapshot=snapshot,_on_cleanup_pending=self._retain_pending_cleanup)
