"""WK resource transport, installed before pywebview creates its native views."""
from functools import lru_cache
from urllib.parse import urlsplit
from .media_registry import ResourceDenied, byte_range


@lru_cache(maxsize=1)
def _types():
    import objc
    import Foundation
    import WebKit
    from PyObjCTools import AppHelper

    class P011MediaHandler(Foundation.NSObject, protocols=[objc.protocolNamed('WKURLSchemeHandler')]):
        def webView_startURLSchemeTask_(self,view,task):
            request=task.request();url=urlsplit(str(request.URL().absoluteString()));token=url.path.removeprefix('/')
            try:
                if view!=self.owner.trusted or url.scheme!='p011media' or url.netloc!='asset' or url.query or url.fragment or request.HTTPMethod()!='GET':raise ResourceDenied('denied')
                size=self.owner.media.registry.size(token)
                header=request.valueForHTTPHeaderField_('Range')
                start,end,status=byte_range(str(header) if header else None,size)
            except (ValueError,OSError):
                task.didFailWithError_(Foundation.NSError.errorWithDomain_code_userInfo_('P011Media',403,None));return
            headers={'Content-Type':self.owner.media.mime_types.get(token,'application/octet-stream'),'Content-Length':str(end-start+1),'Accept-Ranges':'bytes','Cache-Control':'no-store'}
            if status==206:headers['Content-Range']=f'bytes {start}-{end}/{size}'
            response=Foundation.NSHTTPURLResponse.alloc().initWithURL_statusCode_HTTPVersion_headerFields_(request.URL(),status,'HTTP/1.1',headers)
            self.owner.tasks[task]=[token,start,end]
            task.didReceiveResponse_(response);AppHelper.callAfter(self.chunk_,task)

        def chunk_(self,task):
            entry=self.owner.tasks.get(task)
            if entry is None:return
            token,offset,end=entry
            try:
                data=self.owner.media.registry.read(token,offset,min(256*1024,end-offset+1))
                task.didReceiveData_(Foundation.NSData.dataWithBytes_length_(data,len(data)));entry[1]+=len(data)
                if entry[1]>end:
                    self.owner.tasks.pop(task,None);task.didFinish()
                else:AppHelper.callAfter(self.chunk_,task)
            except (ValueError,OSError):
                self.owner.tasks.pop(task,None)
                task.didFailWithError_(Foundation.NSError.errorWithDomain_code_userInfo_('P011Media',410,None))

        def webView_stopURLSchemeTask_(self,view,task):self.owner.tasks.pop(task,None)

    class P011MediaConfiguration(WebKit.WKWebViewConfiguration):
        owner=None
        def init(self):
            self=objc.super(P011MediaConfiguration,self).init()
            if P011MediaConfiguration.owner is not None:
                self.setMediaTypesRequiringUserActionForPlayback_(0)
                self.setURLSchemeHandler_forURLScheme_(P011MediaConfiguration.owner.handler,'p011media')
            return self
    return P011MediaHandler,P011MediaConfiguration


class CocoaMediaTransport:
    def __init__(self,media):
        import WebKit
        handler,configuration=_types()
        self.media=media;self.trusted=None;self.tasks={}
        self.handler=handler.alloc().init();self.handler.owner=self
        self._original=WebKit.WKWebViewConfiguration
        configuration.owner=self
        WebKit.WKWebViewConfiguration=configuration

    def bind(self,window):
        from webview.platforms.cocoa import BrowserView
        self.trusted=BrowserView.instances[window.uid].webview

    def close(self):
        import WebKit
        self.tasks.clear();self.media.close();self.trusted=None
        _,configuration=_types();configuration.owner=None
        WebKit.WKWebViewConfiguration=self._original
