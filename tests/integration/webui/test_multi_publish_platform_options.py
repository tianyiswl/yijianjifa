"""Reading creator configuration cannot grant or submit a delivery."""
import json
import threading
import time

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_platform_options import safe_options, options_message
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.storage.db import session_scope
from yijianjifa.app_services import bilibili_static_catalog as catalog


def test_options_are_coalesced_and_read_on_original_worker_without_upload(db_engine,app_paths):
    entered=threading.Event();release=threading.Event();threads=[]
    class Adapter:
        platform_id='bilibili';verified_capabilities=set()
        def submit(self,*a,**k):raise AssertionError('options must not submit')
        def reconcile(self,*a,**k):raise AssertionError('options must not query works')
        def get_cached_platform_options(self,account_id):return {'status':'empty'}
        def read_platform_options(self,account_id):
            threads.append(threading.get_ident());entered.set();assert release.wait(2)
            return {'status':'ready','categories':[{'id':1,'name':'真实分类','mode':'legacy','secret':'omit'}],
                'statements':[{'id':-1,'name':'不标记'}],'cookies':'omit','fetched_at':'2026-10-07T00:00:00+00:00'}
    api=DesktopApi(db_engine,app_paths,publish_adapters={'bilibili':Adapter()})
    account=AccountService(db_engine).create('bilibili','111','合成账号')
    def call(name,params,rid):
        return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':'multi_publish.platform_options.'+name,'params':params}),trusted_origin=True)
    try:
        first=call('refresh',{'account_id':account.id},'refresh-one')
        assert first['ok'] and entered.wait(2)
        again=call('refresh',{'account_id':account.id},'refresh-two')
        assert again['data']['ticket']==first['data']['ticket']
        assert call('status',{'ticket':first['data']['ticket']},'pending')['data']['status']=='loading'
        release.set();deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            done=call('status',{'ticket':first['data']['ticket']},'poll')['data']
            if done['status']!='loading':break
            time.sleep(.01)
        assert done['status']=='ready' and len(threads)==1 and threads[0]!=threading.get_ident()
        assert done['options']['statements']==[{'id':-1,'name':'不标记'}]
        assert 'omit' not in json.dumps(done)
        with session_scope(db_engine) as db:assert list(db.scalars(select(TaskCenterRow)))==[]
        assert not call('refresh',{'account_id':account.id,'endpoint':'https://invalid.test'},'injection')['ok']
    finally:
        release.set();assert api.close(3)


def test_bilibili_builtin_choices_do_not_require_login_adapter_or_a_browser_job(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths)
    account=AccountService(db_engine).create('bilibili','111','未登录合成账号')
    try:
        result=api._multi_publish.platform_options.cached(account.id)
        assert result['status']=='ready' and result['options']==catalog.builtin_options()
        assert result['options']['categories']==result['options']['statements']==[]
        assert '实际投稿表单' in result['message']
        assert not api._multi_publish.platform_options.jobs
        with session_scope(db_engine) as db:assert list(db.scalars(select(TaskCenterRow)))==[]
    finally:assert api.close(3)


def test_builtin_dto_rebuilds_only_reviewed_local_choices_and_never_dynamic_permissions():
    result=safe_options({'status':'ready','source':'builtin','catalog_version':catalog.VERSION,
        'category_choices':[{'choice':'human:injected','name':'注入名称','id':999}],
        'creation_statement_choices':[{'choice':'made_up','name':'注入'}],
        'schedule':{'min_lead_seconds':1},'token':'omit'})
    assert result==catalog.builtin_options() and 'omit' not in json.dumps(result)
    assert 'schedule' not in result and 'injected' not in repr(result)
    untrusted=safe_options({'status':'ready','source':'builtin','catalog_version':'wrong',
        'category_choices':[{'choice':'human:injected','name':'注入名称'}]})
    assert 'category_choices' not in untrusted and 'source' not in untrusted


@pytest.mark.parametrize('bad',[{'categories':{}},{'categories':'private'},{'statements':[{'id':True,'name':'not integer'}]},
    {'categories':[{'id':3,'name':'wrong mode','mode':'untrusted'}]},
    {'status':{},'category_mode':{},'categories':[{'id':3,'name':'wrong mode','mode':{}}]},
    {'status':'verification_required','verification_view_state':{}},
    {'status':'verification_required','verification_view_state':[]}])
