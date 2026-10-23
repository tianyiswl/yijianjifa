"""Mac-only custom-scheme experiment. No HTTP server or product data."""
import argparse
import json
import platform
import threading
from pathlib import Path
from urllib.parse import urlsplit

import objc
import Foundation
import WebKit
from PyObjCTools import AppHelper
import webview
from webview.platforms.cocoa import BrowserView

from media_registry import MediaRegistry, ResourceDenied, byte_range
from native_probe import Probe, install_mac_fence, BASE
from probe_lifecycle import run_checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--media', type=Path, default=BASE/'media/fixture.mp4')
    parser.add_argument('--output', type=Path, default=Path.home()/'P011-T02-diagnostics/resource-probe.json')
    parser.add_argument('--fail-core', action='store_true')
    args = parser.parse_args()
    registry = MediaRegistry()
    probe = Probe(args.output)
    try:
        if args.fail_core:
            raise ResourceDenied('startup_test_failure')
        token = registry.grant(args.media)
    except ResourceDenied:
        probe._report.update(startup_error='media_unavailable', writer_started=False)
        error = webview.create_window('P011 T02 startup failure', html='<h1>无法启动媒体验证</h1><p>测试媒体不可用。请关闭窗口，检查测试文件后重试。未启动任务，未修改业务数据。</p>')
        def close_error():
            probe._report['error_page_loaded'] = bool(error.events.loaded.wait(10))
            probe._save()
            probe._stop.wait(2)
            error.destroy()
        webview.start(close_error, gui='cocoa', http_server=False, private_mode=True)
        registry.close()
        probe._shutdown()
        return 20
    probe._report.update(media_bytes=registry.size(token), transport='WKURLSchemeHandler',
                         requests=[], denied=[], chunks=0, max_chunk_bytes=0)

    class SchemeHandler(Foundation.NSObject, protocols=[objc.protocolNamed('WKURLSchemeHandler')]):
        def webView_startURLSchemeTask_(self, view, task):
            request = task.request()
            u = urlsplit(str(request.URL().absoluteString()))
            key = u.path.removeprefix('/')
            try:
                if view != self.trusted or u.scheme != 'p011media' or u.netloc != 'asset' or u.query or u.fragment or request.HTTPMethod() != 'GET':
                    raise ResourceDenied('origin_denied')
                size = registry.size(key)
                header = request.valueForHTTPHeaderField_('Range')
                start, end, status = byte_range(str(header) if header else None, size)
            except ResourceDenied as exc:
                probe._report['denied'].append({'reason': str(exc), 'foreign_view': view != self.trusted})
                task.didFailWithError_(Foundation.NSError.errorWithDomain_code_userInfo_('P011Resource', 403, None))
                return
            probe._report['requests'].append({'range': str(header) if header else None, 'status': status, 'start': start, 'end': end})
            headers = {'Content-Type': 'video/mp4', 'Content-Length': str(end-start+1), 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store'}
            if status == 206:
                headers['Content-Range'] = f'bytes {start}-{end}/{size}'
            response = Foundation.NSHTTPURLResponse.alloc().initWithURL_statusCode_HTTPVersion_headerFields_(request.URL(), status, 'HTTP/1.1', headers)
            self.tasks[task] = [key, start, end]
            task.didReceiveResponse_(response)
            AppHelper.callAfter(self.sendChunk_, task)

        def sendChunk_(self, task):
            entry = self.tasks.get(task)
            if entry is None:
                return
            key, offset, end = entry
            try:
                length = min(256 * 1024, end-offset+1)
                data = registry.read(key, offset, length)
                task.didReceiveData_(Foundation.NSData.dataWithBytes_length_(data, len(data)))
                probe._report['chunks'] += 1
                probe._report['max_chunk_bytes'] = max(probe._report['max_chunk_bytes'], len(data))
                entry[1] += length
                if entry[1] > end:
                    self.tasks.pop(task, None)
                    task.didFinish()
                    probe._save()
                else:
                    AppHelper.callAfter(self.sendChunk_, task)
            except ResourceDenied:
                self.tasks.pop(task, None)
                task.didFailWithError_(Foundation.NSError.errorWithDomain_code_userInfo_('P011Resource', 410, None))

        def webView_stopURLSchemeTask_(self, view, task):
            self.tasks.pop(task, None)

    handler = SchemeHandler.alloc().init()
    handler.tasks = {}
    handler.trusted = None
    OriginalConfiguration = WebKit.WKWebViewConfiguration

    class ResourceConfiguration(OriginalConfiguration):
        def init(self):
            self = objc.super(ResourceConfiguration, self).init()
            self.setURLSchemeHandler_forURLScheme_(handler, 'p011media')
            return self

    WebKit.WKWebViewConfiguration = ResourceConfiguration
    webview.settings['ALLOW_FILE_URLS'] = False
    webview.settings['ALLOW_DOWNLOADS'] = False
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
    html = (BASE/'assets/native.html').read_text()
    html = html.replace('data:video/mp4;base64,__MEDIA__', f'p011media://asset/{token}')
    html = html.replace('media-src data:', 'media-src p011media:')
    html = html.replace('v.currentTime=1;', 'v.currentTime=17;')
    window = webview.create_window('P011 T02 controlled media', html=html, js_api=probe)
    probe._window = window

    def prepare():
        handler.trusted = BrowserView.instances[window.uid].webview
        install_mac_fence(window, probe)
    window.events.before_show += prepare
    runner_done = threading.Event()

    def closing():
        probe._stop.set()
        if not runner_done.is_set():
            # Keep the native event loop alive until its test owner stops using it.
            return False
        probe._shutdown()
    window.events.closing += closing

    def run():
        run_checks(window, lambda: webview.create_window('T02 foreign media fixture',
                   html=f'<video muted autoplay src="p011media://asset/{token}"></video>', hidden=True),
                   probe._stop, registry, token, probe._report, runner_done)
    try:
        webview.start(run, gui='cocoa', http_server=False, private_mode=True)
    finally:
        handler.tasks.clear()
        registry.close()
        probe._report['resources_closed'] = True
        probe._report['diagnostic_runner_done'] = runner_done.is_set()
        probe._shutdown()
        WebKit.WKWebViewConfiguration = OriginalConfiguration
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
