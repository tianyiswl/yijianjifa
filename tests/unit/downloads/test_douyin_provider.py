import json
import pytest
from pathlib import Path
from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.downloads.input import normalize_inputs
from yijianjifa.features.downloads.network import SourceRegistry
from yijianjifa.features.downloads.providers import DouyinProvider

class Token:
    def cancelled(self): return False

from test_catalog_first import post

class Reader:
    def __init__(self, payload): self.payload = payload
    def read(self, url, *, cancel): return self.payload

def work(i):
    return {'aweme_id': str(i), 'desc': 'hello https://signed.invalid/a?token=SECRET', 'author': {'nickname':'author', 'sec_uid':'public-author'}, 'video': {'duration':1234, 'play_addr': {'url_list':['https://example.com/video.mp4?token=SECRET']}}}

def test_provider_pages_are_finite_and_sanitized():
    p = DouyinProvider(SourceRegistry(), Reader(post([work(i) for i in range(600)], author='public-author')))
    source = normalize_inputs('https://www.douyin.com/user/public-author')[0]
    page = p.resolve(source, None, cancel=Token())
    assert len(page.candidates) == 20
    rows = list(page.candidates)
    while page.has_more:
        page = p.resolve(source, page.cursor, cancel=Token()); rows.extend(page.candidates)
    assert len(rows) == 500
    assert 'SECRET' not in repr(rows)
    assert page.completion == 'limited'

@pytest.mark.parametrize('payload,code', [({}, 'metadata_missing'), ({'aweme_detail':{'desc':'bad'}},'metadata_missing'), ({'aweme_detail':dict(work(1),images=[{}])},'unsupported_source'), ({'verification':'login'},'verification_required')])
def test_concrete_failures(payload,code):
    p = DouyinProvider(SourceRegistry(), Reader(payload))
    with pytest.raises(DownloadError) as e: p.resolve(normalize_inputs('https://www.douyin.com/video/1')[0],None,cancel=Token())
    assert e.value.code == code

def test_duplicate_and_cursor_binding():
    p = DouyinProvider(SourceRegistry(), Reader(post([work(1),work(1)], author='public-author')))
    source=normalize_inputs('https://www.douyin.com/user/public-author')[0]
    assert len(p.resolve(source,None,cancel=Token()).candidates)==1
    with pytest.raises(DownloadError): p.resolve(source,'private-cursor?SECRET',cancel=Token())

from yijianjifa.features.downloads.browser import DownloadBrowserPool, navigation_allowed, PublicConnectProxy
from yijianjifa.webui.chromium_publish import account_lock
import socket
import threading

class Context:
    def __init__(self): self.closed=False; self.routes=[]
    def route(self,*args): self.routes.append(args)
    def new_page(self): return object()
    def close(self): self.closed=True

def test_consumer_isolation_same_account_and_cleanup(tmp_path):
    launches=[]
    def launch(profile,**options):
        launches.append((profile,options)); return Context()
    pool=DownloadBrowserPool(tmp_path,launch=launch)
    lease=pool.acquire('same-account')
    assert Path(launches[0][0]).parent==tmp_path/'consumers'
    assert '--disable-quic' in launches[0][1]['args']
    assert 'disable_non_proxied_udp' in str(launches[0][1]['args'])
    assert not account_lock('same-account').acquire(blocking=False)
    with pytest.raises(DownloadError) as e: pool.acquire('same-account')
    assert e.value.code=='account_busy'
    port=lease.proxy.server.server_address[1]
    lease.close(); lease.close()
    assert lease.context.closed
    with pytest.raises(OSError): socket.create_connection(('127.0.0.1',port),timeout=.1)
    again=pool.acquire('same-account'); again.close()

@pytest.mark.parametrize('url',['https://127.0.0.1/','http://www.douyin.com/','https://www.douyin.com.evil.invalid/','https://evil@www.douyin.com/','https://creator.douyin.com/'])
def test_navigation_does_not_reach_publisher_or_private(url):
    assert not navigation_allowed(url)

