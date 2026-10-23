"""Offline Qt WebEngine voice-list scroll behavior probe.

Reads the real voices.html/common.css/voices.js. A memory-only bridge supplies
synthetic voices and a silent WAV sample. All network requests are blocked.
The mouse click goes through Qt's normal event delivery, not DOM .click().
This is not a Windows-device or supplier-service acceptance test.
"""
from __future__ import annotations
import argparse, base64, hashlib, io, json, os, re, sys, wave
from pathlib import Path

# Windows uses the native platform, like the successful audio/video probes.
# The offscreen platform is only needed for this Mac's isolated virtual view.
if sys.platform == 'darwin':
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu --disable-dev-shm-usage')
from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer, QUrl, qVersion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'src/yijianjifa/webui/assets'

class Fence(QWebEngineUrlRequestInterceptor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.blocked = []
    def interceptRequest(self, info):
        scheme = info.requestUrl().scheme()
        if scheme not in ('data', 'about'):
            self.blocked.append(scheme)
            info.block(True)

class Page(QWebEnginePage):
    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self.errors = []
        self.lifecycle = []
        self.renderer_failure = None
        self.loadStarted.connect(lambda: self.lifecycle.append({'event':'loadStarted'}))
        self.loadFinished.connect(lambda ok: self.lifecycle.append({'event':'loadFinished','ok':ok}))
        self.renderProcessTerminated.connect(self.renderer_terminated)
    def renderer_terminated(self, status, exit_code):
        self.renderer_failure = {'status':status.name,'exit_code':exit_code}
        self.lifecycle.append({'event':'renderProcessTerminated',**self.renderer_failure})
    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.errors.append({'line': line, 'message': message})

def wait(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()

def load_document(page, html):
    loop = QEventLoop()
    result = {'done':False}
    def loaded(ok):
        result.update(done=True,ok=ok)
        loop.quit()
    def terminated(*_):
        loop.quit()
    page.loadFinished.connect(loaded)
    page.renderProcessTerminated.connect(terminated)
    try:
        page.setHtml(html, QUrl('about:blank'))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
    finally:
        page.loadFinished.disconnect(loaded)
        page.renderProcessTerminated.disconnect(terminated)
    if page.renderer_failure:
        raise RuntimeError('Render process terminated while loading the synthetic page')
    if not result['done']:
        raise TimeoutError('Page loadFinished timed out before catalog readback')
    if not result['ok']:
        raise RuntimeError('Synthetic page loadFinished reported failure')

def js(page, source):
    loop = QEventLoop()
    result = {'done': False}
    def receive(value):
        result.update(done=True, value=value)
        loop.quit()
    def terminated(*_):
        loop.quit()
    page.renderProcessTerminated.connect(terminated)
    try:
        page.runJavaScript('JSON.stringify('+source+')', receive)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
    finally:
        page.renderProcessTerminated.disconnect(terminated)
    if page.renderer_failure:
        raise RuntimeError('Render process terminated during JavaScript readback')
    if not result['done']:
        raise TimeoutError('JavaScript readback timed out')
    value=result.get('value')
    return json.loads(value) if isinstance(value,str) else value

def silent_wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
        stream.writeframes(bytes(1600))
    return 'data:audio/wav;base64,' + base64.b64encode(output.getvalue()).decode('ascii')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', default='observed')
    parser.add_argument('--voice-script', type=Path)
    parser.add_argument('--output', type=Path, help='Report directory; defaults to build/voice-scroll-20261022/<label>')
    parser.add_argument('--expect', choices=('observe', 'stable', 'regression'), default='observe')
    args = parser.parse_args()
    output = args.output or ROOT / 'build/voice-scroll-20261022' / args.label
    output.mkdir(parents=True, exist_ok=True)
    sources = {name: (ASSETS/name).read_text(encoding='utf-8') for name in ('voices.html','common.css','voices.js')}
    if args.voice_script:
        sources['voices.js']=args.voice_script.read_text(encoding='utf-8')
    hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name,text in sources.items()}
    # The shared CSS follows the route's style, as in app_bundle.py.
    html = sources['voices.html'].replace('</head>', '<style>'+sources['common.css']+'</style></head>')
    html = html.replace('<body>', '<body data-view="voices">')
    stub = r'''
window.P011Views={}; window.P011Router={go(){}};
window.probeCalls=[];
window.probeCatalog={configured:true,mode:'hosted',selected:'',favorites:[],items:
 Array.from({length:40},(_,i)=>({id:'synthetic:'+i,name:'合成测试音色 '+i,gender:i%2?'男声':'女声',language:'中文',style:'通用场景',available:true,standard_sample:true,sample_source:'官方试听'}))};
window.P011CloudVoice={words:t=>Array.from(t).length,mount:()=>({isBusy:()=>false,canChoose:()=>true,selectionChanged(){},refreshAfterGeneration(){},chooseVoice:fn=>fn(),dispose(){}})};
window.webkit={messageHandlers:{p011App:{postMessage:async raw=>{
 const r=JSON.parse(raw); window.probeCalls.push([r.operation,r.params]);
 if(r.operation==='voice.catalog')return JSON.stringify({ok:true,data:window.probeCatalog});
 if(r.operation==='voice.selection.save'){if(window.probeWaitFavorite)await new Promise(resolve=>window.probeReleaseFavorite=resolve);Object.assign(window.probeCatalog,r.params);return JSON.stringify({ok:true,data:window.probeCatalog});}
 if(r.operation==='voice.standard')return JSON.stringify({ok:true,data:{url:WAV_URL}});
 throw Error('Offline probe forbids operation '+r.operation);
}}}};
'''.replace('WAV_URL', json.dumps(silent_wav()))
    script = stub+'\n'+sources['voices.js']+'\nwindow.probeDispose=P011Views.voices();'
    html = html.replace('</body>', '<script>'+script+'</script></body>')
    app = QApplication([])
    view = QWebEngineView(); view.resize(1280,800)
    profile = QWebEngineProfile(view)
    fence = Fence(profile); profile.setUrlRequestInterceptor(fence)
    page = Page(profile, view); view.setPage(page)
    view.show()
    report = {'passed':False,'runtime':'Qt WebEngine', 'qt':qVersion(), 'platform':sys.platform,
              'qt_platform':app.platformName(),
              'viewport':[1280,800], 'synthetic_only':True, 'source_sha256':hashes,
              'cases':[], 'network_blocked':fence.blocked, 'errors':page.errors,
              'page_lifecycle':page.lifecycle}
    stage = 'initialization'
    case = None
    try:
        for case in ('select-row','favorite','official-sample','text-audition-focus','delayed-favorite','favorite-bottom-clamp'):
            stage = 'page-load'
            case_html=html.replace('standard_sample:true','standard_sample:false') if case=='text-audition-focus' else html
            if case=='favorite-bottom-clamp':
                case_html=case_html.replace("favorites:[]","favorites:Array.from({length:20},(_,i)=>'synthetic:'+i)")
            load_document(page, case_html)
            stage = 'catalog-ready'
            for _ in range(50):
                wait(40)
                if js(page,"document.querySelectorAll('#voiceRows>tr').length===20"):
                    break
            else: raise RuntimeError('Synthetic catalog did not render')
            stage = 'scroll-setup'
            if case=='favorite-bottom-clamp':
                checkbox=js(page,"(()=>{const r=document.getElementById('favoriteOnly').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
                QTest.mouseClick(view.focusProxy(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,QPoint(round(checkbox['x']),round(checkbox['y'])))
                wait(80)
            js(page,"(()=>{window.probeBottom="+str(case=='favorite-bottom-clamp').lower()+";window.probeWaitFavorite="+str(case=='delayed-favorite').lower()+";return true;})()")
            setup = js(page,r'''(()=>{
 const sc=document.querySelector('.voice-scroll');sc.scrollTop=window.probeBottom?sc.scrollHeight:430;sc.scrollLeft=0;
 const rect=sc.getBoundingClientRect(); const rows=[...document.querySelectorAll('#voiceRows>tr')];
 const available=rows.filter(n=>{const r=n.getBoundingClientRect();return r.top>rect.top+36&&r.bottom<rect.bottom-8;});
 const row=available.at(-1);if(!row)return {error:'No fully visible row'};
 window.probeRowIndex=rows.indexOf(row);
 return {before:sc.scrollTop,clientHeight:sc.clientHeight,scrollHeight:sc.scrollHeight,index:window.probeRowIndex};
})()''')
            if setup.get('error'): raise RuntimeError(setup['error'])
            if setup['before']<=0 or setup['scrollHeight']<=setup['clientHeight']:
                raise RuntimeError('Real overflow precondition not satisfied')
            wait(80)
            view.grab().save(str(output/(case+'-before.png')))
            action = "row" if case=='select-row' else "row.children[3].querySelector('button')" if case in ('favorite','delayed-favorite','favorite-bottom-clamp') else "row.children[2].querySelector('button')"
            point = js(page,"(()=>{const row=document.querySelectorAll('#voiceRows>tr')[window.probeRowIndex];const t="+action+";const r=t.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
            stage = 'mouse-click'
            QTest.mouseClick(view.focusProxy(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(round(point['x']),round(point['y'])))
            if case=='delayed-favorite':
                wait(100)
                setup['clickBefore']=setup['before']
                setup['before']=js(page,"(()=>{const sc=document.querySelector('.voice-scroll');sc.scrollTop=700;window.probeReleaseFavorite();return sc.scrollTop;})()")
            wait(650)
            stage = 'action-readback'
            state = js(page,r'''(()=>{const sc=document.querySelector('.voice-scroll');return {after:sc.scrollTop,afterMax:sc.scrollHeight-sc.clientHeight,rows:document.querySelectorAll('#voiceRows>tr').length,activeName:document.getElementById('activeName').textContent,focus:document.activeElement.id||document.activeElement.tagName,operations:window.probeCalls,samplePresent:!!document.querySelector('#standardPlayer audio')};})()''')
            result = {'case':case,**setup,**state}
            result['expectedAfter']=min(result['before'],result['afterMax'])
            result['stable']=abs(result['expectedAfter']-result['after'])<=1
            result['jump_to_top']=result['before']>0 and result['after']==0
            result['no_paid_synthesis']=not any(op=='voice.audition.text' for op,_ in result['operations'])
            report['cases'].append(result)
            view.grab().save(str(output/(case+'-after.png')))
        report['layout_valid']=all(c['before']>0 and c['scrollHeight']>c['clientHeight'] for c in report['cases'])
        report['all_stable']=all(c['stable'] for c in report['cases'])
        report['regression_reproduced']=any(c['jump_to_top'] for c in report['cases'])
        report['no_paid_synthesis']=all(c['no_paid_synthesis'] for c in report['cases'])
        report['all_actions_exercised']=report['cases'][0]['activeName']==f"合成测试音色 {report['cases'][0]['index']}" and len(report['cases'][1]['operations'])==2 and report['cases'][2]['samplePresent'] and report['cases'][3]['focus']=='auditionText' and report['cases'][4]['before']==700 and report['cases'][5]['rows']==19
        expected = args.expect=='observe' or (args.expect=='stable' and report['all_stable']) or (args.expect=='regression' and report['regression_reproduced'])
        report['passed']=bool(expected and report['layout_valid'] and report['no_paid_synthesis'] and report['all_actions_exercised'] and not report['errors'])
    except Exception as error:
        report['failure']={'type':type(error).__name__,'message':str(error),'case':case,'stage':stage}
        report['renderer_failure']=page.renderer_failure
    finally:
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        # Windows CI stdout may use cp1252. Keep diagnostics ASCII so printing
        # Chinese synthetic labels cannot hide the actual renderer/load failure.
        print(json.dumps(report,ensure_ascii=True),flush=True)
        page.setHtml('<html></html>'); wait(30); view.close(); page.deleteLater(); wait(40); profile.deleteLater(); wait(40)
    return 0 if report.get('passed') else 1

if __name__=='__main__':
    raise SystemExit(main())