def test_malformed_options_do_not_expose_or_invent_choices(bad):
    result=safe_options({'status':'ready',**bad,'signed_url':'omit','token':'omit'})
    assert result['categories']==[] and result['statements']==[]
    assert 'omit' not in json.dumps(result)


@pytest.mark.parametrize('kind,stage,expected',[
    ('login','local','本地缺少有效登录资料'),
    ('login','manager','内容管理页检测到登录提示'),
    ('sms','upload','投稿页检测到短信验证码'),
    ('complex','manager','内容管理页检测到图形或滑块验证'),
])
def test_verification_message_reports_observed_kind_without_declaring_expired(kind,stage,expected):
    result=safe_options({'status':'verification_required','verification_kind':kind,
        'verification_stage':stage,'url':'secret','cookies':'secret'})
    assert result['verification_kind']==kind and result['verification_stage']==stage
    assert expected in options_message(result) and '已过期' not in options_message(result)
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('status,kind,stage',[
    ('ready','login','upload'),('verification_required',{},[]),
    ('verification_required','secret','https://secret.test'),
])
def test_verification_diagnostics_are_bounded_and_only_present_when_required(status,kind,stage):
    result=safe_options({'status':status,'verification_kind':kind,'verification_stage':stage})
    assert 'verification_kind' not in result and 'verification_stage' not in result


def test_option_error_is_finite_and_does_not_leak_platform_exception(db_engine,app_paths):
    class Adapter:
        platform_id='bilibili';verified_capabilities=set()
        def submit(self,*a,**k):raise AssertionError('must not submit')
        def reconcile(self,*a,**k):raise AssertionError('must not query')
        def get_cached_platform_options(self,account_id):return {'status':'empty'}
        def read_platform_options(self,account_id):raise RuntimeError('secret-normal-response')
    api=DesktopApi(db_engine,app_paths,publish_adapters={'bilibili':Adapter()})
    account=AccountService(db_engine).create('bilibili','111','合成账号')
    try:
        jobs=api._multi_publish.platform_options
        first=jobs.refresh(account.id);deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            result=jobs.status(first['ticket'])
            if result['status']!='loading':break
            time.sleep(.01)
        assert result['status']=='error' and 'secret' not in json.dumps(result)
        assert '原选择和缓存仍保留' in result['message']
    finally:assert api.close(3)


def test_metadata_cancel_while_initial_read_is_running_closes_on_original_worker(db_engine,app_paths):
    entered=threading.Event();release=threading.Event();events=[]
    class Adapter:
        platform_id='bilibili';verified_capabilities=set()
        def submit(self,*args,**kwargs):raise AssertionError('metadata cannot submit')
        def reconcile(self,*args,**kwargs):raise AssertionError('metadata cannot query works')
        def get_cached_platform_options(self,aid):return {'status':'empty'}
        def read_platform_options(self,aid):raise AssertionError('ticket required')
        def begin_platform_options(self,aid,ticket):
            events.append(('begin',threading.get_ident()));entered.set();assert release.wait(2)
            return {'status':'verification_required','verification_available':True}
        def continue_platform_options(self,aid,ticket,action):
            events.append((action,threading.get_ident()));assert action=='cancel'
            return {'status':'cancelled','cleanup_complete':True}
    api=DesktopApi(db_engine,app_paths,publish_adapters={'bilibili':Adapter()})
    account=AccountService(db_engine).create('bilibili','111','合成账号')
    try:
        jobs=api._multi_publish.platform_options;ticket=jobs.refresh(account.id)['ticket'];assert entered.wait(2)
        assert jobs.cancel(ticket)['status']=='cancelling';release.set()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and jobs.status(ticket)['status']!='cancelled':time.sleep(.01)
        result=jobs.status(ticket);assert result['status']=='cancelled' and not jobs.busy_accounts()
        assert [e[0] for e in events]==['begin','cancel'] and len({e[1] for e in events})==1
        assert 'verification_id' not in result
    finally:release.set();assert api.close(3)


