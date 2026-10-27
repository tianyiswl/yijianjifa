import threading
from yijianjifa.webui.exit_controller import ExitController


def test_native_exit_signals_workers_before_draining_requests_and_only_runs_once():
    events=[];draining=threading.Event();release=threading.Event();finished=threading.Event();started=threading.Event()
    def drain():
        events.append('drain');draining.set();assert release.wait(2)
    def finish():events.append('finish');finished.set()
    controller=ExitController(started,lambda:events.append('signal'),drain,lambda:events.append('wait') or True,finish)
    controller.request();assert started.is_set();assert draining.wait(2)
    for _ in range(5):controller.request()
    assert events==['signal','drain']
    release.set();assert finished.wait(2)
    assert events==['signal','drain','wait','finish']


def test_native_exit_keeps_waiting_for_worker_cleanup_before_destroying_window():
    finished=threading.Event();events=[]
    def wait():events.append('wait');return events.count('wait')==3
    controller=ExitController(threading.Event(),lambda:events.append('signal'),lambda:events.append('drain'),wait,finished.set)
    controller.request();assert finished.wait(2);assert events==['signal','drain','wait','wait','wait']


def test_qt_window_close_requests_native_cleanup_even_if_editor_hook_would_block(qapp):
    from PySide6.QtGui import QCloseEvent
    from yijianjifa.webui.qt_runtime import Window,Scheduler
    window=Window(Scheduler());requests=[];scripts=[]
    from yijianjifa import APP_VERSION
    assert APP_VERSION in window.windowTitle()
    window.request_exit=lambda:requests.append('exit')
    window.page.runJavaScript=lambda *args:scripts.append(args)
    event=QCloseEvent();window.closeEvent(event)
    assert not event.isAccepted();assert requests==['exit'];assert scripts==[]
    window.allowed_close.set();event=QCloseEvent();window.closeEvent(event)
    assert event.isAccepted();assert requests==['exit']
