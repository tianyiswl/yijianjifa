"""Bounded single-owner service executor; callbacks choose their UI scheduler."""
from concurrent.futures import ThreadPoolExecutor
import threading
from .library_queries import LibraryQueries


class DispatchWorker:
    def __init__(self,capacity=32):
        self._pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='p011-services')
        self._slots=threading.BoundedSemaphore(capacity)
        self._lock=threading.Lock()
        self._closed=False

    def submit(self,operation,deliver):
        with self._lock:
            if self._closed or not self._slots.acquire(blocking=False):return False
            def run():
                try:result=operation()
                except Exception:result=LibraryQueries._error('internal_error',None)
                try:deliver(result)
                finally:self._slots.release()
            self._pool.submit(run)
            return True

    def close(self, *, wait=False):
        with self._lock:
            self._closed=True
        # Joining while holding the admission lock can deadlock a callback.
        self._pool.shutdown(wait=wait)
