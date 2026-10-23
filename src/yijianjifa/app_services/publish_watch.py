"""Observe expired locally held schedules independently of the displayed page.

This is not a submission worker: it only invokes the local recovery transaction.
"""
from datetime import datetime, timezone
import logging
import threading
from .publish_control import recover_local_software


class PublishWatch:
    def __init__(self, engine, *, recover=recover_local_software, interval=5):
        if interval<=0:raise ValueError('interval must be positive')
        self._engine=engine
        self._recover=recover
        self._interval=interval
        self._stop=threading.Event()
        self._lock=threading.Lock()
        self._thread=None
        self.failed=False
        self._last_success_at=None
        self._last_failure_at=None

    def start(self):
        with self._lock:
            if self._stop.is_set():return False
            if self._thread is not None and self._thread.is_alive():return True
            self._thread=threading.Thread(target=self._run,name='p011-publish-watch',daemon=False)
            self._thread.start()
            return True

    def status(self):
        with self._lock:
            running=self._thread is not None and self._thread.is_alive() and not self._stop.is_set()
            state=('failed' if self.failed else 'healthy' if self._last_success_at else 'starting') if running else 'stopped'
            return dict(state=state,last_success_at=self._last_success_at,last_failure_at=self._last_failure_at)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._recover(self._engine,now=datetime.now(timezone.utc))
                with self._lock:
                    self.failed=False
                    self._last_success_at=datetime.now(timezone.utc).isoformat()
            except Exception:
                # Do not expose task paths, snapshots or session information.
                with self._lock:
                    if not self.failed:self._last_failure_at=datetime.now(timezone.utc).isoformat()
                    self.failed=True
                logging.getLogger(__name__).warning('Local schedule check failed; will retry')
            if self._stop.wait(self._interval):return

    def close(self,timeout=0):
        with self._lock:
            self._stop.set()
            thread=self._thread
        if thread is not None:thread.join(timeout)
        return thread is None or not thread.is_alive()
