from yijianjifa.webui.chromium_publish import evaluate_script, challenge_kind, browser_options

def test_packaged_scripts_receive_arguments_not_interpolated_text():
    page=type('Page',(),{'evaluate':lambda self,code,args:(code,args)})()
    code,args=evaluate_script(page,'return title_text;',{'title_text':'`danger`'})
    assert '`danger`' not in code and args['title_text']=='`danger`'

def test_modes_default_to_true_headless():
    assert browser_options('background')['headless'] is True
    assert browser_options('visible')['headless'] is False

def test_challenge_distinguishes_sms_and_complex_and_login():
    assert challenge_kind({'sms':True})=='sms'
    assert challenge_kind({'complex':True})=='complex'
    assert challenge_kind({'login':True})=='login'
    assert challenge_kind({}) is None

import sys
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
import pytest

@pytest.mark.parametrize('busy',[False,True])
@pytest.mark.parametrize('uid',['123','456'])
@pytest.mark.parametrize('restricted',[False,True])
@pytest.mark.parametrize('existing,expected_final,reauth',[(False,1,False),(True,0,False),(False,1,True),(False,1,'before')])
def test_transport_binds_file_and_clicks_at_most_once(db_engine,app_paths,monkeypatch,existing,expected_final,reauth,uid,restricted,busy):
    from yijianjifa.webui import chromium_publish as module
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.webui.douyin_publish_form import validate_form_request
    from yijianjifa.features.library.media_identity import sha256_file
    account=AccountService(db_engine).create('douyin',uid,'测试账号')
    when=(datetime.now(timezone.utc)+timedelta(hours=4)).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    form=validate_form_request(expected_uid=uid,title='测试',body='简介',declaration='',scheduled_time_text=when)
    identity={'uid':uid,'uid_after':uid,'status':0,'profile_status':0,'after_status':0,'nickname':'测试账号'}
    path=app_paths.data_dir/'fixture.mp4';path.write_bytes(b'test file')
    events=[]
    class Input:
        def count(self):return 1
        def set_input_files(self,value):events.append(('upload',value));page.url='https://creator.douyin.com/creator-micro/content/post/video'
    class Page:
        url=''
        result=False
        restored=False
        def goto(self,url,**kw):
            self.url=url
            if url.endswith('/home'):self.restored=True
        def add_init_script(self,s):pass
        def locator(self,s):return Input()
        def wait_for_timeout(self,n):
            request=module.broker.status()['request']
            if reauth and request and not self.restored:
                if request.get('view_mode')=='page':
                    self.restored=True;events.append(('user_verified',None))
                else:
                    assert request['external_browser'] is False
                    module.broker.respond(request['id'],'visible')
        def screenshot(self,**kw):return b'fixture screenshot'
        def evaluate(self,code,args=None):
            if code==module.CHALLENGE:return ({'complex':True} if reauth=='before' else {'login':True}) if reauth and (any(e[0]=='upload' for e in events) if reauth=='before' else self.result) and not self.restored else {}
            if 'window.p011FinalClicked' in code:
                self.result=True;self.url='https://creator.douyin.com/creator-micro/content/manage';events.append(('click',None));return {'attempted':True}
            if 'return identity;' in code:return identity
            if 'p011ResultNeedsSearch' in code:
                rows=[{'time':datetime.strptime(when,'%Y-%m-%d %H:%M').strftime('%Y年%m月%d日 %H:%M'),'review':'审核中'}] if self.result or existing else []
                return dict(identity=identity,path='/creator-micro/content/manage',ready=True,query='测试',matches=rows)
            if reauth=='before' and not self.restored:return dict(identity=identity,ready=False)
            return dict(identity=identity,path='/creator-micro/content/post/video',ready=True,title='测试',body='简介',declaration='',title_count=1,editor_count=1,mode_controls=1,final_button_count=1,upload_complete=True,final_button_enabled=True,scheduled_time_text=when)
    page=Page()
    class Context:
        pages=[page]
        def set_default_timeout(self,n):pass
        def close(self):events.append(('close',None))
    def launch(profile,**options):
        events.append(('launch',options['headless']));return Context()
    class Driver:
        def start(self):return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch),stop=lambda:None)
        def __enter__(self):return self.start()
        def __exit__(self,*a):pass
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=lambda:Driver()))
    monkeypatch.setattr(module,'session_blob',lambda *a:b'fake private session')
    transport=module.make_chromium_transport(db_engine,app_paths,uid if restricted else None)
    import threading
    waited=threading.Event();releaser=None
    if busy:
        lock=module.account_lock(account.id);lock.acquire()
        monkeypatch.setattr(module.progress,'update',lambda *a,**k:waited.set() if '等待账号' in k.get('stage','') else None)
        def release():
            waited.wait(3)
            lock.release()
        releaser=threading.Thread(target=release);releaser.start()
    outcome=transport({'id':account.id,'platform_user_id':uid},{'path':str(path),'content_hash':sha256_file(path)},form,'future',before_final=lambda:events.append(('journal',None)),cancel=threading.Event())
    monkeypatch.setattr(module,'persist_refreshed_sessions',lambda *a:None)
    transport.flush_sessions()
    if releaser:
        releaser.join(3);assert waited.is_set() and not lock.locked()
    assert sum(e[0]=='click' for e in events)==expected_final
    if expected_final:
        assert outcome.platform_schedule_confirmation=='future'
        assert [e[0] for e in events if e[0] in {'upload','journal','click'}]==['upload','journal','click']
        if reauth:
            assert sum(e[0]=='launch' for e in events)==1
            assert ('user_verified',None) in events
    else:assert outcome.error_code=='submission_existing_work' and not outcome.final_action_triggered