def test_failed_metadata_cancel_enqueue_retains_recoverable_original_handle(db_engine):
    from types import SimpleNamespace
    from yijianjifa.app_services.multi_publish_platform_options import PlatformOptionsJobs
    def unavailable(*args):raise ValueError('app closing')
    jobs=PlatformOptionsJobs(SimpleNamespace(enqueue=unavailable))
    jobs.jobs['t']={'account_id':'a','status':'verification_required','cancel_requested':False,
        'retained':True,'verification_id':'n','expires':time.monotonic()+30,'message':'original'}
    with pytest.raises(ValueError):jobs.cancel('t')
    assert jobs.status('t')['status']=='verification_required' and jobs.busy_accounts()=={'a'}
    assert jobs.status('t')['verification_id']=='n' and 'pending_action' not in jobs.jobs['t']


def test_metadata_cancel_after_account_removal_uses_original_adapter(db_engine,app_paths):
    events=[]
    class Adapter:
        platform_id='bilibili';verified_capabilities=set()
        def submit(self,*args,**kwargs):raise AssertionError('metadata cannot submit')
        def reconcile(self,*args,**kwargs):raise AssertionError('metadata cannot query works')
        def get_cached_platform_options(self,aid):return {'status':'empty'}
        def read_platform_options(self,aid):raise AssertionError('ticket required')
        def begin_platform_options(self,aid,ticket):return {'status':'verification_required','verification_available':True}
        def continue_platform_options(self,aid,ticket,action):
            events.append(action);assert action=='cancel';return {'status':'cancelled','cleanup_complete':True}
    api=DesktopApi(db_engine,app_paths,publish_adapters={'bilibili':Adapter()})
    accounts=AccountService(db_engine);account=accounts.create('bilibili','111','合成账号')
    try:
        jobs=api._multi_publish.platform_options;ticket=jobs.refresh(account.id)['ticket'];deadline=time.monotonic()+2
        while time.monotonic()<deadline and jobs.status(ticket)['status']=='loading':time.sleep(.01)
        assert jobs.status(ticket)['status']=='verification_required'
        accounts.remove(account.id)
        assert jobs.cancel(ticket)['accepted'];deadline=time.monotonic()+2
        while time.monotonic()<deadline and jobs.status(ticket)['status']!='cancelled':time.sleep(.01)
        assert jobs.status(ticket)['status']=='cancelled' and events==['cancel'] and not jobs.busy_accounts()
        assert '_adapter' not in jobs.status(ticket)
    finally:assert api.close(3)


def test_metadata_cancel_queued_before_read_never_opens_original_page(db_engine,app_paths,monkeypatch):
    queued=[];events=[]
    class Adapter:
        platform_id='bilibili';verified_capabilities=set()
        def submit(self,*args,**kwargs):raise AssertionError('metadata cannot submit')
        def reconcile(self,*args,**kwargs):raise AssertionError('metadata cannot query works')
        def get_cached_platform_options(self,aid):return {'status':'empty'}
        def read_platform_options(self,aid):raise AssertionError('ticket required')
        def begin_platform_options(self,aid,ticket):raise AssertionError('cancelled before opening')
        def continue_platform_options(self,aid,ticket,action):events.append(action);return {'status':'cancelled','cleanup_complete':True}
    api=DesktopApi(db_engine,app_paths,publish_adapters={'bilibili':Adapter()})
    account=AccountService(db_engine).create('bilibili','111','合成账号')
    monkeypatch.setattr(api._multi_publish,'enqueue',lambda *args:queued.append(args))
    try:
        jobs=api._multi_publish.platform_options;ticket=jobs.refresh(account.id)['ticket'];jobs.cancel(ticket)
        jobs.run(ticket);jobs.run_action(ticket,'cancel')
        assert jobs.status(ticket)['status']=='cancelled' and events==['cancel'] and not jobs.busy_accounts()
    finally:assert api.close(3)


