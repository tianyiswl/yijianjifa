"""Native authentication configuration. No application handlers or user scripts.

Hosts must be supplied by a reviewed native provider, never by Web UI input.
This module creates no window and loads no URL.
"""
import re
from urllib.parse import urlsplit


class AuthNavigation:
    def __init__(self, hosts):
        self.hosts=frozenset(hosts)
        if not self.hosts or any(type(h) is not str or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?',h) for h in self.hosts):
            raise ValueError('invalid authentication hosts')

    def allows(self,url,*,new_window=False):
        if new_window or type(url) is not str or len(url)>8192 or any(ord(c)<33 for c in url):return False
        try:
            parsed=urlsplit(url)
            return (parsed.scheme=='https' and parsed.hostname in self.hosts
                    and parsed.port in (None,443) and parsed.username is None
                    and parsed.password is None)
        except ValueError:return False


def make_auth_configuration():
    import WebKit
    import objc
    # CocoaMediaTransport replaces WebKit.WKWebViewConfiguration in Python.
    # Fetch the genuine Objective-C class to avoid inheriting p011media access.
    config=objc.lookUpClass('WKWebViewConfiguration').alloc().init()
    config.setUserContentController_(WebKit.WKUserContentController.alloc().init())
    config.setWebsiteDataStore_(WebKit.WKWebsiteDataStore.nonPersistentDataStore())
    config.setProcessPool_(WebKit.WKProcessPool.alloc().init())
    config.preferences().setJavaScriptCanOpenWindowsAutomatically_(False)
    return config