def test_restore_shared_session_does_not_overwrite_same_archive(tmp_path,monkeypatch):
    from yijianjifa.webui import chromium_publish as module
    calls=[]
    store=SimpleNamespace(get=lambda *a,**k:calls.append('read') or b'archive')
    context=SimpleNamespace(add_cookies=lambda rows:calls.append('import'),cookies=lambda:[dict(name='session',domain='.douyin.com',path='/',value='rotated')])
    account=SimpleNamespace(id='account',session_ref='ref')
    monkeypatch.setattr(module,'native_to_chromium',lambda *a:[dict(name='session',domain='.douyin.com',path='/',value='old')])
    module.restore_account_session(context,store,account,tmp_path,'123')
    module.restore_account_session(context,store,account,tmp_path,'123')
    assert calls==['read','import','read']

def test_login_expiry_requires_both_account_identity_reads():
    from yijianjifa.webui.chromium_publish import identity_session_expired
    assert not identity_session_expired({'status':0,'profile_status':8,'after_status':0})
    assert not identity_session_expired({'status':8,'after_status':0})
    assert identity_session_expired({'status':8,'after_status':8})


def test_restore_missing_session_cookie_after_browser_restart(tmp_path,monkeypatch):
    from yijianjifa.webui import chromium_publish as module
    rows=[dict(name='session',domain='.douyin.com',path='/',value='private')]
    calls=[]
    (tmp_path/'.source-ref').write_text('ref')
    monkeypatch.setattr(module,'native_to_chromium',lambda *a:rows)
    module.restore_account_session(SimpleNamespace(cookies=lambda:[],add_cookies=lambda value:calls.append(value)),SimpleNamespace(get=lambda *a,**k:b'archive'),SimpleNamespace(id='account',session_ref='ref'),tmp_path,'123')
    assert calls==[rows]


def test_selected_account_mismatch_rejected_before_browser(db_engine,app_paths):
    import threading
    from yijianjifa.webui.chromium_publish import make_chromium_transport
    from yijianjifa.features.accounts.service import AccountService
    account=AccountService(db_engine).create('douyin','456','另一个账号')
    transport=make_chromium_transport(db_engine,app_paths)
    with pytest.raises(ValueError,match='account mismatch'):
        transport({'id':account.id,'platform_user_id':'123'},{},{'expected_uid':'123'},'future',before_final=lambda:None,cancel=threading.Event())

@pytest.mark.parametrize('cancel_wait',[False,True])
def test_busy_account_waits_without_opening_browser_and_can_cancel(db_engine,app_paths,monkeypatch,cancel_wait):
    import threading
    from yijianjifa.webui import chromium_publish as module,playwright_runtime
    from yijianjifa.features.accounts.service import AccountService
    account=AccountService(db_engine).create('douyin','876','等待测试')
    held=module.account_lock(account.id);held.acquire()
    waiting=threading.Event();opened=threading.Event();cancel=threading.Event();outcomes=[]
    monkeypatch.setattr(module.progress,'update',lambda *a,**k:waiting.set() if '等待账号' in k.get('stage','') else None)
    def open_driver():opened.set();raise RuntimeError('offline launch sentinel')
    monkeypatch.setattr(playwright_runtime,'acquire_playwright',open_driver)
    transport=module.make_chromium_transport(db_engine,app_paths)
    def run():
        try:outcomes.append(transport({'id':account.id,'platform_user_id':'876'},{},{'expected_uid':'876','title_text':'fixture'},None,before_final=lambda:pytest.fail('no final'),cancel=cancel))
        finally:transport.flush_sessions()
    worker=threading.Thread(target=run);worker.start()
    try:
        assert waiting.wait(2),'busy task must wait rather than fail'
        assert worker.is_alive() and not opened.is_set()
        if cancel_wait:cancel.set()
        else:held.release()
        worker.join(3);assert not worker.is_alive()
        assert opened.is_set() is (not cancel_wait)
        assert outcomes[0].error_code!='submission_account_busy'
        if cancel_wait:assert held.locked()
        else:assert not held.locked()
    finally:
        cancel.set()
        if held.locked():held.release()
        worker.join(3)
