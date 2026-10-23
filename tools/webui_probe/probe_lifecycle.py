"""Cancellable diagnostic orchestration; GUI close waits for this owner to finish."""
import time


def wait_loaded(window, stop):
    deadline = time.monotonic() + 15
    while not window.events.loaded.is_set():
        if time.monotonic() >= deadline or stop.wait(0.05):
            return False
    return not stop.is_set()


def run_checks(window, create_other, stop, registry, token, report, done):
    other = None
    try:
        if not wait_loaded(window, stop):
            report['load_cancelled_or_timeout'] = True
            return
        report['page_loaded'] = True
        if stop.wait(8):
            return
        other = create_other()
        if not wait_loaded(other, stop) or stop.wait(1):
            return
        other.destroy()
        other = None
        if stop.is_set():
            return
        window.run_js('window.deniedMedia=false; const bad=document.createElement("video");bad.onerror=()=>window.deniedMedia=true;bad.src="p011media://asset/unknown";bad.load();')
        if stop.wait(1):
            return
        report['unknown_media_error'] = window.evaluate_js('window.deniedMedia')
        if stop.is_set():
            return
        registry.revoke(token)
        window.run_js(f'window.revokedMedia=false; const revoked=document.createElement("video");revoked.onerror=()=>window.revokedMedia=true;revoked.src="p011media://asset/{token}";revoked.load();')
        if stop.wait(1):
            return
        report['revoked_media_error'] = window.evaluate_js('window.revokedMedia')
    finally:
        if other is not None:
            other.destroy()
        done.set()
        window.destroy()
