from types import SimpleNamespace
from yijianjifa.webui import douyin_scheduled as scheduled


def fixture(minimized=False,hidden=False):
    calls=[]
    window=SimpleNamespace(isMiniaturized=lambda:minimized,
        deminiaturize_=lambda sender:calls.append('restore'),
        orderFront_=lambda sender:calls.append('show'),
        setTitle_=lambda text:calls.append(text))
    app=SimpleNamespace(isHidden=lambda:hidden,unhideWithoutActivation=lambda:calls.append('unhide'))
    return window,app,calls


def test_minimized_publishing_window_is_restored_without_activating_app():
    window,app,calls=fixture(minimized=True)
    assert scheduled.restore_publishing_window(window,app) is True
    assert calls[:2]==['restore','show']
    assert '恢复' in calls[2]


def test_hidden_application_is_unhidden_before_page_work():
    window,app,calls=fixture(hidden=True)
    assert scheduled.restore_publishing_window(window,app) is True
    assert calls[:2]==['unhide','show']


def test_normal_window_does_not_steal_focus():
    window,app,calls=fixture()
    assert scheduled.restore_publishing_window(window,app) is False
    assert not calls


def test_busy_javascript_cannot_prevent_host_timeout(monkeypatch):
    w=scheduled.ScheduledWindow.__new__(scheduled.ScheduledWindow)
    w.finished=False;w.busy=True;w.view=object();w.window=None
    w.started=0;w.receipt_confirmed=False
    calls=[];w.complete=lambda **kw:calls.append(kw)
    monkeypatch.setattr(scheduled.time,'monotonic',lambda:301)
    w.observe()
    assert calls==[dict(confirmed=False,error='submission_timeout')]


def test_occluded_editor_window_is_shown_without_key_window_activation():
    window,app,calls=fixture()
    window.occlusionState=lambda:0
    assert scheduled.restore_publishing_window(window,app) is True
    assert calls[0]=='show'


def test_visible_editor_stays_in_place():
    window,app,calls=fixture()
    window.occlusionState=lambda:2
    assert scheduled.restore_publishing_window(window,app) is False
    assert not calls
