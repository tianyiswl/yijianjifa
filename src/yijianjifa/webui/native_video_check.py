"""Validate native Qt video/audio decoding from an already-granted file handle."""
import json,subprocess,tempfile
from pathlib import Path
from PySide6.QtCore import QTimer,QFile,QIODevice,QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QMediaPlayer
from yijianjifa.webui.qt_runtime import Window,Scheduler,register_media_scheme
from yijianjifa.app_services.media_access import MediaAccess
from types import SimpleNamespace
import threading

def check(output,ffmpeg):
    tmp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True);path=Path(tmp.name)/'video.mp4'
    subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=20','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(path)],check=True,timeout=30)
    register_media_scheme();app=QApplication([]);window=Window(Scheduler());window.show()
    media=MediaAccess(None,None,native_video=True)
    grant=media.grant_read(path,name='video',kind='video',owner='library');token=grant['url'].rsplit('/',1)[1]
    errors=[];connected=False;widget=None;dialog=None
    def open_video():
     try:media.play_native(token,window,surface={'rect':[40,60,480,300],'clip':[40,60,480,300],'visible':True})
     except Exception as error:errors.append(type(error).__name__)
    threading.Thread(target=open_video,daemon=True).start()
    result={'frames':0,'seeked':False,'ended':False,'audio':False,'embedded':False,'resized':False,'clipped':False,'no_extra_window':False,'errors':[]}
    def position(value):
     if value>400 and not result['seeked']:result['seeked']=True;widget.seek.sliderMoved.emit(1500)
    def connect():
     nonlocal widget,dialog,connected
     if connected:return
     dialog=getattr(window,'_native_video',None)
     if dialog is None:return
     connected=True;widget=dialog.preview;widget.audio.setMuted(True)
     result['embedded']=not dialog.isWindow() and dialog.parentWidget() is window.view
     result['no_extra_window']=not any(w.isVisible() and w is not window for w in QApplication.topLevelWidgets())
     widget.player.videoSink().videoFrameChanged.connect(lambda f:result.update(frames=result['frames']+int(f.isValid())))
     widget.player.errorOccurred.connect(lambda e:result['errors'].append(widget.player.errorString()))
     widget.player.positionChanged.connect(position)
    timer=QTimer();timer.timeout.connect(connect);timer.start(20)
    def move():
     if not connected:return
     window.position_media(token,{'rect':[100,120,640,360],'clip':[100,150,640,330],'visible':True})
     result['resized']=dialog.width()==640 and dialog.x()==100 and dialog.y()==120
     result['clipped']=dialog.mask().boundingRect().y()==30 and dialog.mask().boundingRect().height()==330
    QTimer.singleShot(1200,move)
    def finish():
     if not connected:
      print('NATIVE_VIDEO_PROBE='+json.dumps({'opened':False,'errors':errors}),flush=True);window.allowed_close.set();window.close();app.exit(1);return
     result['audio']=widget.player.hasAudio();result['ended']=widget.player.mediaStatus()==QMediaPlayer.MediaStatus.EndOfMedia
     result['opened_on_gui_thread']=connected;result['errors']+=errors
     dialog.close();result['device_closed']=not dialog.stream.isOpen()
     Path(output).write_text(json.dumps(result),encoding='utf-8');print('NATIVE_VIDEO_PROBE='+json.dumps(result),flush=True);window.allowed_close.set();window.close()
     app.exit(0 if result['frames']>0 and all(result[k] for k in ('seeked','ended','audio','device_closed','embedded','resized','clipped','no_extra_window')) and not result['errors'] else 1)
    QTimer.singleShot(7000,finish)
    code=app.exec();media.registry.close();return code
