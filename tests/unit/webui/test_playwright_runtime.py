"""No platform IO: one SDK per worker, independently owned contexts/locks."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright import sync_api
from yijianjifa.webui import playwright_runtime as runtime
from yijianjifa.webui.chromium_publish_session import LiveSession
from yijianjifa.webui.chromium_publish import account_lock
from yijianjifa.webui import chromium_multi_publish as douyin
from yijianjifa.webui import bilibili_multi_publish as bili
from yijianjifa.webui import channels_multi_publish as channels
from yijianjifa.webui import kuaishou_multi_publish as kuaishou


@pytest.fixture
def sdk(monkeypatch):
    events=[];contexts=[];drivers=[]
    class Context:
        def __init__(self):
            self.closed=False;self.fail=False
            self.pages=[SimpleNamespace(on=lambda *args:None,goto=lambda *args,**kw:None,
                                        add_init_script=lambda *args:None)]
        def set_default_timeout(self,*args):pass
        def close(self):
            events.append(('context-close',self))
            if self.fail:raise RuntimeError('synthetic context close failure')
            self.closed=True
    class Driver:
        def __init__(self):self.chromium=self;self.fail=False
        def start(self):events.append(('start',threading.get_ident()));drivers.append(self);return self
        def launch_persistent_context(self,*args,**kwargs):
            context=Context();contexts.append(context);return context
        def stop(self):
            events.append(('stop',threading.get_ident()))
            if self.fail:raise RuntimeError('synthetic driver stop failure')
            for context in contexts:context.closed=True
    monkeypatch.setattr(sync_api,'sync_playwright',Driver)
    yield SimpleNamespace(events=events,contexts=contexts,drivers=drivers)
    active=getattr(runtime._local,'runtime',None)
    if active:
        active.driver.fail=False
        for lease in list(active.leases):lease.stop()


def test_same_worker_leases_share_one_sdk_until_last_release(sdk):
    first=runtime.acquire_playwright();second=runtime.acquire_playwright()
    assert first.chromium is second.chromium and not first.exclusive
    first.stop()
    assert [event[0] for event in sdk.events]==['start'] and second.exclusive
    second.stop()
    assert [event[0] for event in sdk.events]==['start','stop']
    third=runtime.acquire_playwright();third.stop()
    assert [event[0] for event in sdk.events]==['start','stop','start','stop']


def test_failed_last_stop_keeps_owner_blocks_new_acquire_and_retries_fake(sdk):
    lease=runtime.acquire_playwright();sdk.drivers[0].fail=True
    with pytest.raises(RuntimeError):lease.stop()
    assert lease.exclusive
    with pytest.raises(ValueError,match='cleanup must be retried'):runtime.acquire_playwright()
    sdk.drivers[0].fail=False;lease.stop()
    new=runtime.acquire_playwright();new.stop()
    assert sum(event[0]=='start' for event in sdk.events)==2


def test_thread_isolation_and_cross_thread_release_rejected(sdk):
    first=runtime.acquire_playwright();result=[]
    def worker():
        with pytest.raises(ValueError,match='original host worker thread'):first.stop()
        other=runtime.acquire_playwright();result.append(other.chromium is first._runtime.driver)
        other.stop()
    thread=threading.Thread(target=worker);thread.start();thread.join()
    assert result==[False] and first.exclusive
    first.stop()
    assert len({event[1] for event in sdk.events if event[0]=='start'})==2


CLASSES=[douyin.ChromiumMultiPublishAdapter,kuaishou.KuaishouMultiPublishAdapter,
         bili.BilibiliMultiPublishAdapter,channels.ChannelsMultiPublishAdapter]


def prepare_adapter(cls,aid,db_engine,app_paths,monkeypatch,*,inspection=False):
    adapter=cls(db_engine,app_paths,inspection_only=inspection);adapter._thread()
    account=SimpleNamespace(id=aid,platform_user_id='111',session_ref='fixture-reference')
    monkeypatch.setattr(adapter,'_account',lambda s:account)
    monkeypatch.setattr(adapter,'_install_publish_guard',lambda s:None)
    monkeypatch.setattr(adapter,'_install_write_guard',lambda c:None)
    monkeypatch.setattr(adapter,'_persist_sessions',lambda:None)
    monkeypatch.setattr(douyin.legacy,'chromium_profile',lambda *args:Path('fixture-profile'))
    monkeypatch.setattr(douyin.legacy,'restore_account_session',lambda *args:None)
    monkeypatch.setattr(kuaishou.auth,'chromium_profile',lambda *args:Path('fixture-profile'))
    monkeypatch.setattr(kuaishou.auth,'restore_session',lambda *args:None)
    monkeypatch.setattr(bili.creator_auth,'chromium_profile',lambda *args:Path('fixture-profile'))
    monkeypatch.setattr(bili.creator_auth,'restore_session',lambda *args:None)
    s=LiveSession(dict(account_id=aid,platform_user_id='111'),dict(delivery_id=aid))
    adapter.sessions[aid]=s
    return adapter,s


@pytest.mark.parametrize('order',[CLASSES,list(reversed(CLASSES))])
def test_four_real_openers_share_sdk_keep_distinct_contexts_and_close_in_order(sdk,db_engine,app_paths,monkeypatch,order):
    opened=[]
    try:
        for index,cls in enumerate(order):
            adapter,s=prepare_adapter(cls,'fixture-'+str(index),db_engine,app_paths,monkeypatch)
            adapter._open(s);opened.append((adapter,s))
        assert len(sdk.contexts)==4 and sum(event[0]=='start' for event in sdk.events)==1
        assert len({id(s.context) for _,s in opened})==4
        for index,(adapter,s) in enumerate(opened):
            adapter.close()
            assert not s.locked and s.context is None
            assert all(not other.context.closed for _,other in opened[index+1:])
            assert sum(event[0]=='stop' for event in sdk.events)==int(index==3)
    finally:
        for adapter,_ in opened:adapter.close()


@pytest.mark.parametrize('inspection',[False,True])
def test_failed_context_retains_lock_lease_and_cancel_reference_until_retry(sdk,db_engine,app_paths,monkeypatch,inspection):
    owner,s=prepare_adapter(bili.BilibiliMultiPublishAdapter,'fixture-failed',db_engine,app_paths,monkeypatch,inspection=inspection)
    other,t=prepare_adapter(channels.ChannelsMultiPublishAdapter,'fixture-parked',db_engine,app_paths,monkeypatch)
    owner._open(s);other._open(t);context=s.context;context.fail=True
    with pytest.raises(RuntimeError):owner.cancel_delivery_session('fixture-failed')
    assert owner.sessions and s.context is context and s.locked
    assert not account_lock('fixture-failed').acquire(blocking=False)
    with pytest.raises(ValueError,match='browser close failed'):owner.close()
    assert owner.driver and s.context is context and s.locked and not t.context.closed
    assert not any(event[0]=='stop' for event in sdk.events)
    context.fail=False;owner.close()
    assert not owner.sessions and owner.driver is None and not s.locked and not t.context.closed
    other.close()
    assert sum(event[0]=='stop' for event in sdk.events)==1


def test_bili_option_failure_never_releases_another_platform_page(sdk,db_engine,app_paths,monkeypatch):
    owner,s=prepare_adapter(bili.BilibiliMultiPublishAdapter,'fixture-option',db_engine,app_paths,monkeypatch)
    owner.sessions.clear();s.options_only=True;owner.options_sessions['ticket']=s
    other,t=prepare_adapter(channels.ChannelsMultiPublishAdapter,'fixture-option-other',db_engine,app_paths,monkeypatch)
    owner._open(s);other._open(t);s.context.fail=True
    with pytest.raises(ValueError,match='configuration browser close failed'):owner.close()
    assert s.locked and owner.driver and owner.options_sessions and not t.context.closed
    other.close()
    with pytest.raises(ValueError,match='configuration browser close failed'):owner.close()
    assert not owner.options_sessions and owner.driver is None and not s.locked
    assert sum(event[0]=='stop' for event in sdk.events)==1


def test_real_sdk_double_start_reproduces_and_leases_fix_without_browser(tmp_path):
    code='''
import json
from playwright.sync_api import sync_playwright
from yijianjifa.webui.playwright_runtime import acquire_playwright
first=sync_playwright().start()
failed=False
try:
 try:sync_playwright().start()
 except Exception as e:failed='Sync API inside the asyncio loop' in str(e)
finally:first.stop()
a=acquire_playwright();b=acquire_playwright()
same=a.chromium is b.chromium
a.stop();second_alive=b.chromium is not None
b.stop()
c=acquire_playwright();c.stop()
print(json.dumps(dict(sync_api_inside_asyncio_loop=failed,shared_sdk=same,second_owner_survives=second_alive,new_cycle_after_last_release=True,browser_launched=False)))
'''
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True,
        env={**os.environ,'PYTHONPATH':os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)})
    assert json.loads(result.stdout)==dict(sync_api_inside_asyncio_loop=True,shared_sdk=True,
        second_owner_survives=True,new_cycle_after_last_release=True,browser_launched=False)


def test_real_sdk_failed_stop_cannot_be_proved_by_second_public_noop():
    # Isolated process: mutate only a freshly started SDK transport, no browser.
    code='''
import json
from yijianjifa.webui.playwright_runtime import acquire_playwright
lease=acquire_playwright();r=lease._runtime;m=r.manager
original=m._connection.stop_sync
m._connection.stop_sync=lambda:(_ for _ in ()).throw(RuntimeError('fixture'))
try:
 try:lease.stop()
 except RuntimeError:pass
 m._connection.stop_sync=original
 rejected=False
 try:lease.stop()
 except ValueError:rejected=True
 retained=not lease._released and not m._loop.is_closed()
 print(json.dumps(dict(retry_rejected=rejected,uncertain_owner_retained=retained)))
finally:
 # Fixture-only final cleanup of the deliberately interrupted SDK.
 m._exit_was_called=False
 r.driver.stop()
'''
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True,
        env={**os.environ,'PYTHONPATH':os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)})
    assert json.loads(result.stdout)==dict(retry_rejected=True,uncertain_owner_retained=True)


@pytest.mark.parametrize('close_fails',[False,True])
def test_ordinary_douyin_transport_coexists_with_platform_adapter_and_retries_flush(sdk,db_engine,app_paths,monkeypatch,close_fails):
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.webui.chromium_publish import make_chromium_transport
    account=AccountService(db_engine).create('douyin','111','fixture')
    cancelled=threading.Event();cancelled.set()
    transport=make_chromium_transport(db_engine,app_paths)
    adapter,s=prepare_adapter(channels.ChannelsMultiPublishAdapter,'fixture-normal-other',db_engine,app_paths,monkeypatch)
    # A cancelled transport opens its normal session then skips all publish DOM
    # steps, so this exercises SDK ownership without uploads or a final marker.
    transport({'id':account.id,'platform_user_id':'111'},{},
              {'expected_uid':'111','title_text':'fixture'},None,
              before_final=lambda:pytest.fail('no final allowed'),cancel=cancelled)
    context=sdk.contexts[0]
    adapter._open(s)
    assert len(sdk.contexts)==2 and sum(event[0]=='start' for event in sdk.events)==1
    context.fail=close_fails
    if close_fails:
        with pytest.raises(ValueError,match='browser close failed'):transport.flush_sessions()
        assert not context.closed and not s.context.closed
        assert not account_lock(account.id).acquire(blocking=False)
        context.fail=False
    transport.flush_sessions()
    assert context.closed and not s.context.closed
    assert sum(event[0]=='stop' for event in sdk.events)==0
    assert account_lock(account.id).acquire(blocking=False);account_lock(account.id).release()
    adapter.close()
    assert sum(event[0]=='stop' for event in sdk.events)==1


def test_worker_shutdown_retries_terminal_clone_after_other_owner_releases(sdk,db_engine,app_paths,monkeypatch):
    from yijianjifa.app_services.multi_publish_inspection import InspectionJobs
    from yijianjifa.app_services.multi_publish_runtime import MultiPublishRuntime
    clone,s=prepare_adapter(bili.BilibiliMultiPublishAdapter,'fixture-shutdown-clone',db_engine,app_paths,monkeypatch,inspection=True)
    other,t=prepare_adapter(channels.ChannelsMultiPublishAdapter,'fixture-shutdown-other',db_engine,app_paths,monkeypatch)
    clone._open(s);other._open(t);context=s.context;context.fail=True
    closed=threading.Event();closed.set()
    host=SimpleNamespace(closed=closed,platform_options=SimpleNamespace(close=lambda:None),
                         catalog=SimpleNamespace(_transports={'channels'},transport_for=lambda p:other))
    jobs=InspectionJobs(host);host.inspections=jobs
    jobs.jobs['fixture']=dict(state='inspection_complete',adapter=clone,context={'handle':s.handle},
        media=None,payload=s.payload,error_code=None,cleanup_failed=True)
    MultiPublishRuntime._run(host)
    assert context.closed and not s.locked and not t.locked
    assert clone.driver is None and other.driver is None
    assert jobs.jobs['fixture']['adapter'] is None and not jobs.jobs['fixture']['cleanup_failed']
    assert not jobs.busy_accounts() and sum(event[0]=='stop' for event in sdk.events)==1


@pytest.mark.parametrize('cls',CLASSES)
def test_all_platform_openers_report_transient_busy_without_opening_profile(sdk,db_engine,app_paths,monkeypatch,cls):
    from yijianjifa.features.publish.account_wait import AccountBusy
    adapter,s=prepare_adapter(cls,'busy-platform-owner',db_engine,app_paths,monkeypatch)
    lock=account_lock(s.payload['account_id']);lock.acquire()
    try:
        assert adapter.account_available(s.payload) is False
        with pytest.raises(AccountBusy):adapter._open(s)
        assert s.context is None and not s.locked and not sdk.contexts
        other,t=prepare_adapter(cls,'free-platform-owner',db_engine,app_paths,monkeypatch)
        assert other.account_available(t.payload) is True
        other._open(t);other.close()
    finally:lock.release()
    assert adapter.account_available(s.payload) is True
    adapter._open(s);adapter.close()
    assert not lock.locked()


def test_ordinary_queue_closes_pages_but_keeps_lease_until_final_save(sdk,db_engine,app_paths,monkeypatch):
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.webui import chromium_publish as module
    account=AccountService(db_engine).create('douyin','111','queue-owner')
    transport=module.make_chromium_transport(db_engine,app_paths)
    cancel=threading.Event();cancel.set()
    def submit():transport({'id':account.id,'platform_user_id':'111'},{},{'expected_uid':'111','title_text':'fixture'},None,before_final=lambda:pytest.fail('no final'),cancel=cancel)
    submit();transport.release_sessions()
    assert sdk.contexts[0].closed and account_lock(account.id).locked()
    submit();transport.release_sessions()
    assert len(sdk.contexts)==2 and all(c.closed for c in sdk.contexts)
    transport.flush_sessions()
    assert not account_lock(account.id).locked()


@pytest.mark.parametrize('state',['published','paused_auth','result_unknown'])
def test_only_terminal_cleanup_is_retried_without_replaying_submission(sdk,db_engine,app_paths,monkeypatch,state):
    from yijianjifa.features.publish.coordinator import AdapterOutcome
    adapter,s=prepare_adapter(douyin.ChromiumMultiPublishAdapter,'cleanup-retry-'+state,db_engine,app_paths,monkeypatch)
    adapter._open(s);context=s.context;context.fail=True;calls=[]
    try:
        monkeypatch.setattr(adapter,'_advance',lambda *a,**k:(calls.append('submit'),AdapterOutcome(state))[1])
        assert adapter._step(s).state==state
        assert account_lock(s.payload['account_id']).locked()
        context.fail=False
        s.cleanup_retry_at=0
        assert adapter.account_available(s.payload) is (state=='published')
        assert context.closed is (state=='published') and calls==['submit']
    finally:context.fail=False;adapter.close()
