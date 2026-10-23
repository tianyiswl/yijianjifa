"""Source-only WebEngine/Qt integration; synthetic media, no account or packaging."""
import base64,json,os,subprocess,tempfile,threading
from pathlib import Path
from types import SimpleNamespace
import imageio_ffmpeg
import shiboken6
from PySide6.QtCore import QTimer,QUrl,QRect,QPoint
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QMediaPlayer
from yijianjifa.app_services.media_access import MediaAccess
from yijianjifa.webui.qt_runtime import Window,Scheduler,register_media_scheme,attach
from yijianjifa.webui.host_commands import make_host


def main(root):
 root.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory() as temporary:
  movie=Path(temporary)/'controlled.mp4'
  subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-f','lavfi','-i','testsrc2=size=480x300:rate=20','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(movie)],check=True,timeout=30)
  register_media_scheme();app=QApplication([]);window=Window(Scheduler())
  window.setWindowTitle('一剪即发 · 内嵌视频预览验证（合成素材）')
  media=MediaAccess(None,None,native_video=True)
  grant=media.grant_read(movie,name='controlled',kind='video',owner='library');token=grant['url'].rsplit('/',1)[1]
  api=SimpleNamespace(dispatch=lambda *a,**k:{'ok':False},_error=lambda code,rid:{'ok':False,'error':{'code':code,'message':code}})
  host=make_host(api=api,engine=None,window=window,webview=None,exit_started=threading.Event(),finished_files=None,finished_deletion=None,deletion_recovery_pending=False,cover_response=None,drops=None,drop_access=None,media=media,excel=None,output=None,voice_catalog=None,volc_config=None,voices=None,play_sound=None,copy_text=None,request_exit=None)
  features=(Path(__file__).resolve().parents[1]/'src/yijianjifa/webui/assets/features.js').read_text(encoding='utf-8')
  script=features+'''
 const video=document.getElementById('video');
 const api=async(operation,params)=>{const r=JSON.parse(await window.webkit.messageHandlers.p011App.postMessage(JSON.stringify({v:1,request_id:'layout-'+Math.random().toString().slice(2),operation,params})));if(!r.ok)throw Error(r.error.message);return r.data;};
 P011NativeVideoPreview(video,{native_video:true,url:''' + json.dumps(grant['url']) + '''},api,message=>window.layoutProbeError=message);
 video.play().catch(e=>window.layoutProbeError=e.message);
 '''
  bridge=attach(window,host,media,script)
  window.page.setHtml('''<!doctype html><meta charset="utf-8"><style>body{margin:0;background:#f5f7fb;color:#243349;font:20px sans-serif}h2{margin:12px 40px}#slot{position:absolute;left:40px;top:60px;width:480px;height:300px;overflow:hidden}video{position:absolute;left:0;top:0;width:100%;height:100%;border-radius:12px;background:#111827}#cover{position:fixed;inset:0;background:#fff;z-index:10}</style><h2>素材预览 · 合成测试视频</h2><div id="slot"><video id="video"></video></div>''',QUrl('about:blank'))
  window.show();result={'frames':0,'embedded':False,'visible_before':False,'position_updated':False,'clip_updated':False,'hidden_by_overlay':False,'visible_after':False,'device_closed':False,'audio':False,'ended':False,'no_extra_window':False,'errors':[]}
  panel=None;stream=None;proof_scheduled=False
  def save_proof():
   # QWidget.grab omits the native GPU video child; capture screen pixels.
   origin=window.mapToGlobal(QPoint(0,0))
   pixmap=window.screen().grabWindow(0,origin.x(),origin.y(),window.width(),window.height())
   if pixmap.isNull():result['screen_color_samples']=0;return
   pixmap.save(str(root/'embedded-playing.png'))
   image=pixmap.toImage();colored=0
   for y in range(85,min(320,image.height()),15):
    for x in range(75,min(490,image.width()),15):
     color=image.pixelColor(x,y);rgb=(color.red(),color.green(),color.blue())
     colored+=int(max(rgb)-min(rgb)>60)
   result['screen_color_samples']=colored
  def connect():
   nonlocal panel,stream
   if panel is not None:return
   panel=getattr(window,'_native_video',None)
   if panel is None:return
   stream=panel.stream;panel.preview.audio.setMuted(True)
   def frame(value):
    nonlocal proof_scheduled
    result['frames']+=int(value.isValid())
    if result['frames']>=10 and not proof_scheduled:
     proof_scheduled=True;QTimer.singleShot(100,save_proof)
   panel.preview.player.videoSink().videoFrameChanged.connect(frame)
   panel.preview.player.errorOccurred.connect(lambda *_:result['errors'].append(panel.preview.player.errorString()))
   result['embedded']=panel.parentWidget() is window.view and not panel.isWindow()
  timer=QTimer();timer.timeout.connect(connect);timer.start(20)
  def initial():
   if panel is None:return
   result['visible_before']=panel.isVisible() and panel.geometry()==QRect(40,60,480,300)
   window.page.runJavaScript("void document.getElementById('video').p011NativePlay()",42)
  def expanded():
   if panel is None:return
   result['expanded_inside_app']=panel.expanded and panel.geometry()==window.view.rect() and not panel.isWindow()
   panel.expand_button.click()
   result['returned_to_preview']=not panel.expanded and panel.geometry()==QRect(40,60,480,300)
   window.page.runJavaScript("const slot=document.getElementById('slot'),v=document.getElementById('video');Object.assign(slot.style,{left:'100px',top:'150px',width:'640px',height:'330px'});Object.assign(v.style,{top:'-30px',width:'640px',height:'360px'});",42)
  def moved():
   if panel is None:return
   result['position_updated']=panel.geometry()==QRect(100,120,640,360)
   result['clip_updated']=panel.mask().boundingRect()==QRect(0,30,640,330)
   window.page.runJavaScript("const cover=document.createElement('div');cover.id='cover';document.body.append(cover);",42)
  def covered():
   if panel is not None:result['hidden_by_overlay']=panel.isHidden()
   window.page.runJavaScript("document.getElementById('cover').remove()",42)
  def restored():
   if panel is None:return
   result['visible_after']=panel.isVisible()
   result['audio']=panel.preview.player.hasAudio()
   result['ended']=panel.preview.player.mediaStatus()==QMediaPlayer.MediaStatus.EndOfMedia
   result['no_extra_window']=not any(w.isVisible() and w is not window for w in QApplication.topLevelWidgets())
   window.grab().save(str(root/'embedded-preview.png'))
   window.page.runJavaScript("document.getElementById('video').remove()",42)
  def finish():
   def received(error):
    if error:result['errors'].append(error)
    result['device_closed']=stream is not None and (not shiboken6.isValid(stream) or not stream.isOpen())
    result['passed']=result['frames']>0 and all(result.get(k) for k in ('embedded','visible_before','position_updated','clip_updated','hidden_by_overlay','visible_after','device_closed','audio','ended','no_extra_window','expanded_inside_app','returned_to_preview')) and not result['errors']
    if os.name=='nt':result['passed']=result['passed'] and result.get('screen_color_samples',0)>20
    (root/'embedded-layout-check.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('EMBEDDED_LAYOUT_PROBE='+json.dumps(result),flush=True)
    if os.environ.get('P011_LAYOUT_INLINE_PROOF')=='1' and (root/'embedded-playing.png').is_file():
     print('EMBEDDED_IMAGE_BASE64='+base64.b64encode((root/'embedded-playing.png').read_bytes()).decode(),flush=True)
    bridge.close(wait=True);media.registry.close();window.allowed_close.set();window.close();app.exit(0 if result['passed'] else 1)
   window.page.runJavaScript('window.layoutProbeError||""',42,received)
  for milliseconds,callback in [(1800,initial),(2200,expanded),(2800,moved),(3500,covered),(4400,restored),(5300,finish)]:QTimer.singleShot(milliseconds,callback)
  return app.exec()

if __name__=='__main__':
 raise SystemExit(main(Path(os.environ.get('P011_LAYOUT_REPORT_DIR','windows-media-validation'))))