def test_proxy_uses_pinned_connector_and_closes_active_socket():
    called=[]; remote,peer=socket.socketpair()
    class Connector:
        def open_tcp(self,host,port): called.append((host,port)); return remote
    proxy=PublicConnectProxy(Connector())
    client=socket.create_connection(proxy.server.server_address,timeout=1)
    client.sendall(b'CONNECT www.douyin.com:443 HTTP/1.1\r\nHost: www.douyin.com\r\n\r\n')
    assert b'200' in client.recv(100)
    assert called==[('www.douyin.com',443)]
    client.sendall(b'opaque TLS'); assert peer.recv(100)==b'opaque TLS'
    proxy.close(); assert peer.recv(100)==b''
    client.close();peer.close()

def test_failed_launch_releases_account_lock(tmp_path):
    def fail(*args,**kwargs): raise RuntimeError('https://private.invalid/?signature=SECRET')
    pool=DownloadBrowserPool(tmp_path,launch=fail)
    with pytest.raises(DownloadError) as e: pool.acquire('failed-launch')
    assert str(e.value)=='network'
    lock=account_lock('failed-launch')
    assert lock.acquire(blocking=False)
    lock.release()


@pytest.mark.parametrize('setup_failure', ['route', 'session', 'page'])
@pytest.mark.parametrize('cleanup_failure', ['context', 'driver'])
def test_failed_constructor_cleanup_retains_owner_and_blocks_launch_until_retry(tmp_path, monkeypatch, setup_failure, cleanup_failure):
    import sys
    from types import SimpleNamespace
    import yijianjifa.features.downloads.browser as browser
    import yijianjifa.features.downloads.account_session as sessions
    import yijianjifa.webui.chromium_publish as publishing
    ready=False;contexts=[];drivers=[];owner=threading.get_ident()
    identity='constructor-'+setup_failure+'-'+cleanup_failure
    actual_lock=account_lock(identity)
    class TrackedLock:
        releases=0
        def acquire(self,**kwargs):return actual_lock.acquire(**kwargs)
        def release(self):self.releases+=1;actual_lock.release()
    lock=TrackedLock()
    monkeypatch.setattr(publishing,'account_lock',lambda _:lock)
    class Proxy:
        url='http://127.0.0.1:1'
        def __init__(self,*_):pass
        def close(self):assert threading.get_ident()==owner
    monkeypatch.setattr(browser,'PublicConnectProxy',Proxy)
    class FailedContext(Context):
        def __init__(self):super().__init__();self.first=not contexts;self.closes=0;contexts.append(self)
        def route(self,*args):
            assert threading.get_ident()==owner
            if self.first and setup_failure=='route':raise RuntimeError('SECRET route')
        def new_page(self):
            if self.first and setup_failure=='page':raise RuntimeError('SECRET page')
            return object()
        def close(self):
            assert threading.get_ident()==owner;self.closes+=1
            if self.first and cleanup_failure=='context' and not ready:raise RuntimeError('SECRET context')
            self.closed=True
    class Driver:
        def __init__(self):self.stops=0;drivers.append(self);self.chromium=SimpleNamespace(launch_persistent_context=lambda *a,**k:FailedContext())
        def stop(self):
            assert threading.get_ident()==owner;self.stops+=1
            if self is drivers[0] and cleanup_failure=='driver' and not ready:raise RuntimeError('SECRET driver')
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=lambda:SimpleNamespace(start=Driver)))
    def restore(context,*_):
        if context.first and setup_failure=='session':raise DownloadError('identity_mismatch')
    monkeypatch.setattr(sessions,'restore_consumer_session',restore)
    managed=SimpleNamespace(load=lambda _:SimpleNamespace(expected_uid=None))
    pool=DownloadBrowserPool(tmp_path,managed_sessions=managed)
    try:
        with pytest.raises(DownloadError) as error:pool.acquire(identity)
        assert error.value.code==('identity_mismatch' if setup_failure=='session' else 'network')
        pending=getattr(pool,'_pending_lease',None)
        assert pending is not None and pending.closed
        assert pending.cleanup_errors==(cleanup_failure+'_cleanup_failed',)
        assert 'SECRET' not in repr(pending.cleanup_errors)
        assert actual_lock.locked() and lock.releases==0
        with pytest.raises(DownloadError) as error:pool.acquire(identity)
        assert error.value.code=='account_busy'
        assert len(contexts)==len(drivers)==1 and lock.releases==0
        assert (contexts[0].closes if cleanup_failure=='context' else drivers[0].stops)==2
        ready=True
        lease=pool.acquire(identity)
        assert pool._pending_lease is None and pending.cleanup_errors==()
        assert lock.releases==1 and len(contexts)==len(drivers)==2
        pending.close();assert lock.releases==1
        lease.close();assert lock.releases==2 and not actual_lock.locked()
        assert pool.retry_pending_cleanup() and len(contexts)==2
    finally:
        ready=True
        if hasattr(pool,'retry_pending_cleanup'):pool.retry_pending_cleanup()
        if 'lease' in locals():lease.close()
        elif actual_lock.locked():actual_lock.release()  # RED-only synthetic lock teardown.

