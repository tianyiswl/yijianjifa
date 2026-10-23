"""Mac Web UI development entry. Existing Qt entry remains until acceptance."""
import argparse
import json
from pathlib import Path
import sqlite3
import threading

from yijianjifa.os_integration.paths import AppPaths, get_app_paths
from yijianjifa.storage.db import create_engine_for, migrate_to_head
from yijianjifa.app_services.desktop_api import DesktopApi
from .single_instance import InstanceLock, AlreadyRunning


def startup_notice(title, message):
    """Frozen Finder launches have no terminal; use a native, path-free alert."""
    import sys
    print(message)
    if not getattr(sys, 'frozen', False):
        return
    try:
        from AppKit import NSApplication, NSAlert
        app=NSApplication.sharedApplication()
        app.activateIgnoringOtherApps_(True)
        alert=NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_('知道了')
        alert.runModal()
    except Exception:
        # Never expose exception paths/session data through a startup fallback.
        print('无法显示系统提示：'+message, file=sys.stderr)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path);parser.add_argument('--authorized-douyin-uid');args=parser.parse_args()
    preparing_paths=True
    try:
        if args.data_dir:
            data=args.data_dir.resolve();paths=AppPaths(data,data/'cache',data/'logs',data/'managed_media')
            for path in (paths.data_dir,paths.cache_dir,paths.logs_dir,paths.managed_media_dir):path.mkdir(parents=True,exist_ok=True)
        else:paths=get_app_paths()
        with InstanceLock(paths.data_dir):
            preparing_paths=False
            run(paths,authorized_douyin_uid=args.authorized_douyin_uid,host_lock_held=True)
    except AlreadyRunning:
        startup_notice('一剪即发已经运行','一剪即发已经运行，请使用已打开的窗口。')
        return 0
    except Exception:
        message=('无法使用数据目录，请检查目录位置和读写权限后重新打开。' if preparing_paths else
                 '程序未能正常运行，请检查数据目录权限、可用磁盘空间和应用是否完整，再重新打开。请勿删除原有资料。')
        startup_notice('一剪即发未能启动或运行',message)
        return 1
    return 0


