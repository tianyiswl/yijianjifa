"""Exercise the actual Qt audio scheme without cloud calls or user data."""
import json
import base64
import math
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import wave

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEngineScript
from PySide6.QtWebEngineCore import QWebEngineSettings
from yijianjifa.webui.qt_runtime import register_media_scheme, Window, Scheduler, MediaHandler
from yijianjifa.webui.media_registry import MediaRegistry
from yijianjifa.os_integration.resources import ffmpeg_path

register_media_scheme()
app = QApplication([])
window = Window(Scheduler())
window.page.smoke_mode = True
window.page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
registry = MediaRegistry()
media = SimpleNamespace(registry=registry, mime_types={})
requests = []
class ProbeHandler(MediaHandler):
    def requestStarted(self, job):
        requests.append({'method': bytes(job.requestMethod()).decode(), 'initiator': job.initiator().toString(),
                         'mime': media.mime_types.get(job.requestUrl().path().lstrip('/'))})
        super().requestStarted(job)
handler = ProbeHandler(media, window.profile, trusted_page=window.page)
window.profile.installUrlSchemeHandler(b'p011media', handler)
folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
wav = Path(folder.name) / 'tone.wav'
with wave.open(str(wav), 'wb') as f:
    f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
    f.writeframes(b''.join(struct.pack('<h', int(5000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(24000)))
mp3 = Path(folder.name) / 'tone.mp3'
subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y', '-i', str(wav),
                '-c:a', 'libmp3lame', str(mp3)], check=True, timeout=30, capture_output=True)
cases = []
for path, mime in [(wav, 'audio/wav'), (mp3, 'audio/mpeg')]:
    token = registry.grant(path)
    media.mime_types[token] = mime
    cases.append(dict(name=path.suffix[1:], url='p011media://asset/' + token, mime=mime))
    cases.append(dict(name=path.suffix[1:] + '_inline', url='data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode(), mime=mime))
script = QWebEngineScript()
script.setWorldId(42)
script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
script.setSourceCode('''window.probe=[];for(const c of ''' + json.dumps(cases) + '''){
 const a=document.createElement('audio');a.controls=true;a.muted=true;
 const r={name:c.name,support:a.canPlayType(c.mime),events:[]};window.probe.push(r);
 for(const event of ['loadedmetadata','canplay','playing','ended','error'])a.addEventListener(event,()=>{
 r.events.push(event);r.duration=Number.isFinite(a.duration)?a.duration:null;r.time=a.currentTime;
 r.error=a.error?{code:a.error.code,message:a.error.message}:null;});
 a.src=c.url;document.body.append(a);a.play().catch(e=>r.rejection={name:e.name,message:e.message});
}''')
window.page.scripts().insert(script)
window.page.setHtml('''<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; media-src p011media: data:; frame-src 'none'; base-uri 'none'; form-action 'none'"><body>Windows local audio probe</body>''', QUrl('about:blank'))
window.show()
def finish():
    def received(value):
        value = json.loads(value) if value else None
        print('AUDIO_PROBE=' + json.dumps(value, ensure_ascii=False), flush=True)
        print('AUDIO_REQUESTS=' + json.dumps(requests), flush=True)
        window.allowed_close.set()
        window.close()
        app.exit(0 if value and all('canplay' in x['events'] and 'playing' in x['events'] and not x.get('error') for x in value) else 1)
    window.page.runJavaScript('JSON.stringify(window.probe)', 42, received)
QTimer.singleShot(15000, finish)
code = app.exec()
registry.close()
raise SystemExit(code)
