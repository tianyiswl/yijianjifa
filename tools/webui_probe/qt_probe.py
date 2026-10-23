"""T02 diagnostic only. No production imports, database, account or HTTP server."""
from __future__ import annotations

import argparse
import json
import platform
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QObject, QTimer, QUrl, Slot, qVersion
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
    QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestJob,
    QWebEngineUrlScheme, QWebEngineUrlSchemeHandler, qWebEngineChromiumVersion,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog

BASE = Path(__file__).resolve().parent
TRUSTED = 'probe://app/index.html'
RESOURCES = {'/index.html': ('text/html', BASE / 'assets/index.html'),
             '/probe.mp4': ('video/mp4', BASE / 'assets/probe.mp4')}


class Resources(QWebEngineUrlSchemeHandler):
    def requestStarted(self, job):
        url = job.requestUrl()
        if url.host() != 'app' or url.path() not in RESOURCES or url.hasQuery():
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        mime, path = RESOURCES[url.path()]
        device = QFile(str(path), job)
        if not device.open(QIODevice.OpenModeFlag.ReadOnly):
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        job.reply(mime.encode(), device)


class NetworkFence(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        u = info.requestUrl()
        allowed = (u.scheme() == 'probe' and u.host() == 'app' and u.path() in RESOURCES
                   and not u.hasQuery()) or u.toString() == 'qrc:///qtwebchannel/qwebchannel.js'
        info.block(not allowed)


class Page(QWebEnginePage):
    def __init__(self, profile, parent):
        super().__init__(profile, parent)
        self.denied = []

    def acceptNavigationRequest(self, url, kind, main):
        allowed = main and url.toString() == TRUSTED
        if not allowed:
            self.denied.append(url.toString())
        return allowed

    def createWindow(self, kind):
        return None


class Bridge(QObject):
    def __init__(self, view, report, output):
        super().__init__(view)
        self.view, self.report, self.output = view, report, output
        self.stop = threading.Event()
        self.worker = None
        self.done = False

    @Slot(str, result=str)
    def call(self, operation):
        if operation == 'ping':
            return json.dumps({'ok': True, 'protocol': 1})
        if operation == 'start':
            if self.worker is None:
                def work():
                    self.done = not self.stop.wait(1.0)
                self.worker = threading.Thread(target=work, name='T02-finite-worker')
                self.worker.start()
            return json.dumps({'accepted': True})
        if operation == 'status':
            return json.dumps({'done': self.done})
        return json.dumps({'error': 'unsupported_operation'})

    @Slot()
    def chooseFile(self):
        path, _ = QFileDialog.getOpenFileName(self.view, 'T02 select a test file')
        self.report['file_dialog'] = 'selected' if path else 'cancelled'
        self.save()

    @Slot(str)
    def record(self, payload):
        if len(payload) > 20000:
            return
        try:
            value = json.loads(payload)
        except ValueError:
            return
        if isinstance(value, dict):
            self.report['javascript_observations'] = value
            self.save()

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(self.report, indent=2), encoding='utf-8')

    def shutdown(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=2)
        self.report['worker_joined'] = not (self.worker and self.worker.is_alive())
        self.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--auto-close', type=int, default=0)
    parser.add_argument('--fail-core', action='store_true')
    args = parser.parse_args()
    if args.fail_core:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'core_start': 'controlled_failure', 'writer_started': False}))
        return 20
    scheme = QWebEngineUrlScheme(b'probe')
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme)
    QWebEngineUrlScheme.registerScheme(scheme)
    app = QApplication([sys.argv[0]])
    app.setApplicationName('P011 T02 shell diagnostic')
    report = {'os': platform.platform(), 'qt': qVersion(), 'chromium': qWebEngineChromiumVersion(),
              'http_server': False, 'production_data_access': False,
              'file_dialog': 'not_tested', 'system_reveal': 'not_tested'}
    view = QWebEngineView()
    profile = QWebEngineProfile(view)  # memory-only, never use a platform session
    fence, handler = NetworkFence(profile), Resources(profile)
    profile.setUrlRequestInterceptor(fence)
    profile.installUrlSchemeHandler(b'probe', handler)
    page = Page(profile, view)
    view.setPage(page)
    settings = page.settings()
    for attr in ('LocalContentCanAccessFileUrls', 'LocalContentCanAccessRemoteUrls',
                 'JavascriptCanOpenWindows', 'LocalStorageEnabled'):
        settings.setAttribute(getattr(QWebEngineSettings.WebAttribute, attr), False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
    bridge = Bridge(view, report, args.output)
    channel = QWebChannel(page)
    channel.registerObject('probe', bridge)
    page.setWebChannel(channel)
    page.renderProcessTerminated.connect(lambda status, code: report.update(renderer_exit=code))
    page.loadFinished.connect(lambda ok: (report.setdefault('load_events', []).append(ok), bridge.save()))
    view.setWindowTitle('P011 T02 - diagnostic only')
    view.resize(1000, 720)
    view.show()
    view.load(QUrl(TRUSTED))
    # A separate untrusted-origin fixture has no web channel and no real network/login.
    other_profile = QWebEngineProfile(view)
    other_fence = NetworkFence(other_profile)
    other_profile.setUrlRequestInterceptor(other_fence)
    other = QWebEnginePage(other_profile, view)
    other.setHtml('<title>Untrusted test fixture</title>', QUrl('https://untrusted.invalid/'))
    other.loadFinished.connect(lambda ok: other.runJavaScript(
        'typeof qt === "undefined" && typeof QWebChannel === "undefined"',
        lambda absent: (report.update(untrusted_bridge_absent=absent), bridge.save())))
    QTimer.singleShot(2500, lambda: (view.resize(800, 600), report.update(resized=[800, 600])))
    QTimer.singleShot(3500, lambda: page.runJavaScript(
        'window.location.href="https://untrusted.invalid/escape"'))
    if args.auto_close:
        QTimer.singleShot(args.auto_close * 1000, view.close)
    app.aboutToQuit.connect(bridge.shutdown)
    code = app.exec()
    report['navigation_denied'] = page.denied
    report['closed_normally'] = True
    bridge.save()
    other.deleteLater()
    page.deleteLater()
    app.sendPostedEvents(None, 0)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
