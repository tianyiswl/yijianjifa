"""Native Qt codec and local video transport diagnosis; synthetic media only."""
import base64,json,subprocess,tempfile,threading
from pathlib import Path
from types import SimpleNamespace
import imageio_ffmpeg
from PySide6.QtCore import QTimer,QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEngineScript,QWebEngineSettings
from yijianjifa.webui.qt_runtime import register_media_scheme,Window,Scheduler,MediaHandler
from yijianjifa.webui.media_registry import MediaRegistry
folder=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
root=Path(folder.name)
for name,codec,extra in [('h264.mp4','libx264',['-pix_fmt','yuv420p','-movflags','+faststart']),('vp9.webm','libvpx-vp9',['-b:v','180k'])]:
 subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=15','-t','2','-an','-c:v',codec,*extra,str(root/name)],check=True,timeout=30)
register_media_scheme();app=QApplication([]);window=Window(Scheduler());window.page.smoke_mode=True
window.page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture,False)
registry=MediaRegistry();media=SimpleNamespace(registry=registry,mime_types={});requests=[]
class Handler(MediaHandler):
 def requestStarted(self,job):
  requests.append({'method':bytes(job.requestMethod()).decode(),'mime':media.mime_types.get(job.requestUrl().path().lstrip('/'))})
  super().requestStarted(job)
handler=Handler(media,window.profile,trusted_page=window.page);window.profile.installUrlSchemeHandler(b'p011media',handler)
cases=[]
for filename,mime in [('h264.mp4','video/mp4'),('vp9.webm','video/webm')]:
 path=root/filename;token=registry.grant(path);media.mime_types[token]=mime
 cases += [{'name':filename+'_scheme','url':'p011media://asset/'+token,'mime':mime},{'name':filename+'_inline','url':'data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode(),'mime':mime}]
script=QWebEngineScript();script.setWorldId(42);script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
script.setSourceCode("""
window.videoProbe=[];window.codecProbe={h264:document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E"'),vp9:document.createElement('video').canPlayType('video/webm; codecs="vp9"')};
for(const c of """+json.dumps(cases)+"""){
const v=document.createElement('video');v.controls=true;v.muted=true;v.width=200;
const r={name:c.name,events:[],frames:0};window.videoProbe.push(r);
const frame=()=>{r.frames++;v.requestVideoFrameCallback(frame)};if(v.requestVideoFrameCallback)v.requestVideoFrameCallback(frame);
for(const e of ['loadedmetadata','canplay','playing','ended','error','seeked'])v.addEventListener(e,()=>{r.events.push(e);r.duration=Number.isFinite(v.duration)?v.duration:null;r.time=v.currentTime;r.error=v.error?{code:v.error.code,message:v.error.message}:null});
v.src=c.url;document.body.append(v);v.play().catch(e=>r.rejection={name:e.name,message:e.message});
}
""")
window.page.scripts().insert(script);window.page.setHtml('<!doctype html><body>Windows synthetic video diagnosis</body>',QUrl('about:blank'));window.show()
def finish():
 def received(value):
  print('VIDEO_PROBE='+str(value),flush=True);print('VIDEO_REQUESTS='+json.dumps(requests),flush=True)
  window.allowed_close.set();window.close();app.exit(0 if value else 1)
 window.page.runJavaScript('JSON.stringify({codecs:window.codecProbe,cases:window.videoProbe})',42,received)
QTimer.singleShot(12000,finish)
code=app.exec();registry.close();raise SystemExit(code)
