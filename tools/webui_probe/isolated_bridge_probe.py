"""Native security fixture: privileged script world versus hostile page script."""
import argparse
import json
import tempfile
import threading
from pathlib import Path

import webview
from PyObjCTools import AppHelper
from webview.platforms.cocoa import BrowserView
from yijianjifa.app_services.library_queries import LibraryCommands
from yijianjifa.features.library.copy_service import CategoryRepository
from yijianjifa.storage.db import create_engine_for, migrate_to_head
from yijianjifa.webui import cocoa_read_bridge as module


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    cls=getattr(module,'CocoaAppBridge',None)
    assert cls is not None, 'isolated application bridge is not implemented'
    report={};stop=threading.Event();done=threading.Event()
    trusted='''(async()=>{try {
const request=JSON.stringify({v:1,request_id:'isolated-create',operation:'category.create',params:{kind:'copy',name:'可信分类'}});
const first=JSON.parse(await window.webkit.messageHandlers.p011App.postMessage(request));
const second=JSON.parse(await window.webkit.messageHandlers.p011App.postMessage(request));
window.privateResult={ok:first.ok,replayed:first.data.id===second.data.id};
document.querySelector('#trusted').textContent=JSON.stringify(window.privateResult);
}catch(e){document.querySelector('#trusted').textContent=JSON.stringify({error:String(e)});}})();'''
    html='''<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-src about:"><h1>T03 脚本隔离测试</h1><pre id="trusted"></pre><script>
window.attack={app:typeof window.webkit.messageHandlers.p011App,read:typeof window.webkit.messageHandlers.p011Read};
const f=document.createElement('iframe');document.body.appendChild(f);
const s=f.contentDocument.createElement('script');s.textContent="parent.attack.childAlias=typeof parent.webkit.messageHandlers.p011App";f.contentDocument.body.appendChild(s);
</script>'''
    with tempfile.TemporaryDirectory(prefix='p011-isolated-') as folder:
        engine=create_engine_for(Path(folder)/'fixture.db');migrate_to_head(engine)
        bridge=cls(LibraryCommands(engine),trusted)
        window=webview.create_window('P011 T03 isolated bridge',html=html)
        window.events.before_show += lambda:bridge.attach(window)
        def closing():
            stop.set()
            if not done.is_set():return False
            bridge.close()
        window.events.closing += closing
        def evaluate(script):
            ready=threading.Event();box={}
            def callback(value,error):box.update(value=value,error=str(error) if error else None);ready.set()
            AppHelper.callAfter(lambda:BrowserView.instances[window.uid].webview.evaluateJavaScript_completionHandler_(script,callback))
            for _ in range(100):
                if ready.wait(.05):
                    if box['error']:raise RuntimeError(box['error'])
                    return box['value']
                if stop.is_set():return None
            raise RuntimeError('evaluation timeout')
        def run():
            try:
                for _ in range(200):
                    if stop.wait(.05):return
                    if window.events.loaded.is_set():break
                for _ in range(100):
                    if stop.wait(.05):return
                    raw=evaluate("document.querySelector('#trusted').textContent")
                    if raw:report['trusted']=json.loads(str(raw));break
                report['page']=json.loads(str(evaluate("JSON.stringify({...window.attack,privateGlobal:typeof window.privateResult})")))
                report['categories']=[c.name for c in CategoryRepository(engine).list('copy')]
            finally:
                done.set();window.destroy()
        webview.settings['ALLOW_FILE_URLS']=False;webview.settings['ALLOW_DOWNLOADS']=False;webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']=False
        try:webview.start(run,gui='cocoa',http_server=False,private_mode=True)
        finally:
            bridge.close();engine.dispose()
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    assert report['trusted']=={'ok':True,'replayed':True},report
    assert report['page']=={'app':'undefined','read':'undefined','childAlias':'undefined','privateGlobal':'undefined'},report
    assert report['categories']==['可信分类'],report

if __name__=='__main__':main()