def test_cancel_and_media_revoke():
    registry=SourceRegistry()
    p=DouyinProvider(registry,Reader({'aweme_detail':work(1)}))
    source=normalize_inputs('https://www.douyin.com/video/1')[0]
    candidate=p.resolve(source,None,cancel=Token()).candidates[0]
    handle=p.media(candidate,cancel=Token())
    p.close()
    with pytest.raises(DownloadError): registry.lookup(handle)
    class Cancelled:
        def cancelled(self): return True
    with pytest.raises(DownloadError) as e: p.resolve(source,None,cancel=Cancelled())
    assert e.value.code=='cancelled'

def test_cancel_interrupts_navigation_and_releases_lease(tmp_path):
    cancelled=threading.Event()
    class Cancel:
        def cancelled(self): return cancelled.is_set()
    class Page:
        def on(self,*args): pass
        def remove_listener(self,*args): pass
        def goto(self,*args,**kwargs):
            assert lease.proxy.stopped.wait(2)
            raise RuntimeError('navigation disconnected https://private.invalid/?SECRET')
    class NavigationContext(Context):
        def new_page(self): return Page()
    pool=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:NavigationContext())
    lease=pool.acquire(None)
    timer=threading.Timer(.05,cancelled.set);timer.start()
    with pytest.raises(DownloadError) as e: lease.read('https://www.douyin.com/video/1',cancel=Cancel())
    timer.join()
    assert e.value.code=='cancelled'
    assert lease.closed and lease.context.closed and lease.proxy.closed
    again=pool.acquire(None);again.close()

@pytest.mark.parametrize('patch',[{'video':{'play_addr':[]}}, {'video':{'play_addr':{'url_list':'private'}}}, {'author':[]}, {'video':{'play_addr':{'url_list':[{}]}}}])
def test_changed_fields_are_finite_failures(patch):
    p=DouyinProvider(SourceRegistry(),Reader({'aweme_detail':dict(work(1),**patch)}))
    with pytest.raises(DownloadError) as e: p.resolve(normalize_inputs('https://www.douyin.com/video/1')[0],None,cancel=Token())
    assert e.value.code=='metadata_missing'

def test_only_observed_anonymous_bootstrap_post_is_allowed():
    from yijianjifa.features.downloads.browser import request_allowed
    base=dict(resource_type='xhr',navigation=False,frame_url='https://www.douyin.com/video/1')
    assert request_allowed('POST','https://mcs.zijieapi.com/webid',**base,anonymous_probe_initializers=True)
    assert request_allowed('POST','https://mcs.zijieapi.com/webid?aid=6383',**base,anonymous_probe_initializers=True)
    for url in ['https://mcs.zijieapi.com/upload','https://creator.douyin.com/publish','https://www.douyin.com/passport/login','https://mcs.zijieapi.com.evil.invalid/webid']:
        assert not request_allowed('POST',url,**base)
    assert not request_allowed('POST','https://mcs.zijieapi.com/webid',**dict(base,frame_url='https://evil.invalid/'))