def run(paths, *, authorized_douyin_uid=None, host_lock_held=False, cloud_voice_endpoint=None, local_voice_test=False):
    if local_voice_test:
        from yijianjifa.app_services.cloud_voice_test import require_test_client, validate_test_endpoint
        validate_test_endpoint(cloud_voice_endpoint)
        require_test_client(paths.data_dir)
    db=paths.data_dir/'app.db'
    if db.exists():
        import datetime
        backup=paths.data_dir/'backups'/('before-webui-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
        backup.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(db) as source,sqlite3.connect(backup) as target:source.backup(target)
    engine=create_engine_for(db);migrate_to_head(engine)
    from .douyin_auth import make_douyin_provider
    provider=make_douyin_provider(engine,paths,expected_uid=authorized_douyin_uid)
    from .creator_providers import make_creator_login_providers
    api=DesktopApi(engine,paths,login_provider=provider,
        login_providers=make_creator_login_providers(engine,paths),host_lock_held=host_lock_held)
    import webview
    from PyObjCTools import AppHelper
    from webview.platforms.cocoa import BrowserView
    from .cocoa_read_bridge import CocoaAppBridge
    from yijianjifa.app_services.media_access import MediaAccess, DropAccess
    from .cocoa_media import CocoaMediaTransport
    from yijianjifa.app_services.excel_access import ExcelAccess
    excel=ExcelAccess(engine)
    media=MediaAccess(engine,paths);api.configure_downloads(media)
    cover_grant=None
    def cover_response(data,path,token=None):
        nonlocal cover_grant
        result=dict(data)
        if token:result['token']=token
        if path:
            granted=media.registry.grant(path)
            media.mime_types[granted]='image/png'
            if cover_grant:
                media.registry.revoke(cover_grant);media.mime_types.pop(cover_grant,None)
            cover_grant=granted
            result['url']='p011media://asset/'+granted
        return result
    from yijianjifa.app_services.finished_files import FinishedFiles
    finished_files=FinishedFiles(engine)
    from yijianjifa.app_services.finished_delete import FinishedDeletion
    finished_deletion=FinishedDeletion(engine)
    deletion_recovery_pending=finished_deletion.recover()
    from yijianjifa.app_services.voice_access import VoiceAccess
    from yijianjifa.features.mixing.tts.voice_services import build_voice_services
    volc_config,tts,cloud_voice=build_voice_services(paths,endpoint=cloud_voice_endpoint,local_test=local_voice_test)
    api._mix.tts=tts
    voices=VoiceAccess(engine,paths,media,adapter=tts)
    from yijianjifa.app_services.voice_catalog import VoiceCatalog
    voice_catalog=VoiceCatalog(paths,media,volc_config)
    from yijianjifa.app_services.output_access import OutputAccess
    output=OutputAccess(engine,paths)
    from yijianjifa.app_services.notification_sound import NotificationSoundPump
    from AppKit import NSSound
    def play_sound():
        def play():
            if api._mix.settings.notification_sound_enabled:
                sound=NSSound.soundNamed_('Ping')
                if sound:sound.play()
        AppHelper.callAfter(play)
    sound_pump=NotificationSoundPump(engine,api._mix.settings,play_sound)
    transport=CocoaMediaTransport(media)
    from .media_registry import DropRegistry
    from .cocoa_drop import CocoaDropTransport
    drops=DropRegistry();drop_transport=CocoaDropTransport(drops)
    drop_access=DropAccess(drops,media,excel)
    assets=Path(__file__).parent/'assets'
    import AppKit
    screen=AppKit.NSScreen.mainScreen()
    available=screen.visibleFrame().size if screen is not None else None
    width=min(1440,max(800,int(available.width)-32)) if available else 1440
    height=min(900,max(600,int(available.height)-32)) if available else 900
    title='一剪即发 · 本地云配音测试（数据隔离）' if local_voice_test else '一剪即发 · Mac 开发版'
    window=webview.create_window(title,html=(assets/'library.html').read_text(),width=width,height=height,min_size=(min(1100,width),min(700,height)))
    allowed_close=threading.Event();exit_started=threading.Event()

    from .host_commands import make_host
    def copy_text(content):
        from AppKit import NSPasteboard,NSPasteboardTypeString
        board=NSPasteboard.generalPasteboard();board.clearContents()
        if not board.setString_forType_(content,NSPasteboardTypeString):raise RuntimeError('clipboard write failed')
    from .exit_controller import ExitController
    def finish_exit():
        allowed_close.set();AppHelper.callAfter(window.destroy)
    exit_controller=ExitController(exit_started,lambda:api.close(0),
        lambda:bridge.close(wait=True),lambda:api.close(.1),finish_exit)
    request_exit=exit_controller.request
    host=make_host(api=api,engine=engine,window=window,webview=webview,exit_started=exit_started,
        finished_files=finished_files,finished_deletion=finished_deletion,deletion_recovery_pending=deletion_recovery_pending,
        cover_response=cover_response,drops=drops,drop_access=drop_access,media=media,excel=excel,output=output,
        voice_catalog=voice_catalog,volc_config=volc_config,voices=voices,play_sound=play_sound,cloud_voice=cloud_voice,
        copy_text=copy_text,request_exit=request_exit)

    from .app_bundle import application_script
    bridge=CocoaAppBridge(host,application_script())
    window.events.before_show += lambda:(transport.bind(window),bridge.attach(window),drop_transport.bind(window,bridge))
    def closing():
        if allowed_close.is_set():bridge.close();return True
        request_exit()
        return False
    window.events.closing += closing
    webview.settings['ALLOW_FILE_URLS']=False
    webview.settings['ALLOW_DOWNLOADS']=False
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']=False
    try:
        api.start_local_checks()
        sound_pump.start()
        webview.start(gui='cocoa',http_server=False,private_mode=True)
    finally:
        bridge.close(wait=True)
        while not api.close(.1):pass
        while not sound_pump.close(.1):pass
        voices.close()
        api._covers.close()
        drop_transport.close()
        transport.close()
        engine.dispose()

if __name__=='__main__':
    raise SystemExit(main())
