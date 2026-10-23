import json
from types import SimpleNamespace
import pytest
from yijianjifa.features.downloads.account_session import ManagedDownloadSessions
from yijianjifa.features.downloads.browser import DownloadBrowserPool
from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.webui.portable_session import encode_session, decode_session
from yijianjifa.webui.chromium_publish import account_lock

DOMAINS=('douyin.com','creator.douyin.com')
COOKIE=dict(name='secret',value='PRIVATE_COOKIE',domain='.douyin.com',path='/',secure=True,httpOnly=True)
class Service:
    def __init__(self, account): self.account=account; self.reads=[]
    def default(self, platform): self.reads.append('default'); return self.account
    def get(self, id): self.reads.append(id); return self.account
class Context:
    def __init__(self): self.rows=[]; self.events=[]; self.closed=False
    def route(self,*a): self.events.append('route')
    def cookies(self): return self.rows
    def clear_cookies(self): self.events.append('clear'); self.rows=[]
    def add_cookies(self,rows): self.events.append('restore'); self.rows.extend(rows)
    def new_page(self): self.events.append('page'); return object()
    def close(self): self.closed=True
@pytest.fixture
def setup(tmp_path):
    store=SessionStore(tmp_path/'source')
    ref=store.put('managed',encode_session([COOKIE],'123',DOMAINS),versioned=True)
    account=SimpleNamespace(id='managed',platform='douyin',platform_user_id='123',login_state='valid',session_ref=ref)
    service=Service(account)
    adapter=ManagedDownloadSessions(service,store,decoder=lambda blob,uid:decode_session(blob,uid,DOMAINS))
    return service,store,adapter

def test_default_restore_source_immutable_and_rotation(tmp_path,setup):
    service,store,adapter=setup
    before=store.get('managed',ref=service.account.session_ref)
    context=Context()
    pool=DownloadBrowserPool(tmp_path,managed_sessions=adapter,launch=lambda *a,**k:context)
    lease=pool.acquire(None)
    assert context.events==['route','clear','restore','page']
    assert context.rows==[COOKIE]
    assert 'PRIVATE_COOKIE' not in repr(adapter.load('managed'))
    assert '123' not in repr(adapter.load('managed'))
    assert service.account.session_ref not in repr(adapter.load('managed'))
    lease.close()
    context.rows=[]
    lease=pool.acquire(None); assert context.rows==[COOKIE]; lease.close()
    context.rows[0]['value']='rotated'
    lease=pool.acquire(None); assert context.rows[0]['value']=='rotated'; lease.close()
    assert store.get('managed',ref=service.account.session_ref)==before
    assert service.account.login_state=='valid'
    service.account.session_ref=store.put('managed',before,versioned=True)
    lease=pool.acquire(None); assert context.rows[0]['value']=='PRIVATE_COOKIE'; lease.close()

@pytest.mark.parametrize('field,value,code',[('platform','other','account_unavailable'),('login_state','removed','account_unavailable'),('platform_user_id','notnumeric','session_invalid'),('platform_user_id','456','session_invalid'),('session_ref',None,'session_missing')])
def test_invalid_account(setup,field,value,code):
    service,store,adapter=setup; setattr(service.account,field,value)
    with pytest.raises(DownloadError) as e: adapter.load('managed')
    assert str(e.value)==code

def test_missing_corrupt_and_lock_cleanup(tmp_path,setup):
    service,store,adapter=setup
    store.put('managed',b'PRIVATE_BAD_BLOB')
    service.account.session_ref=store.put('managed',b'PRIVATE_BAD_BLOB',versioned=True)
    pool=DownloadBrowserPool(tmp_path,managed_sessions=adapter,launch=lambda *a,**k:Context())
    with pytest.raises(DownloadError) as e: pool.acquire(None)
    assert str(e.value)=='session_invalid'
    lock=account_lock('managed'); assert lock.acquire(False); lock.release()
    store.delete('managed')
    with pytest.raises(DownloadError) as e: pool.acquire(None)
    assert str(e.value)=='session_missing'

def test_probe_never_reads_managed(tmp_path,setup):
    service,store,adapter=setup
    lease=DownloadBrowserPool(tmp_path,managed_sessions=adapter,anonymous_probe_initializers=True,launch=lambda *a,**k:Context()).acquire(None)
    assert service.reads==[]; lease.close()