def test_profile_does_not_accept_unrelated_video_detail():
    provider=DouyinProvider(SourceRegistry(),Reader({'aweme_detail':work(1)}))
    with pytest.raises(DownloadError) as e: provider.resolve(normalize_inputs('https://www.douyin.com/user/a')[0],None,cancel=Token())
    assert e.value.code=='metadata_missing'

def test_security_initializer_is_explicit_anonymous_probe_only():
    from yijianjifa.features.downloads.browser import request_allowed
    base=dict(resource_type='xhr',navigation=False,frame_url='https://www.douyin.com/video/1')
    url='https://mssdk.bytedance.com/web/r/token'
    assert not request_allowed('POST',url,**base)
    assert request_allowed('POST',url,**base,anonymous_probe_initializers=True)
    for bad in ['http://mssdk.bytedance.com/web/r/token','https://mssdk.bytedance.com/other','https://mssdk.bytedance.com.evil.invalid/web/r/token']:
        assert not request_allowed('POST',bad,**base,anonymous_probe_initializers=True)
    assert not request_allowed('POST',url,**dict(base,frame_url='https://evil.invalid/'),anonymous_probe_initializers=True)


def test_experiment_config_is_only_an_explicit_probe_read():
    from yijianjifa.features.downloads.browser import request_allowed
    base=dict(resource_type='xhr',navigation=False,frame_url='https://www.douyin.com/video/1')
    url='https://www.douyin.com/service/2/abtest_config/'
    assert not request_allowed('POST',url,**base)
    assert request_allowed('POST',url,**base,anonymous_probe_initializers=True)
    assert not request_allowed('POST',url.replace('abtest_config','publish'),**base,anonymous_probe_initializers=True)

def test_native_metadata_is_bound_to_the_authorized_work():
    from yijianjifa.features.downloads.browser import select_work_metadata
    assert select_work_metadata({'data':{'changed_field':work(1)}},'1')['aweme_detail']['aweme_id']=='1'
    assert select_work_metadata({'aweme_list':[work(2),work(3)]},'1') is None

def test_verification_event_is_finite_and_close_never_means_success(tmp_path):
    class Locator:
        def count(self): return 0
    class Page:
        url='https://www.douyin.com/user/public-author'
        def on(self,*a): pass
        def remove_listener(self,*a): pass
        def goto(self,*a,**kw): pass
        def locator(self,*a): return Locator()
        def evaluate(self,code): return None if '_ROUTER_DATA' in code else 'login'
    class LoginContext(Context):
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:LoginContext()).acquire(None)
    lease._verification_deadline=__import__('time').monotonic()+600
    lease._verification_check=True  # An explicit login verification check, not a marketing modal.
    with pytest.raises(DownloadError) as e: lease.read(Page.url,cancel=Token())
    assert e.value.code=='verification_required'
    assert lease.verification_event.reason=='login'
    assert lease.verification_event.state=='required'
    with pytest.raises(DownloadError): lease.read(Page.url,cancel=Token())
    lease.close()
    assert lease.verification_event.state=='cancelled'

def test_interactive_auth_is_explicit_and_cannot_write_content():
    from yijianjifa.features.downloads.browser import request_allowed
    base=dict(resource_type='xhr',navigation=False,frame_url='https://www.douyin.com/user/a')
    auth='https://sso.douyin.com/passport/web/sms/login/'
    assert not request_allowed('POST',auth,**base)
    assert request_allowed('POST',auth,**base,interactive=True)
    assert request_allowed('OPTIONS',auth,**base,interactive=True,preflight_method='POST')
    assert not request_allowed('OPTIONS',auth,**base,interactive=True,preflight_method='PUT')
    for url in ['https://creator.douyin.com/publish','https://www.douyin.com/aweme/v1/web/aweme/post/','https://sso.douyin.com/passport/web/password/change/','https://sso.douyin.com/passport/web/account/delete/']:
        assert not request_allowed('POST',url,**base,interactive=True)
    assert not request_allowed('POST',auth,**dict(base,frame_url='https://evil.invalid/'),interactive=True)


