"""Thread-owned synchronous Playwright leases, with independent browser contexts.

Playwright's sync SDK owns one asyncio loop in its thread. Starting a second
SDK in that thread fails even if the first browser context has been closed.
Leases share only the SDK, never profiles, pages, account locks or authority.
"""
import threading

_local = threading.local()


class _Runtime:
    def __init__(self, driver, manager):
        self.driver = driver
        self.manager = manager
        self.stop_retryable = True
        self.owner = threading.current_thread()
        self.leases = set()
        self.stop_failed = False


class PlaywrightLease:
    def __init__(self, runtime):
        self._runtime = runtime
        self._released = False
        runtime.leases.add(self)

    def _check(self):
        if threading.current_thread() is not self._runtime.owner:
            raise ValueError('Playwright requires its original host worker thread')
        if self._released:
            raise ValueError('Playwright lease already released')

    def __getattr__(self, name):
        self._check()
        if self._runtime.stop_failed:
            raise ValueError('Playwright runtime cleanup must be retried')
        return getattr(self._runtime.driver, name)

    @property
    def exclusive(self):
        self._check()
        return len(self._runtime.leases) == 1

    def stop(self):
        """Release this owner; only the last owner stops the actual SDK.

        A failed actual stop retains the last lease and registry for retry.
        No new owner can acquire a runtime whose stop outcome is uncertain.
        """
        if self._released:
            return
        self._check()
        runtime = self._runtime
        if len(runtime.leases) == 1:
            if not runtime.stop_retryable:
                raise ValueError('Playwright runtime stop outcome remains unknown')
            try:
                runtime.driver.stop()
            except Exception:
                runtime.stop_failed = True
                # The SDK marks its context manager exited before stop_sync.
                # A second public stop would then return without doing cleanup.
                # Retain the uncertain runtime instead of accepting that no-op.
                if getattr(runtime.manager, '_exit_was_called', False):
                    runtime.stop_retryable = False
                raise
            if getattr(_local, 'runtime', None) is runtime:
                del _local.runtime
        runtime.leases.remove(self)
        self._released = True


def acquire_playwright():
    runtime = getattr(_local, 'runtime', None)
    if runtime is None:
        # Dynamic import preserves injected SDK factories in lifecycle tests.
        from playwright.sync_api import sync_playwright
        manager = sync_playwright()
        runtime = _Runtime(manager.start(), manager)
        _local.runtime = runtime
    elif runtime.stop_failed:
        raise ValueError('Playwright runtime cleanup must be retried')
    return PlaywrightLease(runtime)


def can_stop_exclusively(driver):
    """Raw injected drivers retain their legacy dedicated-driver contract."""
    return driver is not None and (not isinstance(driver, PlaywrightLease) or driver.exclusive)
