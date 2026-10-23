"""Disposable T02 native WebView experiment, not a product entry point."""
from __future__ import annotations

import argparse
import base64
import importlib.metadata
import json
import os
import platform
import subprocess
import threading
from pathlib import Path

import webview

BASE = Path(__file__).resolve().parent


class Probe:
    def __init__(self, output, worker_delay=1):
        self._output = output
        self._window = None
        self._stop = threading.Event()
        self._worker = None
        self._worker_delay = worker_delay
        self._lock = threading.RLock()
        self._report = {'os': platform.platform(), 'python': platform.python_version(),
                        'pywebview': importlib.metadata.version('pywebview'),
                        'http_server': False, 'production_data_access': False,
                        'file_dialog': 'not_tested', 'system_reveal': 'not_tested',
                        'navigation_denied': [], 'worker_done': False}

    def _save(self):
        with self._lock:
            self._output.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._output.with_suffix('.tmp')
            temporary.write_text(json.dumps(self._report, indent=2), encoding='utf-8')
            temporary.replace(self._output)

    def command(self, operation):
        """Only diagnostic operations. No page-supplied paths/SQL/code."""
        if operation == 'ping':
            return {'ok': True, 'protocol': 1}
        if operation == 'start':
            with self._lock:
                if self._worker is None:
                    def work():
                        self._report['worker_done'] = not self._stop.wait(self._worker_delay)
                        self._save()
                    self._worker = threading.Thread(target=work, name='T02-native-worker')
                    self._worker.start()
            return {'accepted': True}
        if operation == 'choose':
            paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False)
            self._report['file_dialog'] = 'selected' if paths else 'cancelled'
            self._save()
            return {'selected': bool(paths)}  # no path or contents sent to JS
        if operation == 'reveal':
            fixture = BASE / 'assets/probe.mp4'
            if platform.system() == 'Darwin':
                r = subprocess.run(['open', '-R', str(fixture)], check=False)
                self._report['system_reveal'] = {'command_exit': r.returncode, 'visual_verified': False}
            elif platform.system() == 'Windows':
                subprocess.Popen(['explorer', '/select,', str(fixture)])
                self._report['system_reveal'] = {'launched': True, 'visual_verified': False}
            self._save()
            return {'requested': True}
        return {'error': 'unsupported_operation'}

    def observations(self, value):
        if not isinstance(value, dict) or len(json.dumps(value)) > 20000:
            return {'error': 'invalid_observations'}
        self._report['javascript_observations'] = value
        self._save()
        return {'recorded': True}

    def _shutdown(self):
        self._stop.set()
        if self._worker:
            self._worker.join(2)
        self._report['worker_joined'] = not (self._worker and self._worker.is_alive())
        self._report['closed_normally'] = True
        self._save()


def install_mac_fence(window, probe):
    # Version-specific experimental adapter; must be rechecked on pywebview updates.
    from webview.platforms.cocoa import BrowserView
    import WebKit

    class T02NavigationDelegate(BrowserView.BrowserDelegate):
        def webView_decidePolicyForNavigationAction_decisionHandler_(self, view, action, handler):
            url = str(action.request().URL().absoluteString())
            if url == 'about:blank':
                handler(WebKit.WKNavigationActionPolicyAllow)
            else:
                probe._report['navigation_denied'].append(url)
                probe._save()
                handler(WebKit.WKNavigationActionPolicyCancel)

    native = BrowserView.instances[window.uid]
    delegate = T02NavigationDelegate.alloc().init()
    native._browserDelegate = delegate
    native.webview.setNavigationDelegate_(delegate)
    probe._report['navigation_guard'] = 'macOS WKNavigationDelegate'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path.home() / 'P011-T02-diagnostics/native-probe.json')
    parser.add_argument('--auto-close', type=float, default=0)
    parser.add_argument('--worker-delay', type=float, default=1)
    parser.add_argument('--fail-core', action='store_true')
    args = parser.parse_args()
    probe = Probe(args.output, args.worker_delay)
    if args.fail_core:
        probe._report.update(core_start='controlled_failure', writer_started=False)
        probe._save()
        return 20
    webview.settings['ALLOW_FILE_URLS'] = False
    webview.settings['ALLOW_DOWNLOADS'] = False
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
    html = (BASE / 'assets/native.html').read_text(encoding='utf-8')
    html = html.replace('__MEDIA__', base64.b64encode((BASE / 'assets/probe.mp4').read_bytes()).decode())
    window = webview.create_window('P011 T02 native diagnostic', html=html, js_api=probe,
                                   width=1000, height=720)
    probe._window = window
    if platform.system() == 'Darwin':
        window.events.before_show += lambda: install_mac_fence(window, probe)
    else:
        # This revision is deliberately not a Windows security-capable candidate.
        probe._report['navigation_guard'] = 'not_implemented_on_this_os'
        probe._save()
        return 21
    window.events.closing += probe._shutdown
    window.events.initialized += lambda renderer: probe._report.update(renderer=renderer)

    def started():
        if not window.events.loaded.wait(15):
            probe._report['load_timeout'] = True
            probe._save()
            window.destroy()
            return
        probe._report['page_loaded'] = True
        probe._save()
        # An independent static fixture, no remote content and no application API.
        other = webview.create_window('T02 untrusted fixture', html='<h1>Untrusted fixture</h1>', hidden=True)
        if other.events.loaded.wait(10):
            probe._report['untrusted_application_api_absent'] = other.evaluate_js(
                'Object.keys(window.pywebview.api).length === 0')
        else:
            probe._report['untrusted_fixture_timeout'] = True
        other.destroy()
        window.resize(800, 600)
        if args.auto_close:
            probe._stop.wait(args.auto_close)
            # Deliberately attempted foreign navigation; native delegate cancels before request.
            window.run_js('window.location.href="https://untrusted.invalid/escape"')
            probe._stop.wait(0.5)
            window.destroy()
    webview.start(started, gui='cocoa', http_server=False, private_mode=True)
    probe._shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