def test_headers_event_never_waits_for_unfinished_body(tmp_path):
    handlers={}
    class Response:
        url='https://www.douyin.com/aweme/v1/web/aweme/post/'
        status=200
        headers={'content-type':'application/json'}
        touched=False
        def body(self): self.touched=True; return b'{}'
    response=Response()
    class Page:
        url='https://www.douyin.com/user/a'
        def on(self,name,callback): handlers[name]=callback
        def remove_listener(self,*args): pass
        def goto(self,*args,**kwargs):
            handlers['response'](response)
            raise DownloadError('metadata_missing')
    class C(Context):
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:C()).acquire(None)
    with pytest.raises(DownloadError): lease.read(Page.url,cancel=Token())
    lease.close()
    assert not response.touched

def test_websocket_is_held_without_connecting_or_sync_close(tmp_path):
    class C(Context):
        def route_web_socket(self,pattern,handler):
            assert pattern=='**/*'
            class Socket:
                def close(self): raise AssertionError('sync reentry')
                def connect_to_server(self): raise AssertionError('outbound websocket')
            handler(Socket())
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:C()).acquire(None)
    lease.close()

@pytest.mark.parametrize('proof',[False,True])
def test_manual_resume_requires_identity_and_readable_works(tmp_path,proof):
    class Page:
        def goto(self,*a,**kw): pass
        def is_closed(self): return False
    class C(Context):
        def add_init_script(self,*a): pass
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:C(),visible=True).acquire(None)
    event=lease.begin_verification('https://www.douyin.com/user/a')
    def read(url,*,cancel):
        assert not lease.interactive
        if proof: lease._identity='123'
        return {'aweme_list':[work(1)]}
    lease.read=read
    if proof:
        assert lease.complete_verification(cancel=Token()).state=='resolved'
        assert not lease.interactive
    else:
        with pytest.raises(DownloadError) as error: lease.complete_verification(cancel=Token())
        assert error.value.code=='verification_required'
        assert lease.verification_event.id==event.id and lease.interactive
    lease.close()

@pytest.mark.parametrize('closed,expired',[(True,False),(False,True)])
def test_manual_window_close_and_deadline_cancel_resources(tmp_path,closed,expired):
    class Page:
        def goto(self,*a,**kw): pass
        def is_closed(self): return closed
    class C(Context):
        def add_init_script(self,*a): pass
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:C(),visible=True).acquire(None)
    lease.begin_verification('https://www.douyin.com/user/a')
    if expired: lease._verification_deadline=0
    with pytest.raises(DownloadError): lease.poll_verification(cancel=Token())
    assert lease.closed and lease.proxy.closed
    assert lease.verification_event.state=='cancelled'

def test_next_page_is_explicit_native_read_and_deduplicated():
    class Paged(Reader):
        calls=0
        def read_next(self,url,*,cancel):
            self.calls+=1
            return post([work(20),work(21)], False, 20, 10, 'public-author')
    reader=Paged(post([work(i) for i in range(1,21)], True, 10, author='public-author'))
    provider=DouyinProvider(SourceRegistry(),reader)
    source=normalize_inputs('https://www.douyin.com/user/public-author')[0]
    first=provider.resolve(source,None,cancel=Token())
    assert first.has_more and reader.calls==0
    second=provider.resolve(source,first.cursor,cancel=Token())
    assert [c.work_id for c in second.candidates]==['21']
    assert not second.has_more and reader.calls==1

@pytest.mark.parametrize('url',['https://mcs.zijieapi.com/webid','https://mssdk.bytedance.com/web/r/token','https://www.douyin.com/service/2/abtest_config/'])
def test_all_initializers_require_unauthed_explicit_probe(url):
    from yijianjifa.features.downloads.browser import request_allowed
    base=dict(resource_type='xhr',navigation=False,frame_url='https://www.douyin.com/video/1')
    for method in ('POST','OPTIONS'):
        opts=dict(base,preflight_method='POST')
        assert not request_allowed(method,url,**opts)
        assert not request_allowed(method,url,**opts,anonymous_probe_initializers=True,interactive=True)
        assert not request_allowed(method,url,**opts,anonymous_probe_initializers=True,authenticated=True)
        assert request_allowed(method,url,**opts,anonymous_probe_initializers=True)

