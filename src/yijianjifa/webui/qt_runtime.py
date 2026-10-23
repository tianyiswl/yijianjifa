"""Windows bundled interface: private-world channel, no remote app navigation."""
import json
import threading
from types import SimpleNamespace
from PySide6.QtCore import QObject,Signal,Slot,QUrl,QFile,QIODevice,QBuffer,QByteArray,QEvent
from PySide6.QtWidgets import QApplication,QMainWindow,QFileDialog,QWidget
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage,QWebEngineProfile,QWebEngineScript,QWebEngineUrlScheme,QWebEngineUrlSchemeHandler
from PySide6.QtWebEngineWidgets import QWebEngineView
from yijianjifa.app_services.dispatch_worker import DispatchWorker

WORLD=42
class Scheduler(QObject):
    requested=Signal(object)
    def __init__(self):
        super().__init__();self.requested.connect(lambda callback:callback())
    def call(self,callback):self.requested.emit(callback)

class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self,level,message,line,source):
        if getattr(self,'smoke_mode',False):
            print('Offline UI:',str(message)[:180],line,flush=True)
    def acceptNavigationRequest(self,url,kind,is_main):
        if is_main and not getattr(self,'initial_loaded',False) and url.scheme()=='data':
            self.initial_loaded=True;return True
        return url.toString()=='about:blank' and kind==QWebEnginePage.NavigationType.NavigationTypeOther
    def createWindow(self,kind):return None

class Window(QMainWindow):
    def __init__(self,scheduler):
        super().__init__();self.scheduler=scheduler;self.allowed_close=threading.Event()
        self.setWindowTitle('一剪即发 · Windows 测试版');self.resize(1440,900)
        self.view=QWebEngineView(self);self.setCentralWidget(self.view)
        self.profile=QWebEngineProfile(self) # off the record: no app-world disk state
        self.page=Page(self.profile,self.view);self.view.setPage(self.page)
        self.profile.downloadRequested.connect(lambda download:download.cancel())
    def create_file_dialog(self,kind,allow_multiple=False,file_types=(),save_filename=''):
        result=[];done=threading.Event()
        def choose():
            try:
                filters=';;'.join(item.replace(';',' ') for item in file_types) or '所有文件 (*)'
                if kind=='folder':value=QFileDialog.getExistingDirectory(self,'选择文件夹');selected=[value] if value else []
                elif kind=='save':value,_=QFileDialog.getSaveFileName(self,'保存文件',save_filename,filters);selected=[value] if value else []
                elif allow_multiple:selected,_=QFileDialog.getOpenFileNames(self,'选择文件','',filters)
                else:value,_=QFileDialog.getOpenFileName(self,'选择文件','',filters);selected=[value] if value else []
                result.extend(selected)
            finally:done.set()
        self.scheduler.call(choose);done.wait();return result
    def closeEvent(self,event):
        if self.allowed_close.is_set():
            if getattr(self,'_native_video',None):self._native_video.close()
            event.accept()
        else:
            event.ignore();self.request_exit()

    def play_media(self,media,token,surface=None):
        done=threading.Event();cancelled=threading.Event();errors=[]
        def show():
            if cancelled.is_set():done.set();return
            try:
                from .qt_video import NativeVideoPanel
                geometry=surface or {'rect':[0,0,self.view.width(),self.view.height()],
                    'clip':[0,0,self.view.width(),self.view.height()],'visible':True}
                previous=getattr(self,'_native_video',None)
                if previous and previous.token==token:
                    previous.place(geometry,zoom=self.view.zoomFactor());previous.preview.player.play();return
                if previous:previous.close()
                dialog=NativeVideoPanel(media,token,self.view);self._native_video=dialog
                def cleared():
                    if getattr(self,'_native_video',None) is dialog:self._native_video=None
                dialog.destroyed.connect(cleared)
                dialog.place(geometry,zoom=self.view.zoomFactor());dialog.preview.player.play()
            except Exception:errors.append(True)
            finally:done.set()
        self.scheduler.call(show)
        if not done.wait(10):cancelled.set();raise RuntimeError('video window unavailable')
        if errors:raise ValueError('video unavailable')

    def position_media(self,token,surface):
        def position():
            panel=getattr(self,'_native_video',None)
            if panel and panel.token==token:panel.place(surface,zoom=self.view.zoomFactor())
        self.scheduler.call(position)

    def expand_media(self,token):
        def expand():
            panel=getattr(self,'_native_video',None)
            if panel and panel.token==token and not panel.expanded:panel.toggle_expanded()
        self.scheduler.call(expand)

    def stop_media(self,token):
        def close():
            dialog=getattr(self,'_native_video',None)
            if dialog and dialog.token==token:dialog.close()
        self.scheduler.call(close)

