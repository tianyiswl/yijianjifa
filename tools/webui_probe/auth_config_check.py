"""No window/network: inspect real Cocoa configuration isolation."""
import WebKit
from yijianjifa.webui.cocoa_media import CocoaMediaTransport
from yijianjifa.webui.auth_isolation import make_auth_configuration
class Media:
    def close(self):pass
transport=CocoaMediaTransport(Media())
try:
    application_config=WebKit.WKWebViewConfiguration.alloc().init()
    assert application_config.urlSchemeHandlerForURLScheme_('p011media') is not None
    config=make_auth_configuration();other=make_auth_configuration()
    assert config.urlSchemeHandlerForURLScheme_('p011media') is None
    assert len(config.userContentController().userScripts())==0
    assert config.userContentController()!=other.userContentController()
    assert not config.websiteDataStore().isPersistent()
    assert config.websiteDataStore()!=other.websiteDataStore()
    assert not config.preferences().javaScriptCanOpenWindowsAutomatically()
finally:transport.close()
print("native auth config isolation passed; no window or network opened")
from types import SimpleNamespace
from Foundation import NSURL, NSURLRequest
from yijianjifa.webui.cocoa_auth import auth_delegate_type
from yijianjifa.webui.auth_isolation import AuthNavigation
delegate=auth_delegate_type().alloc().init()
delegate.owner=SimpleNamespace(policy=AuthNavigation({'login.example.test'}))
class Action:
    def __init__(self,url,popup=False):self.url=url;self.popup=popup
    def request(self):return NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url))
    def targetFrame(self):return None if self.popup else self
results=[]
for action in (Action('https://login.example.test'),Action('https://evil.example.test'),Action('https://login.example.test',True)):
    delegate.webView_decidePolicyForNavigationAction_decisionHandler_(None,action,results.append)
assert results==[WebKit.WKNavigationActionPolicyAllow,WebKit.WKNavigationActionPolicyCancel,WebKit.WKNavigationActionPolicyCancel]
assert delegate.webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(None,None,None,None) is None
files=[]
delegate.webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(None,None,None,files.append)
assert files==[None]
print('native auth delegates deny external navigation, new windows and file selection')
