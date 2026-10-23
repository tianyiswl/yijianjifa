"""Isolated native auth adapter; not configured in the production Mac host yet.

No platform address/identity script is built in. The reviewed native observer
must verify identity and collect session bytes; page-supplied data is not proof.
"""
from functools import lru_cache
import threading
from .auth_isolation import AuthNavigation, make_auth_configuration


class CocoaAuthProvider:
    def __init__(self,url,hosts,observer,*,schedule=None,window_factory=None):
        self.policy=AuthNavigation(hosts)
        if not self.policy.allows(url):raise ValueError('invalid authentication URL')
        if not callable(observer):raise ValueError('identity observer required')
        self.url=url;self.observer=observer;self.schedule=schedule;self.window_factory=window_factory

    def __call__(self,account_id,cancel):
        if threading.current_thread() is threading.main_thread():raise RuntimeError('authentication must run off the UI thread')
        schedule=self.schedule
        if schedule is None:
            from PyObjCTools import AppHelper
            schedule=AppHelper.callAfter
        done=threading.Event();state={'window':None,'value':None,'error':None}
        def closed(value,error):
            state.update(window=None,value=value,error=error);done.set()
        def start():
            if cancel.is_set():closed(None,None);return
            try:
                factory=self.window_factory or CocoaAuthWindow
                state['window']=factory(self.url,self.policy,self.observer,closed)
                state['window'].show()
            except Exception:
                if state['window'] is not None:state['window'].fail()
                else:closed(None,'authentication_window_failed')
        schedule(start);cancel_sent=False
        while not done.wait(.1):
            if cancel.is_set() and not cancel_sent:
                cancel_sent=True
                def stop():
                    if state['window'] is not None:state['window'].cancel()
                schedule(stop)
        if cancel.is_set():return None
        if state['error']:raise RuntimeError('authentication window failed')
        return state['value']


@lru_cache(maxsize=1)
def auth_delegate_type():
    import objc
    import Foundation
    import WebKit
    import AppKit
    # Use PyObjC's informal delegate metadata, as the Cocoa host does.
    # Explicit formal protocols lose completion-block metadata on this runtime;
    # the real offline WebKit acceptance covers both allow and deny callbacks.
    class P011AuthDelegate(Foundation.NSObject):
        def webView_decidePolicyForNavigationAction_decisionHandler_(self,view,action,reply):
            try:allowed=self.owner.policy.allows(str(action.request().URL().absoluteString()),new_window=action.targetFrame() is None)
            except Exception:allowed=False
            reply(WebKit.WKNavigationActionPolicyAllow if allowed else WebKit.WKNavigationActionPolicyCancel)
        def webView_decidePolicyForNavigationResponse_decisionHandler_(self,view,response,reply):
            try:allowed=bool(response.canShowMIMEType()) and self.owner.policy.allows(str(response.response().URL().absoluteString()))
            except Exception:allowed=False
            reply(WebKit.WKNavigationResponsePolicyAllow if allowed else WebKit.WKNavigationResponsePolicyCancel)
        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self,view,config,action,features):return None
        def webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(self,view,parameters,frame,reply):
            handler=getattr(self.owner,"open_panel",None)
            if handler:handler(parameters,frame,reply)
            else:reply(None)
        def webView_didStartProvisionalNavigation_(self,view,navigation):self.owner.generation+=1
        def webView_didFinishNavigation_(self,view,navigation):self.owner.observe()
        def webView_didFailProvisionalNavigation_withError_(self,view,navigation,error):
            if error.code()!=-999:self.owner.fail()
        def webView_didFailNavigation_withError_(self,view,navigation,error):
            if error.code()!=-999:self.owner.fail()
        def webViewWebContentProcessDidTerminate_(self,view):self.owner.fail()
        def windowShouldClose_(self,sender):
            handler=getattr(self.owner,"request_close",None)
            return handler() if handler else True
        def windowWillClose_(self,notification):self.owner.closed()
    return P011AuthDelegate


class CocoaAuthWindow:
    def __init__(self,url,policy,observer,on_closed):
        from Foundation import NSThread
        if not NSThread.isMainThread():raise RuntimeError('native auth window requires main thread')
        self.url=url;self.policy=policy;self.observer=observer;self.on_closed=on_closed
        self.window=None;self.view=None;self.delegate=None;self.generation=0;self.finished=False
        self.value=None;self.error=None

    def show(self):
        import AppKit
        import WebKit
        from Foundation import NSURL,NSURLRequest,NSMakeRect
        frame=NSMakeRect(0,0,960,720)
        style=AppKit.NSWindowStyleMaskTitled|AppKit.NSWindowStyleMaskClosable|AppKit.NSWindowStyleMaskResizable
        self.window=AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(frame,style,AppKit.NSBackingStoreBuffered,False)
        self.window.setReleasedWhenClosed_(False);self.window.setTitle_('一剪即发 · 账号登录')
        config=make_auth_configuration();self.configure(config)
        self.view=WebKit.WKWebView.alloc().initWithFrame_configuration_(frame,config)
        self.delegate=auth_delegate_type().alloc().init();self.delegate.owner=self
        self.view.setNavigationDelegate_(self.delegate);self.view.setUIDelegate_(self.delegate)
        self.window.setDelegate_(self.delegate);self.window.setContentView_(self.view)
        self.window.center();self.window.makeKeyAndOrderFront_(None)
        self.load_initial()

    def configure(self,config):pass

    def open_panel(self,parameters,frame,reply):reply(None)

    def load_initial(self):
        from Foundation import NSURL,NSURLRequest
        self.view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))

    def observe(self):
        if self.finished:return
        current=self.view.URL()
        if current is None or not self.policy.allows(str(current.absoluteString())):return
        generation=self.generation
        from PyObjCTools import AppHelper
        def observed(value):
            def apply():
                if not self.finished and generation==self.generation and value is not None:self.finish(value,None)
            AppHelper.callAfter(apply)
        try:self.observer(self.view,observed)
        except Exception:self.fail()

    def finish(self,value,error):
        if self.finished:return
        self.value=value;self.error=error
        if self.window is not None:self.window.close()
        self.closed()

    def cancel(self):self.finish(None,None)
    def fail(self):self.finish(None,'authentication_window_failed')

    def closed(self):
        if self.finished:return
        self.finished=True;self.generation+=1
        if self.view is not None:
            self.view.stopLoading();self.view.setNavigationDelegate_(None);self.view.setUIDelegate_(None)
        if self.window is not None:self.window.setDelegate_(None)
        if self.delegate is not None:self.delegate.owner=None
        self.view=None;self.window=None;self.delegate=None
        self.on_closed(self.value,self.error)