def test_restore_failure_releases_lock(tmp_path,setup):
    class Fail(Context):
        def add_cookies(self,rows): raise RuntimeError('PRIVATE_COOKIE')
    context=Fail()
    with pytest.raises(DownloadError) as e: DownloadBrowserPool(tmp_path,managed_sessions=setup[2],launch=lambda *a,**k:context).acquire(None)
    assert str(e.value)=='session_invalid' and context.closed
    lock=account_lock('managed'); assert lock.acquire(False); lock.release()

@pytest.mark.parametrize('uid',['123','456',None])
def test_managed_read_requires_observed_self(tmp_path,setup,uid):
    class Page:
        url='about:blank'
        def __init__(self): self.handlers={}
        def on(self,name,handler): self.handlers[name]=handler
        def remove_listener(self,name,handler): pass
        def goto(self,url,**kwargs):
            self.url=url
            if uid is not None:
                response=SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/user/profile/self',headers={},body=lambda:json.dumps({'status_code':0,'user':{'uid':uid,'sec_uid':'test','nickname':'test'}}).encode())
                self.handlers['requestfinished'](SimpleNamespace(url=response.url,response=lambda:response))
        def evaluate(self,script): return {'aweme_detail':{'aweme_id':'1','video':{}}} if '_ROUTER_DATA' in script else None
        def locator(self,*a): return SimpleNamespace(count=lambda:0)
        def wait_for_timeout(self,ms):
            # Exercise the finite end-of-read missing-identity path without a real wait.
            lease._identity = None
            raise DownloadError('identity_mismatch')
    class ReadContext(Context):
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,managed_sessions=setup[2],launch=lambda *a,**k:ReadContext()).acquire(None)
    try:
        if uid=='123': assert lease.read('https://www.douyin.com/video/1',cancel=SimpleNamespace(cancelled=lambda:False))['aweme_detail']['aweme_id']=='1'
        else:
            with pytest.raises(DownloadError) as e: lease.read('https://www.douyin.com/video/1',cancel=SimpleNamespace(cancelled=lambda:False))
            assert e.value.code=='identity_mismatch'
    finally: lease.close()

