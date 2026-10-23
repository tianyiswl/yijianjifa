"""Mac read-only bridge for an immutable bundled HTML document.

Requires pywebview 6.2.1 Cocoa and WKScriptMessageHandlerWithReply. Install in
before_show before the initial load. Do not expose this object via js_api.
This adapter is not a general navigation host and exposes no writes or paths.
"""
from functools import lru_cache

from yijianjifa.app_services.library_queries import NativeReadGate


@lru_cache(maxsize=1)
def _native_types():
    import json
    import Foundation
    import objc
    import WebKit
    from webview.platforms.cocoa import BrowserView

    # Objective-C class names are process-global; define these only once.
    class P011ReadHandler(Foundation.NSObject, protocols=[objc.protocolNamed('WKScriptMessageHandlerWithReply')]):
        def userContentController_didReceiveScriptMessage_replyHandler_(self, controller, message, reply):
            frame = message.frameInfo()
            body = message.body()
            # PyObjC strings are subclasses; do not coerce arrays/objects to strings.
            raw = str(body) if isinstance(body, str) else None
            view, main, url = message.webView(), bool(frame.isMainFrame()), str(frame.request().URL().absoluteString())
            def execute():
                return self.gate.receive(raw, view=view, main_frame=main, url=url)
            worker = getattr(self, 'worker', None)
            if worker is None:
                reply(json.dumps(execute(), ensure_ascii=False), None)
            else:
                from PyObjCTools import AppHelper
                def deliver(result):
                    AppHelper.callAfter(reply, json.dumps(result, ensure_ascii=False), None)
                if not worker.submit(execute, deliver):
                    deliver({'v':1,'request_id':None,'ok':False,'error':{'code':'busy','message':'操作正在处理中，请稍后重试。','retryable':True}})

    class P011ReadDelegate(BrowserView.BrowserDelegate):
        def webView_decidePolicyForNavigationAction_decisionHandler_(self, view, action, reply):
            url = str(action.request().URL().absoluteString())
            frame = action.targetFrame()
            main = frame is None or bool(frame.isMainFrame())
            allowed = url == 'about:blank' and (not main or not self.started)
            if main and allowed:
                self.started = True
            reply(WebKit.WKNavigationActionPolicyAllow if allowed else WebKit.WKNavigationActionPolicyCancel)

    return P011ReadHandler, P011ReadDelegate


class CocoaReadBridge:
    def __init__(self, service):
        self._gate = NativeReadGate(service)
        self._handler = None
        self._delegate = None

    def attach(self, window):
        import WebKit
        from webview.platforms.cocoa import BrowserView

        P011ReadHandler, P011ReadDelegate = _native_types()
        native = BrowserView.instances[window.uid]
        self._gate.bind(native.webview)
        self._handler = P011ReadHandler.alloc().init()
        self._handler.gate = self._gate
        native.webview.configuration().userContentController().addScriptMessageHandlerWithReply_contentWorld_name_(
            self._handler, WebKit.WKContentWorld.pageWorld(), 'p011Read')
        self._delegate = P011ReadDelegate.alloc().init()
        self._delegate.started = False
        native._browserDelegate = self._delegate
        native.webview.setNavigationDelegate_(self._delegate)

    def close(self):
        """Revoke before destroying the window. This binding cannot be reused."""
        self._gate.close()


class CocoaAppBridge(CocoaReadBridge):
    """Privileged bundled application code runs in a private WKContentWorld.

    Page-world scripts, including same-origin child frames, receive no app
    handler or Python API. Do not create a DOM/postMessage command proxy back
    into this world. application_script must be packaged code selected by the
    native host, never user content or a path supplied by the page.
    """
    def __init__(self, service, application_script):
        super().__init__(service)
        self._application_script = application_script
        self._world = None
        from yijianjifa.app_services.dispatch_worker import DispatchWorker
        self._worker = DispatchWorker()

    def attach(self, window):
        import WebKit
        from webview.platforms.cocoa import BrowserView

        handler_type, delegate_type = _native_types()
        native = BrowserView.instances[window.uid]
        self._gate.bind(native.webview)
        self._world = WebKit.WKContentWorld.worldWithName_('P011Application')
        self._handler = handler_type.alloc().init()
        self._handler.worker = self._worker
        self._handler.gate = self._gate
        controller = native.webview.configuration().userContentController()
        controller.addScriptMessageHandlerWithReply_contentWorld_name_(self._handler, self._world, 'p011App')
        script = WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_inContentWorld_(
            self._application_script, WebKit.WKUserScriptInjectionTimeAtDocumentEnd, True, self._world)
        controller.addUserScript_(script)
        self._delegate = delegate_type.alloc().init()
        self._delegate.started = False
        native._browserDelegate = self._delegate
        native.webview.setNavigationDelegate_(self._delegate)

    def close(self, *, wait=False):
        # Stop admission before waiting for the gate held by an active request.
        # The Mac exit coordinator uses wait=True off the Cocoa main thread.
        self._worker.close()
        super().close()
        if wait:self._worker.close(wait=True)