def test_probe_never_reuses_authenticated_profile(tmp_path):
    paths=[]
    pool=DownloadBrowserPool(tmp_path,launch=lambda p,**kw:(paths.append(p) or Context()),anonymous_probe_initializers=True)
    with pytest.raises(DownloadError): pool.acquire('named')
    pool.acquire(None).close();pool.acquire(None).close()
    assert paths[0]!=paths[1]

@pytest.mark.parametrize('failure',['context','driver','proxy'])
def test_close_failures_retain_lock_until_browser_exit_and_are_retryable(tmp_path,failure):
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:Context()).acquire('cleanup-'+failure)
    calls=[]
    class Broken:
        n=0
        def close(self):
            calls.append(failure);self.n+=1
            if self.n==1: raise RuntimeError('https://secret.invalid/?token=SECRET')
        stop=close
    old=getattr(lease,failure)
    if old is not None:
        (old.stop if failure=='driver' else old.close)()
    setattr(lease,failure,Broken())
    try:
        lease.close()
        lock=account_lock('cleanup-'+failure)
        acquired=lock.acquire(blocking=False)
        if acquired:lock.release()
        assert acquired is (failure=='proxy')
        assert lease.closed and lease.cleanup_errors==(failure+'_cleanup_failed',)
        assert 'SECRET' not in repr(lease.cleanup_errors)
        lease.close()
        assert calls==[failure,failure] and lease.cleanup_errors==()
        assert lock.acquire(blocking=False);lock.release()
        lease.close();assert calls==[failure,failure]
    finally:lease.close()


@pytest.fixture
def known_cleanup_child():
    """Own exactly this subprocess group; no browser/PID discovery or user process."""
    import os
    import select
    import signal
    import subprocess
    import sys
    if os.name!='posix':pytest.skip('independent POSIX group fixture; Windows execution pending')
    child=subprocess.Popen([sys.executable,'-c',"import signal; print('ready',flush=True); signal.pause()"],
                           stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                           shell=False,close_fds=True,start_new_session=True)
    def stop_known_child():
        if child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)
    try:
        assert select.select([child.stdout],[],[],3)[0] and child.stdout.readline()==b'ready\n'
        assert child.poll() is None and os.getpgid(child.pid)==child.pid
        yield child,stop_known_child
    finally:
        stop_known_child();child.stdout.close()


def test_live_owned_child_cleanup_failure_holds_account_lock_until_explicit_reap(tmp_path,known_cleanup_child):
    import os
    child,stop_known_child=known_cleanup_child
    calls=[]
    class ChildContext(Context):
        def close(self):
            calls.append('context')
            if child.poll() is None:raise RuntimeError('https://private.invalid/?token=SECRET')
            super().close()
    class ChildDriver:
        def stop(self):
            calls.append('driver')
            if child.poll() is None:raise RuntimeError('SECRET driver cleanup')
    identity='owned-cleanup-'+tmp_path.name
    pool=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:ChildContext())
    lease=pool.acquire(identity);lease.driver=ChildDriver()
    actual_lock=lease.lock
    class TrackedLock:
        releases=0
        def release(self):self.releases+=1;actual_lock.release()
    lease.lock=TrackedLock()
    try:
        lease.close()
        assert child.poll() is None and os.getpgid(child.pid)==child.pid
        assert lease.closed and not lease.context.closed and lease.lock.releases==0
        assert lease.cleanup_errors==('context_cleanup_failed','driver_cleanup_failed')
        assert 'SECRET' not in repr(lease.cleanup_errors)
        with pytest.raises(DownloadError) as error:pool.acquire(identity)
        assert error.value.code=='account_busy'
        for method in (lease.read,lease.read_next):
            with pytest.raises(DownloadError) as error:method('https://www.douyin.com/user/a',cancel=Token())
            assert error.value.code=='source_missing'
        lease.close()
        assert calls==['context','driver','context','driver'] and lease.lock.releases==0
        stop_known_child()
        assert child.returncode is not None
        with pytest.raises(ProcessLookupError):os.killpg(child.pid,0)
        lease.close();lease.close()
        assert lease.context.closed and lease.cleanup_errors==() and lease.lock.releases==1
        assert calls==['context','driver']*3
        assert actual_lock.acquire(blocking=False);actual_lock.release()
    finally:
        stop_known_child();lease.close()