class Channel(QObject):
    reply=Signal(str,str)
    def __init__(self,host,page):
        super().__init__();self.host=host;self.page=page;self.worker=DispatchWorker();self.closed=False
    @Slot(str,str)
    def request(self,key,raw):
        if self.closed or self.page.url().toString()!='about:blank':return
        def deliver(value):self.reply.emit(key,json.dumps(value,ensure_ascii=False))
        if not self.worker.submit(lambda:self.host.dispatch(raw,trusted_origin=True),deliver):
            deliver({'v':1,'request_id':None,'ok':False,'error':{'code':'busy','message':'操作正在处理中，请稍后重试。','retryable':True}})
    def close(self,wait=False):self.closed=True;self.worker.close(wait=wait)

class MediaHandler(QWebEngineUrlSchemeHandler):
    def __init__(self,media,parent,*,trusted_page):
        super().__init__(parent);self.media=media;self.trusted_page=trusted_page
    def requestStarted(self,job):
        url=job.requestUrl();token=url.path().lstrip('/')
        try:
            if not media_request_allowed(url,job.initiator(),bytes(job.requestMethod()),self.trusted_page.url()):raise ValueError()
            size=self.media.registry.size(token)
            # QFile keeps streaming and seeking inside Qt. The token registry
            # already checked the regular file and owns the lifetime grant.
            import os
            descriptor=self.media.registry.duplicate(token);stream=QFile(job)
            if not stream.open(descriptor,QIODevice.OpenModeFlag.ReadOnly,QFile.FileHandleFlag.AutoCloseHandle):
                os.close(descriptor);raise ValueError()
            job.reply(QByteArray(self.media.mime_types.get(token,'application/octet-stream').encode()),stream)
        except Exception:job.fail(job.Error.RequestDenied)

def media_request_allowed(url,initiator,method,page_url):
    # setHtml(..., about:blank) has an opaque ('null') initiator. No remote
    # navigation/iframes are allowed in this profile; auth uses another one.
    return (page_url.toString()=='about:blank' and initiator.toString()=='null'
            and url.scheme()=='p011media' and url.host()=='asset'
            and not url.userInfo() and url.port(-1)==-1
            and not url.hasQuery() and not url.hasFragment() and method==b'GET')

def register_media_scheme():
    scheme=QWebEngineUrlScheme(b'p011media');scheme.setSyntax(QWebEngineUrlScheme.Syntax.HostAndPort)
    # LocalScheme rejects the app's opaque origin before requestStarted, so
    # enforce the trusted page and initiator explicitly in MediaHandler.
    scheme.setDefaultPort(0);scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme)
    QWebEngineUrlScheme.registerScheme(scheme)

def attach(window,host,media,script):
    channel=QWebChannel(window.page);handler=Channel(host,window.page)
    channel.registerObject('host',handler);window.page.setWebChannel(channel,WORLD)
    window.channel=channel;window.handler=handler
    resources=MediaHandler(media,window.profile,trusted_page=window.page);window.profile.installUrlSchemeHandler(b'p011media',resources);window.resources=resources
    source=QFile(':/qtwebchannel/qwebchannel.js')
    if not source.open(QIODevice.OpenModeFlag.ReadOnly):raise RuntimeError('channel resource missing')
    bootstrap=bytes(source.readAll()).decode()+'''
new QWebChannel(qt.webChannelTransport,function(channel){
 const host=channel.objects.host,pending=new Map();let serial=0;
 host.reply.connect((key,value)=>{const done=pending.get(key);if(done){pending.delete(key);done(value);}});
 window.webkit={messageHandlers:{p011App:{postMessage(raw){return new Promise(resolve=>{const key=String(++serial);pending.set(key,resolve);host.request(key,raw);});}}}};
 '''+script+'\n});'
    injection=QWebEngineScript();injection.setName('P011Application');injection.setWorldId(WORLD)
    injection.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady);injection.setRunsOnSubFrames(False)
    injection.setSourceCode(bootstrap);window.page.scripts().insert(injection)
    return handler

DIALOGS=SimpleNamespace(FileDialog=SimpleNamespace(OPEN='open',FOLDER='folder',SAVE='save'))

class DropFilter(QObject):
    def __init__(self,window,registry):super().__init__(window);self.window=window;self.registry=registry
    def eventFilter(self,watched,event):
        if event.type() not in (QEvent.Type.DragEnter,QEvent.Type.DragMove,QEvent.Type.Drop):return False
        if not isinstance(watched,QWidget) or not (watched is self.window.view or self.window.view.isAncestorOf(watched)):return False
        if self.window.page.url().toString()!='about:blank' or event.source() is not None:return False
        if not event.mimeData().hasUrls():return False
        paths=[url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths:return False
        if event.type()==QEvent.Type.Drop:
            try:
                token=self.registry.grant(paths);point=watched.mapTo(self.window.view,event.position().toPoint())
                payload=json.dumps(dict(token=token,x=point.x(),y=point.y()))
                self.window.page.runJavaScript('void window.p011Drop('+payload+')',WORLD)
            except Exception:return False
        event.acceptProposedAction();return True
