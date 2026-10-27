"""Ordinary publishing's legacy worker must use a real pre-upload login page."""
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.storage.db import session_scope
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.webui import chromium_publish as module
from yijianjifa.webui.douyin_publish_form import validate_form_request


@pytest.mark.parametrize('choice', ['continue', 'close', 'wrong-account', 'retry-launch', 'retry-close', 'retry-navigation-closed'])
def test_ordinary_login_before_upload_uses_visible_browser_and_explicit_original_account(
        db_engine, app_paths, monkeypatch, choice):
    account = AccountService(db_engine).create('douyin', '10203', '合成抖音')
    with session_scope(db_engine) as db:
        db.get(AccountRow, account.id).session_ref = 'synthetic-ordinary-archive'
    when = (datetime.now(timezone.utc)+timedelta(hours=4)).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    form = validate_form_request(expected_uid='10203', title='合成测试', body='合成简介', declaration='', scheduled_time_text=when)
    path = app_paths.data_dir/'synthetic.mp4'; path.write_bytes(b'synthetic-video')
    events = []; pages = []; state = {'logged': False, 'submitted': False, 'waits': 0, 'failed_launch':False, 'failed_close':False, 'failed_navigation':False, 'failure_waits':0}
    cancel = threading.Event()
    identity = {'uid':'10203','uid_after':'10203','status':0,'after_status':0,'profile_status':0,'nickname':'合成抖音'}

    class Page:
        url = ''; closed = False
        def goto(self, url, **kw):
            if self.closed: raise RuntimeError('synthetic page was closed')
            if choice=='retry-navigation-closed' and getattr(self,'native',False) and not state['failed_navigation']:
                state['failed_navigation']=True;self.closed=True
                raise RuntimeError('synthetic page closed during navigation')
            self.url = url
        def add_init_script(self, value): pass
        def bring_to_front(self): events.append('front')
        def is_closed(self): return self.closed
        def screenshot(self, **kw):
            pytest.fail('an unused native login page must not be mirrored as a screenshot')
        def wait_for_timeout(self, ms):
            request = module.broker.status()['request']
            if not request: return
            state['waits'] += 1
            if state['waits'] > 5: cancel.set(); return
            if request.get('browser_login') is not True:
                cancel.set(); return
            assert not any(event in {'upload','journal','final'} for event in events)
            if choice == 'close': self.closed = True; return
            state['logged'] = True
            if choice == 'wrong-account': identity.update(uid='99999',uid_after='99999')
            # Opening/focusing after the human logs in must still not continue.
            module.broker.respond(request['id'], 'visible' if state['waits']==1 else 'done')
        def locator(self, selector):
            return SimpleNamespace(count=lambda:1, set_input_files=lambda value: events.append('upload'))
        def evaluate(self, code, args=None):
            if code == module.CHALLENGE: return {} if state['logged'] else {'login':True}
            if 'window.p011FinalClicked' in code:
                state['submitted']=True; events.append('final'); return {'attempted':True}
            if 'return identity;' in code: return dict(identity)
            if 'p011ResultNeedsSearch' in code:
                rows = [{'time':datetime.strptime(when,'%Y-%m-%d %H:%M').strftime('%Y年%m月%d日 %H:%M'),'review':'审核中'}] if state['submitted'] else []
                return dict(identity=identity,path='/creator-micro/content/manage',ready=True,query='合成测试',matches=rows)
            return dict(identity=identity,path='/creator-micro/content/post/video',ready=True,title='合成测试',body='合成简介',declaration='',title_count=1,editor_count=1,mode_controls=1,final_button_count=1,upload_complete=True,final_button_enabled=True,scheduled_time_text=when)

    class Context:
        def __init__(self): self.closed=False; self.pages=[Page()]; pages.extend(self.pages)
        def set_default_timeout(self, value): pass
        def close(self):
            if self.closed and choice in {'retry-launch','retry-close','retry-navigation-closed'}:
                raise RuntimeError('closed cache must not be reused')
            if choice=='retry-close' and not state['failed_close']:
                state['failed_close']=True; events.append('close_failed')
                raise RuntimeError('synthetic close blocked')
            self.closed=True; self.pages[0].closed=True; events.append('close')

    def launch(profile, **options):
        events.append(('launch',options['headless']))
        if choice=='retry-launch' and not options['headless'] and not state['failed_launch']:
            state['failed_launch']=True; raise RuntimeError('synthetic launch failure')
        context=Context(); context.pages[0].native=not options['headless']; return context
    driver=SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch),stop=lambda:None)
    monkeypatch.setattr(module, 'session_blob', lambda *args:b'synthetic-session')
    monkeypatch.setattr(module, 'restore_account_session', lambda *args:None)
    monkeypatch.setattr(module, 'persist_refreshed_sessions', lambda *args:None)
    from yijianjifa.webui import playwright_runtime
    monkeypatch.setattr(playwright_runtime, 'acquire_playwright', lambda:driver)
    if choice in {'retry-launch','retry-close','retry-navigation-closed'}:
        def wait_for_retry(timeout):
            state['failure_waits']+=1
            if state['failure_waits']>3: cancel.set(); return True
            request=module.broker.status()['request']
            assert request['view_state']=='failed' and not any(event in {'upload','journal','final'} for event in events)
            module.broker.respond(request['id'],'visible')
            return False
        monkeypatch.setattr(cancel,'wait',wait_for_retry)
    transport=module.make_chromium_transport(db_engine,app_paths)
    try:
        outcome=transport({'id':account.id,'platform_user_id':'10203'},
            {'path':str(path),'content_hash':sha256_file(path)},form,'future',
            before_final=lambda:events.append('journal'),cancel=cancel)
    finally:
        transport.flush_sessions()
        pending=module.broker.status()['request']
        if pending:module.broker.finish(pending['id'])
    assert [event[1] for event in events if isinstance(event,tuple)] == ([True,False,False] if choice in {'retry-launch','retry-navigation-closed'} else [True,False])
    assert len(pages)==(3 if choice=='retry-navigation-closed' else 2) and state['waits']>=1
    if choice in {'continue','retry-launch','retry-close','retry-navigation-closed'}:
        assert state['waits']>=2 and outcome.platform_schedule_confirmation=='future'
        assert [event for event in events if event in ('upload','journal','final')] == ['upload','journal','final']
    else:
        assert not outcome.final_action_triggered and not any(event in {'upload','journal','final'} for event in events if isinstance(event,str))
    assert not module.account_lock(account.id).locked()
    current = AccountService(db_engine).get(account.id)
    health = AccountService(db_engine).login_checks([current])[current.id]
    assert health is not None
    assert health['state'] == ('login_required' if choice == 'close' else
                               'identity_mismatch' if choice == 'wrong-account' else 'valid')


def test_coarse_windows_clock_does_not_keep_expired_badge_after_success(db_engine, app_paths, monkeypatch):
    # 强制登录前后处于同一系统时钟刻度，复现 Windows 低精度时钟。
    from yijianjifa.webui import login_observation as observation
    wall = [datetime.now(timezone.utc)]
    waits = []
    class CoarseClock:
        @staticmethod
        def now(tz):
            return wall[0]
    def next_tick(delay):
        waits.append(delay)
        wall[0] += timedelta(milliseconds=16)
    monkeypatch.setattr(observation, 'datetime', CoarseClock)
    monkeypatch.setattr(observation, '_last_observation_start', None, raising=False)
    monkeypatch.setattr(observation, 'sleep', next_tick, raising=False)
    test_ordinary_login_before_upload_uses_visible_browser_and_explicit_original_account(
        db_engine, app_paths, monkeypatch, 'continue')
    assert waits
