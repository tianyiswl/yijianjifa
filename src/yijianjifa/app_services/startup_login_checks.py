"""One optional read-only account pass per native application session."""
import threading

from yijianjifa.features.accounts.service import AccountService


class StartupLoginChecks:
    def __init__(self, engine, login_runtime):
        self._accounts = AccountService(engine)
        self._runtime = login_runtime
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._active_cancel = None
        self._thread = None

    def start(self):
        with self._lock:
            if self._stopped.is_set() or self._thread is not None:
                return False
            self._thread = threading.Thread(target=self._run, name='p011-startup-login', daemon=False)
            self._thread.start()
            return True

    def _run(self):
        for account in self._accounts.list():
            with self._lock:
                if self._stopped.is_set():
                    return
                if not self._runtime.available(account.platform):
                    continue
                cancel = threading.Event()
                self._active_cancel = cancel
            try:
                # The native checker skips busy accounts and never logs in or
                # continues publishing. Explicit user login can cancel this pass.
                self._runtime.check_session(account.id, cancel, background=True)
            except Exception:
                # One unavailable platform cannot prevent checking other accounts.
                pass
            finally:
                with self._lock:
                    self._active_cancel = None

    def close(self, timeout=0):
        with self._lock:
            self._stopped.set()
            if self._active_cancel is not None:
                self._active_cancel.set()
            thread = self._thread
        if thread is not None:
            thread.join(max(0, timeout))
        return thread is None or not thread.is_alive()
