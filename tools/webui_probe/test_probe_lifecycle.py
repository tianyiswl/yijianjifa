import threading
from types import SimpleNamespace

from probe_lifecycle import run_checks


class Window:
    def __init__(self):
        loaded = threading.Event()
        loaded.set()
        self.events = SimpleNamespace(loaded=loaded)
        self.calls = []

    def destroy(self):
        self.calls.append('destroy')

    def run_js(self, script):
        self.calls.append('js')

    def evaluate_js(self, script):
        self.calls.append('evaluate')
        return True


class StopAfter:
    def __init__(self, n):
        self.n = n
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.n -= 1
        self.stopped = self.n <= 0
        return self.stopped


def test_close_during_playback_does_not_create_more_windows():
    window, done = Window(), threading.Event()
    created = []
    run_checks(window, lambda: created.append(True), StopAfter(1),
               SimpleNamespace(revoke=lambda _: None), 'token', {}, done)
    assert created == []
    assert window.calls == ['destroy']
    assert done.is_set()


def test_close_during_foreign_check_cleans_both_without_js():
    window, other, done = Window(), Window(), threading.Event()
    run_checks(window, lambda: other, StopAfter(2),
               SimpleNamespace(revoke=lambda _: None), 'token', {}, done)
    assert other.calls == ['destroy']
    assert window.calls == ['destroy']
    assert done.is_set()
