"""Windows read/probe adapter. Fixed upload only, no draft/save/publish actions."""
import threading
import time
from .chromium_publish import account_lock,browser_options,chromium_profile,restore_account_session,evaluate_script,session_blob
from .douyin_auth import HOST,verified_identity
from .douyin_capability import READ_IDENTITY,READ_PAGE,ADVANCE_PROBE,ProbeObservation,SUCCESS_OBSERVER,UPLOAD_PATH
from .douyin_metadata import READ_METADATA,normalize_observation
from yijianjifa.features.accounts.capability_probe import fixture_path

class ChromiumProbe:
    def __init__(self,engine,paths):self.engine=engine;self.paths=paths;self.cancel=threading.Event()
    def close(self):self.cancel.set()
    def metadata(self,account,kind,query='',scope='local'):
        return self.run(account.id,self.cancel,kind,query,scope)
    def capability(self,account_id,cancel,path):
        if path!=fixture_path():raise ValueError('fixed probe required')
        return self.run(account_id,cancel,'capability')
    def run(self,account_id,cancel,kind,query='',scope='local'):
        from playwright.sync_api import sync_playwright
        from yijianjifa.features.accounts.service import AccountService
        from yijianjifa.features.accounts.session_store import SessionStore
        service=AccountService(self.engine);account=service.get(account_id)
        if account.platform!='douyin' or not account.session_ref:raise ValueError('account needs login')
        uid=account.platform_user_id;lock=account_lock(account.id)
        if not lock.acquire(False):raise ValueError('账号窗口正在使用，请稍后重试。')
        context=None
        try:
            store=SessionStore(self.paths.data_dir/'profiles');profile=chromium_profile(store,uid)
            with sync_playwright() as pw:
                context=pw.chromium.launch_persistent_context(str(profile),**browser_options('visible'))
                context.set_default_timeout(15000);restore_account_session(context,store,account,profile,uid)
                page=context.pages[0] if context.pages else context.new_page()
                page.add_init_script(SUCCESS_OBSERVER.replace('observe(document.documentElement','observe(document'))
                page.goto('https://'+HOST+UPLOAD_PATH,wait_until='domcontentloaded')
                deadline=time.monotonic()+180;uploaded=False;probe=ProbeObservation(uid);previous=None;matches=0
                while not cancel.is_set() and not self.cancel.is_set() and not page.is_closed() and time.monotonic()<deadline:
                    identity=evaluate_script(page,READ_IDENTITY+'return identity;',{})
                    verified_identity(identity,expected_uid=uid)
                    if not uploaded:
                        raw=evaluate_script(page,READ_PAGE,{})
                        probe.consume_upload(raw)
                        field=page.locator('input[type=file][accept*="video"]')
                        if field.count()!=1:raise ValueError('发布上传控件未就绪，请重试。')
                        field.set_input_files(str(fixture_path()));uploaded=True
                    elif kind=='capability':
                        raw=evaluate_script(page,READ_PAGE,{});probe.observe(raw)
                        evaluate_script(page,ADVANCE_PROBE,dict(expected_uid=uid,uploaded=True,ready=probe.ready))
                        if probe.ready and probe.scopes:
                            result=probe.result();break
                    else:
                        raw=evaluate_script(page,READ_METADATA,dict(expected_uid=uid,kind=kind,query=query,scope=scope,request_id='windows-read'))
                        result=normalize_observation(raw,uid,kind,query,scope)
                        if result:
                            matches=matches+1 if result==previous else 1;previous=result
                            if matches>=2:break
                    page.wait_for_timeout(1000)
                else:raise ValueError('后台读取超时或已取消，请确认账号登录和网络后重试。')
                verified_identity(evaluate_script(page,READ_IDENTITY+'return identity;',{}),expected_uid=uid)
                current=service.get(account.id)
                if current.session_ref==account.session_ref:service.attach_session(account.id,store,session_blob(context,uid))
                context.close();context=None
                return result
        finally:
            if context:
                try:context.close()
                except Exception:pass
            lock.release()
