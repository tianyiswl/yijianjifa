"""Native exit is independent of transient view dialogs and service requests."""
import threading


class ExitController:
    def __init__(self, started, signal_stop, drain_requests, wait_stopped, finish):
        self.started = started
        self.signal_stop = signal_stop
        self.drain_requests = drain_requests
        self.wait_stopped = wait_stopped
        self.finish = finish
        self._lock = threading.Lock()

    def request(self):
        with self._lock:
            if self.started.is_set():
                return
            self.started.set()
            threading.Thread(target=self._stop, name='p011-close', daemon=False).start()

    def _stop(self):
        # Interrupt browser/worker waits before joining an in-flight service
        # request. Durable final markers and shutdown recovery remain owned by
        # the existing runtimes; this does not turn uncertainty into a retry.
        self.signal_stop()
        self.drain_requests()
        while not self.wait_stopped():
            pass
        self.finish()
