"""Windows test entry using the current shared interface and command service."""
import argparse
import sqlite3
import threading
from pathlib import Path


def main():
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication,QMessageBox
    from yijianjifa.os_integration.paths import AppPaths,get_app_paths
    from .qt_runtime import register_media_scheme
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path);parser.add_argument('--smoke-check',type=Path);args=parser.parse_args()
    register_media_scheme();app=QApplication([])
    if args.data_dir:
        data=args.data_dir.resolve();paths=AppPaths(data,data/'cache',data/'logs',data/'managed_media')
        for p in (paths.data_dir,paths.cache_dir,paths.logs_dir,paths.managed_media_dir):p.mkdir(parents=True,exist_ok=True)
    else:paths=get_app_paths()
    lock=QLockFile(str(paths.data_dir/'.windows-instance.lock'));lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None,'一剪即发已经运行','请使用已打开的窗口。');return 0
    try:return run(paths,app,smoke_check=args.smoke_check,host_lock_held=True)
    except Exception:
        QMessageBox.critical(None,'一剪即发未能运行','请检查应用完整性、数据目录权限和磁盘空间。请勿删除原有资料。');return 1
    finally:lock.unlock()


def run(paths,app,*,smoke_check=None,host_lock_held=False):
    from PySide6.QtGui import QIcon
    app.setApplicationName('一剪即发')
    app.setWindowIcon(QIcon(str(Path(__file__).parent/'assets/app-icon.png')))
    from .qt_runtime import Window,Scheduler,DIALOGS,attach,DropFilter
    from yijianjifa.storage.db import create_engine_for,migrate_to_head
    from yijianjifa.app_services.desktop_api import DesktopApi
    from .windows_provider import make_windows_provider
    from yijianjifa.app_services.media_access import MediaAccess,DropAccess
    from yijianjifa.app_services.excel_access import ExcelAccess
    from yijianjifa.app_services.finished_files import FinishedFiles
    from yijianjifa.app_services.finished_delete import FinishedDeletion
    from yijianjifa.app_services.voice_access import VoiceAccess
    from yijianjifa.app_services.voice_catalog import VoiceCatalog
    from yijianjifa.app_services.output_access import OutputAccess
    from yijianjifa.features.mixing.tts.voice_services import build_voice_services
    from yijianjifa.app_services.notification_sound import NotificationSoundPump
    from .media_registry import DropRegistry
    from .host_commands import make_host
    from .app_bundle import application_script
    db=paths.data_dir/'app.db'
    if db.exists():
        from datetime import datetime
        backup=paths.data_dir/'backups'/('before-windows-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
        backup.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(db) as source,sqlite3.connect(backup) as target:source.backup(target)
    engine=create_engine_for(db);migrate_to_head(engine)
    provider=make_windows_provider(engine,paths)
    api=DesktopApi(engine,paths,login_provider=provider,
        login_providers=provider.login_providers,host_lock_held=host_lock_held)
    media=MediaAccess(engine,paths,native_video=True);api.configure_downloads(media);excel=ExcelAccess(engine);finished_files=FinishedFiles(engine)
    finished_deletion=FinishedDeletion(engine);deletion_recovery_pending=finished_deletion.recover()
    volc_config,tts,cloud_voice=build_voice_services(paths);api._mix.tts=tts
    voices=VoiceAccess(engine,paths,media,adapter=tts);voice_catalog=VoiceCatalog(paths,media,volc_config)
    output=OutputAccess(engine,paths);drops=DropRegistry();drop_access=DropAccess(drops,media,excel)
    scheduler=Scheduler();window=Window(scheduler);exit_started=threading.Event();cover_grant=None
    def cover_response(data,path,token=None):
        nonlocal cover_grant
        result=dict(data)
        if token:result['token']=token
        if path:
            granted=media.registry.grant(path);media.mime_types[granted]='image/png'
            if cover_grant:media.registry.revoke(cover_grant);media.mime_types.pop(cover_grant,None)
            cover_grant=granted;result['url']='p011media://asset/'+granted
        return result
    def copy_text(content):scheduler.call(lambda:app.clipboard().setText(content))
    def play_sound():
        if api._mix.settings.notification_sound_enabled:scheduler.call(app.beep)
    sound_pump=NotificationSoundPump(engine,api._mix.settings,play_sound)
    from .exit_controller import ExitController
    def finish_exit():
        window.allowed_close.set();scheduler.call(window.close)
    exit_controller=ExitController(exit_started,lambda:api.close(0),
        lambda:bridge.close(wait=True),lambda:api.close(.1),finish_exit)
    request_exit=exit_controller.request
    window.request_exit=request_exit
    host=make_host(api=api,engine=engine,window=window,webview=DIALOGS,exit_started=exit_started,
        finished_files=finished_files,finished_deletion=finished_deletion,deletion_recovery_pending=deletion_recovery_pending,
        cover_response=cover_response,drops=drops,drop_access=drop_access,media=media,excel=excel,output=output,
        voice_catalog=voice_catalog,volc_config=volc_config,voices=voices,play_sound=play_sound,cloud_voice=cloud_voice,
        copy_text=copy_text,request_exit=request_exit)
    window.page.smoke_mode=bool(smoke_check)
    bridge=attach(window,host,media,application_script().replace('Mac 开发版','Windows 测试版'))
    drop_filter=DropFilter(window,drops);app.installEventFilter(drop_filter)
    from PySide6.QtCore import QUrl
    window.page.setHtml((Path(__file__).parent/'assets/library.html').read_text(encoding='utf-8').replace('Mac 开发版','Windows 测试版'),QUrl('about:blank'))
    window.show();api.start_local_checks();sound_pump.start()
    smoke_result={'passed':False}
    if smoke_check:
        from PySide6.QtCore import QTimer
        import json
        window.page.runJavaScript("window.p011SmokeReady=false;",42)
        def check_loaded():
            window.page.runJavaScript("Boolean(window.webkit && document.body.dataset.view === 'mixing' && document.getElementById('quick-generate'))",42,loaded)
        def loaded(value):
            if value and not smoke_result['passed']:
                # Verify the native bridge, not just HTML rendering.
                window.page.runJavaScript("window.webkit.messageHandlers.p011App.postMessage(JSON.stringify({v:1,request_id:'smoke-term',operation:'product.use_term',params:{}})).then(raw=>{window.p011SmokeReady=JSON.parse(raw).ok;});",42)
                window.page.runJavaScript('Boolean(window.p011SmokeReady)',42,checked)
        def checked(value):
            if value:window.page.runJavaScript('typeof qt === "undefined" && typeof window.webkit === "undefined"',0,isolated)
        def isolated(value):
            if value:
                smoke_result['passed']=True;timer.stop()
                smoke_check.parent.mkdir(parents=True,exist_ok=True)
                smoke_check.write_text(json.dumps({'interface':True,'native_bridge':True,'page_world_isolated':True}),encoding='utf-8')
                window.allowed_close.set();window.close()
        timer=QTimer(window);timer.timeout.connect(check_loaded);timer.start(500)
        def timeout():
            if not smoke_result['passed']:
                print('Offline UI timeout',window.page.url().toString(),flush=True)
                window.allowed_close.set();window.close()
        QTimer.singleShot(30000,timeout)
    try:
        code=app.exec()
        return code if smoke_check is None or smoke_result['passed'] else 2
    finally:
        bridge.close(wait=True)
        while not api.close(.1):pass
        while not sound_pump.close(.1):pass
        voices.close();api._covers.close();drops.close();media.close();engine.dispose()

if __name__=='__main__':raise SystemExit(main())