def test_expired_metadata_handle_closes_and_cannot_resume(db_engine,app_paths):
    events=[]
    class Adapter:
        platform_id='bilibili';verified_capabilities=set()
        def submit(self,*args,**kwargs):raise AssertionError('metadata cannot submit')
        def reconcile(self,*args,**kwargs):raise AssertionError('metadata cannot query works')
        def get_cached_platform_options(self,aid):return {'status':'empty'}
        def read_platform_options(self,aid):raise AssertionError('ticket required')
        def begin_platform_options(self,aid,ticket):return {'status':'verification_required','verification_available':True}
        def continue_platform_options(self,aid,ticket,action):
            events.append(action);assert action=='cancel';return {'status':'cancelled','cleanup_complete':True}
    api=DesktopApi(db_engine,app_paths,publish_adapters={'bilibili':Adapter()})
    account=AccountService(db_engine).create('bilibili','111','合成账号')
    try:
        jobs=api._multi_publish.platform_options;ticket=jobs.refresh(account.id)['ticket'];deadline=time.monotonic()+2
        while time.monotonic()<deadline and jobs.status(ticket)['status']=='loading':time.sleep(.01)
        nonce=jobs.status(ticket)['verification_id']
        with jobs.lock:jobs.jobs[ticket]['expires']=0
        with pytest.raises(ValueError):jobs.respond(ticket,nonce,'continue')
        jobs.expire();deadline=time.monotonic()+2
        while time.monotonic()<deadline and jobs.status(ticket)['status']!='cancelled':time.sleep(.01)
        assert jobs.status(ticket)['status']=='cancelled' and events==['cancel'] and not jobs.busy_accounts()
    finally:assert api.close(3)


def test_metadata_verification_keeps_one_ticket_and_explicit_original_page_actions(db_engine,app_paths):
    events=[]
    class Adapter:
        platform_id='bilibili';verified_capabilities=set()
        def get_cached_platform_options(self,account_id):return {'status':'empty'}
        def read_platform_options(self,account_id):raise AssertionError('use ticket-bound reader')
        def begin_platform_options(self,account_id,ticket):
            events.append(('begin',account_id,ticket,threading.get_ident()))
            return {'status':'verification_required','verification_kind':'complex','verification_stage':'manager','verification_available':True}
        def continue_platform_options(self,account_id,ticket,action):
            events.append((action,account_id,ticket,threading.get_ident()))
            if action=='show':return {'status':'verification_required','verification_kind':'complex','verification_stage':'manager','verification_available':True}
            if action=='continue':return {'status':'ready','categories':[{'id':1,'name':'官方分区','mode':'legacy'}],'statements':[{'id':-1,'name':'无需声明'}]}
            return {'status':'cancelled','cleanup_complete':True}
        def submit(self,*a,**k):raise AssertionError('metadata must never submit')
        def reconcile(self,*a,**k):raise AssertionError('metadata must never reconcile')
    api=DesktopApi(db_engine,app_paths,publish_adapters={'bilibili':Adapter()})
    account=AccountService(db_engine).create('bilibili','111','合成账号')
    def settled(jobs,ticket):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            result=jobs.status(ticket)
            if result['status']!='loading':return result
            time.sleep(.01)
        raise AssertionError('metadata action did not finish')
    try:
        jobs=api._multi_publish.platform_options;first=jobs.refresh(account.id);ticket=first['ticket'];initial=settled(jobs,ticket)
        assert initial['status']=='verification_required' and initial['verification_available']
        nonce=initial['verification_id'];assert nonce
        assert jobs.refresh(account.id)['ticket']==ticket
        assert jobs.busy_accounts()=={account.id}
        with pytest.raises(ValueError):jobs.respond(ticket,'stale','continue')
        def respond(action,rid,**extra):
            return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':'multi_publish.platform_options.respond',
                'params':{'ticket':ticket,'verification_id':nonce,'action':action,**extra}}),trusted_origin=True)
        assert not respond('show','bad-metadata-schema',url='https://secret.invalid')['ok']
        assert not respond('submit','not-a-metadata-action')['ok']
        assert respond('show','metadata-show')['ok'];shown=settled(jobs,ticket);assert shown['verification_id']==nonce
        assert respond('continue','metadata-continue')['ok'];done=settled(jobs,ticket)
        assert done['status']=='ready' and not jobs.busy_accounts()
        assert [e[0] for e in events]==['begin','show','continue']
        assert len({e[3] for e in events})==1 and events[0][3]!=threading.get_ident()
        assert all(e[2]==ticket for e in events)
        with session_scope(db_engine) as db:
            assert list(db.scalars(select(TaskCenterRow)))==[]
        with pytest.raises(ValueError):jobs.respond(ticket,nonce,'continue')
    finally:assert api.close(3)
