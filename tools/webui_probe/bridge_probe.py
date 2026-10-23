"""Mac native-message integration fixture; creates only a temporary database."""
import argparse
import json
from pathlib import Path
import tempfile
import threading

import webview
from PyObjCTools import AppHelper
from webview.platforms.cocoa import BrowserView

from yijianjifa.app_services.library_queries import LibraryQueries
from yijianjifa.webui.cocoa_read_bridge import CocoaReadBridge
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.storage.db import create_engine_for, migrate_to_head


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'production_data_access': False, 'adapter': 'CocoaReadBridge'}
    stop = threading.Event(); done = threading.Event()
    with tempfile.TemporaryDirectory(prefix='p011-native-contract-') as folder:
        engine = create_engine_for(Path(folder)/'fixture.db'); migrate_to_head(engine)
        CopyRepository(engine).create(id='C1', title='<img src=x onerror="window.injected=true">', voice='', body='native core', tags='', full='')
        bridge = CocoaReadBridge(LibraryQueries(engine))

        html = '''<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; frame-src about:; style-src 'unsafe-inline'"><h1>T03 原生可信桥接验证</h1><p>隔离数据库技术夹具，不是产品页面</p><pre id="out"></pre><script>
window.result={};
const request=JSON.stringify({v:1,request_id:'native1',operation:'copy.get',params:{id:'C1'}});
(async()=>{
try {
const raw=await window.webkit.messageHandlers.p011Read.postMessage(request);
const data=JSON.parse(raw); result.core=data; document.querySelector('#out').textContent=data.data.title;
result.injected=Boolean(window.injected); result.imageNodes=document.querySelectorAll('#out img').length;
result.objectRequest=JSON.parse(await window.webkit.messageHandlers.p011Read.postMessage({operation:'copy.get'})).error.code;
const bad=await window.webkit.messageHandlers.p011Read.postMessage(JSON.stringify({v:1,request_id:'bad',operation:'shell.exec',params:{}}));
result.unknown=JSON.parse(bad).error.code;
const f=document.createElement('iframe');document.body.appendChild(f);
const denied=await f.contentWindow.webkit.messageHandlers.p011Read.postMessage(request);
result.subframe=JSON.parse(denied).error.code;
result.done=true;
} catch(e){result.error=String(e);result.done=true;}
})();</script>'''
        window=webview.create_window('P011 T03 native bridge', html=html)

        window.events.before_show += lambda: bridge.attach(window)
        def closing():
            stop.set()
            if not done.is_set(): return False
            bridge.close()
        window.events.closing += closing
        def evaluate(script):
            # Native evaluateJavaScript does not use JavaScript eval or relax CSP.
            ready=threading.Event(); box={}
            def callback(value, error):
                box['value']=value;box['error']=str(error) if error else None;ready.set()
            def invoke():
                BrowserView.instances[window.uid].webview.evaluateJavaScript_completionHandler_(script, callback)
            AppHelper.callAfter(invoke)
            for _ in range(100):
                if ready.wait(.05):
                    if box['error']: raise RuntimeError(box['error'])
                    return box['value']
                if stop.is_set(): return None
            raise RuntimeError('native evaluation timeout')

        def run():
            try:
                for _ in range(200):
                    if stop.wait(.05): return
                    if window.events.loaded.is_set(): break
                else: report['error']='load_timeout';return
                for _ in range(100):
                    if stop.wait(.1): return
                    raw=evaluate('JSON.stringify(window.result)')
                    result=json.loads(str(raw)) if raw else None
                    if result and result.get('done'):
                        report['page']=result;break
                evaluate("location.href='https://example.invalid/p011-denied'")
                if not stop.wait(.3):
                    report['url_after_navigation']=str(evaluate('location.href'))
                    bridge.close()
                    evaluate("void window.webkit.messageHandlers.p011Read.postMessage(request).then(x=>window.afterClose=JSON.parse(x).error.code)")
                    for _ in range(50):
                        if stop.wait(.05): break
                        value=evaluate('window.afterClose || null')
                        if value:
                            report['after_close']=str(value);break
            finally:
                done.set();window.destroy()
        webview.settings['ALLOW_FILE_URLS']=False
        webview.settings['ALLOW_DOWNLOADS']=False
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']=False
        try:
            webview.start(run, gui='cocoa', http_server=False, private_mode=True)
        finally:
            bridge.close();engine.dispose()
            report['closed']=True;report['runner_done']=done.is_set()
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

    page=report.get('page',{})
    assert page.get('core',{}).get('data',{}).get('body')=='native core', report
    assert page.get('subframe')=='origin_denied', report
    assert page.get('unknown')=='unknown_operation', report
    assert page.get('objectRequest')=='invalid_request', report
    assert page.get('injected') is False and page.get('imageNodes')==0, report
    assert report.get('url_after_navigation')=='about:blank', report
    assert report.get('after_close')=='origin_denied', report

if __name__=='__main__':main()