def test_sqlite_and_source_archive_unchanged(tmp_path):
    from sqlalchemy import create_engine
    from yijianjifa.storage.models import Base
    from yijianjifa.features.accounts.service import AccountService, AccountRow
    from yijianjifa.storage.db import session_scope
    db=tmp_path/'accounts.sqlite'; engine=create_engine('sqlite:///'+str(db)); Base.metadata.create_all(engine)
    service=AccountService(engine); account=service.create('douyin','123','synthetic')
    store=SessionStore(tmp_path/'profiles'); ref=store.put(account.id,encode_session([COOKIE],'123',DOMAINS),versioned=True)
    with session_scope(engine) as session:
        row=session.get(AccountRow,account.id); row.is_default=True; row.session_ref=ref
    before=db.read_bytes(); files={p:p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
    adapter=ManagedDownloadSessions(service,store,decoder=lambda blob,uid:decode_session(blob,uid,DOMAINS))
    lease=DownloadBrowserPool(tmp_path,managed_sessions=adapter,launch=lambda *a,**k:Context()).acquire(None); lease.close()
    assert db.read_bytes()==before
    assert all(p.read_bytes()==content for p,content in files.items())
    engine.dispose()

def test_default_absent_explicit_and_busy(tmp_path,setup):
    service,store,adapter=setup
    lock=account_lock('managed'); lock.acquire()
    try:
        with pytest.raises(DownloadError) as e: DownloadBrowserPool(tmp_path,managed_sessions=adapter).acquire('managed')
        assert e.value.code=='account_busy' and service.reads==[]
    finally: lock.release()
    service.account=None
    lease=DownloadBrowserPool(tmp_path,managed_sessions=adapter,launch=lambda *a,**k:Context()).acquire(None)
    assert lease._expected_uid is None; lease.close()

def test_native_mac_synthetic_archive_restore():
    import subprocess
    from pathlib import Path
    runner=Path('.venv-webui-probe312/bin/python')
    if not runner.exists(): pytest.skip('native Mac runtime unavailable')
    script='''
import tempfile
from pathlib import Path
from types import SimpleNamespace
import Foundation as F
from yijianjifa.webui.cocoa_session import encode_cookies
from yijianjifa.webui.douyin_auth import DOMAINS
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.features.downloads.account_session import ManagedDownloadSessions, restore_consumer_session
with tempfile.TemporaryDirectory() as root:
    root=Path(root); store=SessionStore(root/'source')
    cookie=F.NSHTTPCookie.cookieWithProperties_({F.NSHTTPCookieName:'synthetic',F.NSHTTPCookieValue:'synthetic',F.NSHTTPCookieDomain:'.douyin.com',F.NSHTTPCookiePath:'/'})
    blob=encode_cookies([cookie],identity='123',domains=DOMAINS)
    ref=store.put('synthetic',blob,versioned=True)
    account=SimpleNamespace(id='synthetic',platform='douyin',platform_user_id='123',login_state='valid',session_ref=ref)
    adapter=ManagedDownloadSessions(SimpleNamespace(get=lambda id:account,default=lambda platform:account),store)
    class Context:
        rows=[]
        def cookies(self): return self.rows
        def clear_cookies(self): self.rows=[]
        def add_cookies(self,rows): self.rows.extend(rows)
    context=Context(); snapshot=adapter.load('synthetic')
    restore_consumer_session(context,root/'consumers'/'synthetic',snapshot)
    assert len(context.rows)==1 and context.rows[0]['name']=='synthetic'
    context.rows[0]['value']='rotated'
    restore_consumer_session(context,root/'consumers'/'synthetic',adapter.load('synthetic'))
    assert context.rows[0]['value']=='rotated'
    assert store.get('synthetic',ref=ref)==blob and account.session_ref==ref and account.login_state=='valid'
print('native_restore_ok')
'''
    result=subprocess.run([str(runner),'-c',script],capture_output=True,text=True,timeout=20)
    assert result.returncode==0, 'native synthetic restore failed'
    assert result.stdout.strip()=='native_restore_ok'

@pytest.mark.parametrize('followup', ['next', 'trailing_slash', 'new_read', 'other_source', 'changed_page', 'mismatch', 'uncompleted', 'reopened'])
def test_managed_pagination_reuses_only_verified_same_source(tmp_path,setup,followup):
    class Page:
        url='about:blank'
        def __init__(self): self.handlers={}; self.mouse=SimpleNamespace(wheel=lambda *a:self.emit(False))
        def on(self,name,handler): self.handlers[name]=handler
        def remove_listener(self,name,handler): pass
        def emit(self,first):
            if first or followup=='mismatch':
                self.deliver('/aweme/v1/web/user/profile/self',{'status_code':0,'user':{'uid':'123' if first else '456','sec_uid':'test','nickname':'test'}})
            self.deliver('/aweme/v1/web/aweme/post?sec_user_id=synthetic&max_cursor=0',{'aweme_list':[{'aweme_id':'1','video':{}}]})
        def deliver(self,path,data):
            result=SimpleNamespace(url='https://www.douyin.com'+path,headers={'content-type':'application/json'},status=200,body=lambda:json.dumps(data).encode())
            self.handlers['requestfinished'](SimpleNamespace(url=result.url,response=lambda:result))
        def goto(self,url,**kwargs):
            first=self.url=='about:blank'; self.url=url; self.emit(first)
        def evaluate(self,script): return None
        def locator(self,*a): return SimpleNamespace(count=lambda:0)
        def wait_for_timeout(self,ms): raise DownloadError('identity_mismatch')
    class ReadContext(Context):
        def new_page(self): return Page()
    lease=DownloadBrowserPool(tmp_path,managed_sessions=setup[2],launch=lambda *a,**k:ReadContext()).acquire(None)
    source='https://www.douyin.com/user/synthetic/' if followup=='trailing_slash' else 'https://www.douyin.com/user/synthetic'
    try:
        assert lease.read(source,cancel=SimpleNamespace(cancelled=lambda:False))['aweme_list']
        if followup in ('next','trailing_slash'):
            lease.page.url=source.rstrip('/')
            assert lease.read_next(source,cancel=SimpleNamespace(cancelled=lambda:False))['aweme_list']
        else:
            if followup=='changed_page': lease.page.url='https://www.douyin.com/user/other'
            if followup=='uncompleted': lease._identity_source=None
            if followup=='reopened':
                lease.close()
                lease=DownloadBrowserPool(tmp_path,managed_sessions=setup[2],launch=lambda *a,**k:ReadContext()).acquire(None)
                lease.page.url=source
            with pytest.raises(DownloadError):
                if followup=='new_read': lease.read(source,cancel=SimpleNamespace(cancelled=lambda:False))
                elif followup=='other_source': lease.read('https://www.douyin.com/user/other',cancel=SimpleNamespace(cancelled=lambda:False))
                else: lease.read_next(source,cancel=SimpleNamespace(cancelled=lambda:False))
    finally: lease.close()
