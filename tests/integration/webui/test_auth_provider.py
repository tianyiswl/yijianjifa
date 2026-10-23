"""Exercise provider lifecycle with a controlled UI scheduler, no real UI/network."""
import threading
from queue import Queue


def test_provider_returns_only_after_window_closes_and_cancellation_wins():
    from yijianjifa.webui.cocoa_auth import CocoaAuthProvider
    scheduled=Queue();shown=threading.Event();cancel=threading.Event();result=[]
    class Window:
        def __init__(self,url,policy,observer,closed):self.closed=closed
        def show(self):shown.set()
        def cancel(self):self.closed(None,None)
    provider=CocoaAuthProvider('https://login.example.test',{'login.example.test'},lambda *args:None,
        schedule=lambda fn:scheduled.put(fn),window_factory=Window)
    thread=threading.Thread(target=lambda:result.append(provider(None,cancel)));thread.start()
    scheduled.get(timeout=1)();assert shown.is_set();assert result==[]
    cancel.set();scheduled.get(timeout=2)();thread.join(2)
    assert not thread.is_alive() and result==[None]


def test_provider_cancelled_before_ui_dispatch_never_opens_window():
    from yijianjifa.webui.cocoa_auth import CocoaAuthProvider
    scheduled=Queue();cancel=threading.Event();result=[];opened=[]
    def window(*args):opened.append(True);raise AssertionError('must not open')
    provider=CocoaAuthProvider('https://login.example.test',{'login.example.test'},lambda *args:None,
        schedule=lambda fn:scheduled.put(fn),window_factory=window)
    thread=threading.Thread(target=lambda:result.append(provider(None,cancel)));thread.start()
    callback=scheduled.get(timeout=1);cancel.set();callback();thread.join(2)
    assert result==[None] and opened==[]


def test_window_creation_failure_cleans_partial_window_before_return():
    from yijianjifa.webui.cocoa_auth import CocoaAuthProvider
    scheduled=Queue();cancel=threading.Event();failures=[];closed=[]
    class Window:
        def __init__(self,url,policy,observer,on_closed):self.on_closed=on_closed
        def show(self):raise RuntimeError('partial native window')
        def fail(self):closed.append(True);self.on_closed(None,'authentication_window_failed')
    provider=CocoaAuthProvider('https://login.example.test',{'login.example.test'},lambda *args:None,
        schedule=lambda fn:scheduled.put(fn),window_factory=Window)
    def work():
        try:provider(None,cancel)
        except RuntimeError:failures.append(True)
    thread=threading.Thread(target=work);thread.start();scheduled.get(timeout=1)();thread.join(2)
    assert failures==[True] and closed==[True]