@pytest.mark.parametrize('mode',['expired','closed','during'])
def test_complete_verification_cannot_outlive_deadline(tmp_path,mode):
    class Page:
        def goto(self,*a,**kw): pass
        def is_closed(self): return mode=='closed'
    class C(Context):
        def add_init_script(self,*a): pass
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:C(),visible=True).acquire(None)
    lease.begin_verification('https://www.douyin.com/user/a')
    if mode=='expired': lease._verification_deadline=0
    called=[]
    def read(*a,**kw):
        called.append(True);lease._identity='123';lease._verification_deadline=0
        return {'aweme_list':[work(1)]}
    lease.read=read
    with pytest.raises(DownloadError) as error: lease.complete_verification(cancel=Token())
    assert error.value.code==('cancelled' if mode=='closed' else 'timeout')
    assert bool(called)==(mode=='during')
    assert lease.closed and lease.verification_event.state=='cancelled'

def test_verification_read_uses_remaining_budget(tmp_path):
    import time
    class Page:
        url='https://www.douyin.com/user/a'
        def is_closed(self): return False
        def on(self,*a): pass
        def remove_listener(self,*a): pass
        def goto(self,*a,**kw):
            if 'timeout' in kw and lease._verification_check:
                assert 0<kw['timeout']<=50
                time.sleep(.07)
    class C(Context):
        def add_init_script(self,*a): pass
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:C(),visible=True).acquire(None)
    lease.begin_verification(Page.url)
    lease._verification_deadline=time.monotonic()+.05
    with pytest.raises(DownloadError) as error: lease.complete_verification(cancel=Token())
    assert error.value.code=='timeout' and lease.closed

def test_original_cancel_survives_cleanup_failure(tmp_path):
    class C(Context):
        attempts=0
        def close(self):
            self.attempts+=1
            if self.attempts==1:raise RuntimeError('https://private/?token=SECRET')
            super().close()
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:C()).acquire('cancel-cleanup')
    class Cancel:
        def cancelled(self): return True
    try:
        with pytest.raises(DownloadError) as error: lease.read('https://www.douyin.com/video/1',cancel=Cancel())
        assert str(error.value)=='cancelled' and lease.cleanup_errors==('context_cleanup_failed',)
        lock=account_lock('cancel-cleanup');assert not lock.acquire(blocking=False)
        lease.close()
        assert lease.cleanup_errors==() and lease.context.closed
        assert lock.acquire(blocking=False);lock.release()
    finally:lease.close()

@pytest.mark.parametrize('mode,allowed',[('default',False),('named',False),('probe',True),('interactive',False),('authenticated',False)])
def test_lease_routes_apply_initializer_scope(tmp_path,mode,allowed):
    pool=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:Context(),anonymous_probe_initializers=mode in ('probe','interactive','authenticated'))
    lease=pool.acquire('named-test' if mode=='named' else None)
    lease.interactive=mode=='interactive'
    if mode=='authenticated':lease._identity='123'
    outcomes=[]
    class Request:
        method='POST';url='https://mcs.zijieapi.com/webid';resource_type='xhr';headers={}
        frame=type('Frame',(),{'url':'https://www.douyin.com/video/1'})()
        def is_navigation_request(self):return False
    class Route:
        request=Request()
        def abort(self):outcomes.append(False)
        def continue_(self):outcomes.append(True)
    lease.context.routes[0][1](Route())
    assert outcomes==[allowed]
    lease.close()

@pytest.mark.parametrize('final', ['https://www.douyin.com/video/1/', 'https://www.iesdouyin.com/share/video/1/'])
def test_share_reader_provenance_accepts_normal_redirect(final):
    from yijianjifa.features.downloads.browser import select_video_metadata
    source=normalize_inputs('分享 https://v.douyin.com/allowed/')[0]
    payload=select_video_metadata({'aweme_detail':work(1), '_video_source':'forged', '_video_work_id':'2'}, final, source.canonical_source)
    assert payload['_video_source']==source.canonical_source and payload['_video_work_id']=='1'
    assert DouyinProvider(SourceRegistry(),Reader(payload)).resolve(source,None,cancel=Token()).candidates[0].work_id=='1'

@pytest.mark.parametrize('final', ['https://evil.invalid/video/1', 'https://www.douyin.com/video/1/extra', 'https://v.douyin.com/video/1', 'http://www.douyin.com/video/1', 'https://www.douyin.com/video/2'])
def test_share_invalid_or_different_redirect_cannot_supply_detail(final):
    from yijianjifa.features.downloads.browser import select_video_metadata
    assert select_video_metadata({'aweme_detail':work(1)},final,'https://v.douyin.com/allowed/') is None

@pytest.mark.parametrize('markers', [{}, {'_video_source':'https://v.douyin.com/other/','_video_work_id':'1'}, {'_video_source':'https://v.douyin.com/allowed/','_video_work_id':'2'}])
def test_share_requires_matching_reader_provenance(markers):
    source=normalize_inputs('https://v.douyin.com/allowed/')[0]
    with pytest.raises(DownloadError) as error:
        DouyinProvider(SourceRegistry(),Reader(dict(aweme_detail=work(1),**markers))).resolve(source,None,cancel=Token())
    assert error.value.code=='metadata_missing'

@pytest.mark.parametrize('identity', [1,2])
def test_direct_full_trailing_slash_retains_exact_work_binding(identity):
    source=normalize_inputs('https://www.douyin.com/video/1/')[0]
    provider=DouyinProvider(SourceRegistry(),Reader({'aweme_detail':work(identity)}))
    if identity==1: assert provider.resolve(source,None,cancel=Token()).candidates[0].work_id=='1'
    else:
        with pytest.raises(DownloadError): provider.resolve(source,None,cancel=Token())

def test_profile_trailing_slash_keeps_author_and_cursor_binding():
    from yijianjifa.features.downloads.browser import select_profile_metadata
    source=normalize_inputs('https://www.douyin.com/user/public-author/')[0]
    payload=select_profile_metadata({'status_code':0,'aweme_list':[work(1)],'has_more':0,'max_cursor':'1'},'https://www.douyin.com/aweme/v1/web/aweme/post?sec_user_id=public-author&max_cursor=0',source.canonical_source)
    assert DouyinProvider(SourceRegistry(),Reader(payload)).resolve(source,None,cancel=Token()).candidates[0].work_id=='1'

def test_browser_read_issues_source_provenance_from_normal_page(tmp_path):
    class Page:
        url='about:blank'
        def on(self,*args): pass
        def remove_listener(self,*args): pass
        def goto(self,url,**kwargs): self.url='https://www.douyin.com/video/1/'
        def evaluate(self,script): return {'aweme_detail':work(1), '_video_source':'forged','_video_work_id':'2'}
        def locator(self,*args): return type('Locator',(),{'count':lambda self:0})()
    class ReadContext(Context):
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,launch=lambda *a,**kw:ReadContext()).acquire(None)
    try:
        source=normalize_inputs('https://v.douyin.com/allowed/')[0]
        result=DouyinProvider(SourceRegistry(),lease).resolve(source,None,cancel=Token())
        assert result.candidates[0].work_id=='1'
    finally: lease.close()

@pytest.mark.parametrize('page', ['https://evil.invalid/user/a','https://www.douyin.com/user/a//','https://www.douyin.com/user/a/extra'])
def test_profile_page_equivalence_stays_bounded(page):
    from yijianjifa.features.downloads.browser import same_profile_page
    assert not same_profile_page(page,'https://www.douyin.com/user/a/')
