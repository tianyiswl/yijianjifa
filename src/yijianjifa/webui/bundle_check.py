"""Explicit developer-only frozen resource check, without opening a GUI or account."""
import json
import sys
from pathlib import Path
import subprocess
import tempfile
import hashlib
import importlib.metadata
from sqlalchemy import inspect
from yijianjifa.storage.db import create_engine_for,migrate_to_head
from yijianjifa.os_integration.resources import ffmpeg_path,ffprobe_path,whisper_cli_path,recognition_lock_path,resource_path
from .app_bundle import application_script


def check_mixing(engine, paths):
    """Render and diagnose only synthetic files in the explicit check database."""
    from yijianjifa.features.library.media_storage import MediaStorageService
    from yijianjifa.features.mixing.desktop_service import DesktopMixService
    from yijianjifa.features.mixing.ffmpeg_renderer import _probe

    source = paths.data_dir/'中文素材，U型视频.mp4'
    subprocess.run([str(ffmpeg_path()), '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=64x64:r=25:d=0.4', '-pix_fmt', 'yuv420p', str(source)],
                   check=True, capture_output=True, timeout=30)
    library = MediaStorageService(engine, paths, kind='video')
    library.import_media(source, keep_copy=True, category_ids=[])
    mixing = DesktopMixService(engine, paths)
    task = mixing.submit(quantity=1, use_copy=False, total_ms=400, segment_ms=400,
                         keep_original_audio=False)
    mixing.queue.run_once_until_idle()
    if mixing.queue.get(task.id).success_count != 1:
        raise RuntimeError('Bundled Chinese-source mixing did not complete')
    output = next((paths.data_dir/'成品'/task.id).glob('*.mp4'))
    if abs(float(_probe(output)['format']['duration']) - 0.4) > 0.08:
        raise RuntimeError('Bundled mixing output duration mismatch')
    broken = paths.data_dir/'损坏的中文测试素材.mp4'
    broken.write_bytes(b'explicit invalid developer fixture')
    library.import_media(broken, keep_copy=False, category_ids=[])
    failure = mixing.submit(quantity=1, use_copy=False, total_ms=400, segment_ms=400)
    mixing.queue.run_once_until_idle()
    item = mixing.item_results(failure.id)[0]
    diagnostic = item.get('diagnostic', {})
    if (diagnostic.get('material') != broken.name or diagnostic.get('stage') != '读取素材视频信息'
            or not diagnostic.get('reason') or diagnostic.get('error_type') != 'CalledProcessError'
            or [e['step'] for e in item.get('execution_log', [])] != ['mix.started', 'mix.failed']):
        raise RuntimeError('Bundled mixing failure attribution or saved steps missing')
    return dict(chinese_source=True, rendered=True, failure_material=broken.name,
                failure_stage=diagnostic['stage'], saved_execution_log=True)


def check_browsers(profiles_root):
    """Exercise the full Chromium used by both publishing modes, offline."""
    from playwright.sync_api import sync_playwright
    from .chromium_publish import browser_options

    # The packaged browser path must be set before Playwright starts its driver.
    browser_options('background')
    profiles_root = Path(profiles_root).resolve()
    profiles_root.mkdir(parents=True, exist_ok=True)
    result = {'channel': 'chromium'}
    with sync_playwright() as driver:
        for mode in ('background', 'visible'):
            context = driver.chromium.launch_persistent_context(
                str(profiles_root / mode), **browser_options(mode))
            try:
                context.route('**/*', lambda route: route.abort())
                page = context.new_page()
                page.set_content('<!doctype html><h1>P011 package browser check</h1>')
                if page.evaluate("document.querySelector('h1')?.textContent === 'P011 package browser check'") is not True:
                    raise RuntimeError('Bundled browser page JavaScript check failed')
                result[mode] = True
            finally:
                context.close()
    return result


def check_recognition(models_root):
    """Load bundled CPU components without fetching or instantiating default models."""
    tool=whisper_cli_path()
    if not tool.is_file(): raise RuntimeError('Bundled whisper-cli is missing')
    lock=json.loads(recognition_lock_path().read_text(encoding='utf-8'))
    inventory=json.loads(resource_path('recognition-inventory.json').read_text(encoding='utf-8'))
    platform='windows_x64' if sys.platform=='win32' else 'macos_arm64'
    if inventory['platform']!=platform or inventory['models_bundled'] is not False:
        raise RuntimeError('Recognition inventory policy mismatch')
    selected=([lock['whisper'][platform]] if platform=='macos_arm64' else lock['whisper'][platform]['candidate_engine_files'])
    locked={row.get('name',row.get('filename')):row for row in selected}
    required=set(locked)
    if platform=='windows_x64': required.update(('msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll','vcomp140.dll'))
    if {row['name'] for row in inventory['engine_files']}!=required:
        raise RuntimeError('Bundled recognition engine dependencies missing')
    files=[]
    for row in inventory['engine_files']:
        path=resource_path(Path('bin')/row['name'])
        if not path.is_file(): raise RuntimeError('Bundled recognition engine dependency missing')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stat().st_size!=row['size_bytes'] or digest!=row['sha256']:
            raise RuntimeError('Bundled recognition engine identity mismatch')
        if row['name'] in locked and row.get('source_sha256',row['sha256'])!=locked[row['name']]['sha256']:
            raise RuntimeError('Recognition source identity does not match lock')
        files.append({'name':row['name'],'sha256':digest,'size_bytes':path.stat().st_size})
    try:
        result=subprocess.run([str(tool),'--help'],stdin=subprocess.DEVNULL,capture_output=True,timeout=30,
            creationflags=0x08000000 if sys.platform=='win32' else 0)
        if result.returncode!=0: raise RuntimeError('Bundled whisper-cli load failed')
    except (OSError,subprocess.TimeoutExpired): raise RuntimeError('Bundled whisper-cli load failed') from None
    packages=dict(lock['packages'])
    source=lock['platforms'][platform].get('source_only_dependency')
    if source:
        name='antlr4-python3-runtime' if source['name']=='antlr4-python3-runtime-built-wheel' else source['name']
        packages[name]=source['version']
        if inventory['packages'].get(name)!=source['version']:
            raise RuntimeError('Bundled recognition dependency inventory does not match lock')
    for name,version in packages.items():
        try: actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise RuntimeError('Bundled recognition package missing: '+name) from None
        if actual!=version: raise RuntimeError('Bundled recognition package version mismatch: '+name)
    import rapidocr
    import onnxruntime
    if 'CPUExecutionProvider' not in onnxruntime.get_available_providers():
        raise RuntimeError('ONNX Runtime CPU provider missing')
    if getattr(sys,'frozen',False) and list((Path(rapidocr.__file__).parent/'models').glob('*.onnx')):
        raise RuntimeError('Implicit OCR model weights included in frozen bundle')
    from yijianjifa.features.downloads.models import ModelStore
    models=ModelStore(Path(models_root)).list()
    if any(row['state']!='missing' for row in models): raise RuntimeError('Bundle check needs an empty isolated model directory')
    return {'whisper_version':lock['whisper']['version'],'engine_files':files,'packages':packages,
        'onnx_cpu_provider_available':True,'models_bundled':False,'model_states':{row['id']:row['state'] for row in models},
        'model_download':'explicit-only','recognition_inference':'not run; requires explicit models and separate acceptance'}


def check(output):
    windows=sys.platform=='win32'
    if windows:
        from PySide6.QtCore import QLibraryInfo
        from .qt_runtime import Channel,MediaHandler
        report={'kind':'windows-test-bundle-check','release_ready':False}
        if QLibraryInfo.isDebugBuild():
            raise RuntimeError('Windows package requires a release Qt runtime')
        report['qt_release_runtime'] = True
    else:
        from webview.platforms import cocoa
        from .cocoa_read_bridge import CocoaAppBridge
        from .cocoa_media import CocoaMediaTransport
        report={'kind':'mac-development-bundle-check','release_ready':False}
    script=application_script()
    if 'P011MixPublish' not in script or 'P011Views.commerce' not in script:raise RuntimeError('Web UI assets missing')
    report['webui_assets']=True
    from yijianjifa.features.mixing.tts.volcengine import MANIFEST
    if not MANIFEST:raise RuntimeError('Voice manifest missing')
    report['voice_catalog_count']=len(MANIFEST)
    from yijianjifa.features.accounts.capability_probe import fixture_path,FIXTURE_SHA256
    fixture=fixture_path()
    report['capability_fixture']={'bundled':True,'size_bytes':fixture.stat().st_size,'sha256':FIXTURE_SHA256}
    with tempfile.TemporaryDirectory(prefix='p011-bundle-check-') as folder:
        root=Path(folder);engine=create_engine_for(root/'app.db')
        try:
            migrate_to_head(engine);migrate_to_head(engine)
            if not {'mix_tasks','finished_videos','settings'}<=set(inspect(engine).get_table_names()):raise RuntimeError('migration missing tables')
            report['database_migrations']=True
            # Exercise the packaged photo codec and bridge without Qt, accounts,
            # network access, or writes to the user's library.
            from PIL import Image
            from yijianjifa.os_integration.paths import AppPaths
            from yijianjifa.app_services.image_post_commands import ImagePostCommands
            photo_root = root.resolve()
            paths = AppPaths(photo_root, photo_root/'cache', photo_root/'logs', photo_root/'managed_media')
            for folder_path in (paths.cache_dir, paths.logs_dir, paths.managed_media_dir):
                folder_path.mkdir(parents=True, exist_ok=True)
            commands = ImagePostCommands(engine, paths)
            fixtures = []
            for number, format_name in enumerate(('JPEG','PNG','WEBP','BMP')):
                path = photo_root / ('photo-'+str(number)+'.'+{'JPEG':'jpg','PNG':'png','WEBP':'webp','BMP':'bmp'}[format_name])
                Image.new('RGB',(48,64),(20+number,80,180)).save(path, format_name)
                fixtures.append(path)
            post = commands.core.import_group(fixtures, title='本地图片编解码检查')
            detail = commands.dispatch({'v':1,'request_id':'frozen_image_check','operation':'image_posts.get','params':{'id':post.id}})
            if not detail['ok'] or detail['data']['image_count'] != 4 or any(not item['thumbnail_url'].startswith('data:image/jpeg;base64,') for item in detail['data']['images']):
                raise RuntimeError('Bundled photo import/thumbnail bridge failed')
            if 'P011ImagePostsView' not in script or 'P011ImagePublishView' not in script:
                raise RuntimeError('Bundled image-post views missing')
            report['image_posts']={'formats':['JPEG','PNG','WEBP','BMP'],'owned_images':4,'bridge_thumbnails':True,
                'client_views':True,'platform_acceptance_tested':False}
            report['mixing'] = check_mixing(engine, paths)
        finally:engine.dispose()
        report['recognition']=check_recognition(root/'models')
        if windows:
            report['playwright_browsers'] = check_browsers(root/'browser-check')
            browser_root = resource_path('playwright-browsers')
            if list(browser_root.glob('chromium_headless_shell-*')):
                raise RuntimeError('Unused Chromium headless shell remains in Windows bundle')
            report['playwright_browsers']['headless_shell_bundled'] = False
        ffmpeg,ffprobe=ffmpeg_path(),ffprobe_path()
        for name,path in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]:
            result=subprocess.run([str(path),'-version'],check=True,capture_output=True,text=True,timeout=30)
            report[name]=result.stdout.splitlines()[0]
        filters=subprocess.run([str(ffmpeg),'-hide_banner','-filters'],check=True,capture_output=True,text=True,timeout=30)
        if 'subtitles' not in filters.stdout:raise RuntimeError('FFmpeg subtitles filter missing')
        video=root/'test.mp4'
        subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i','color=c=blue:s=64x64:r=10:d=0.2','-pix_fmt','yuv420p',str(video)],check=True,capture_output=True,timeout=30)
        info=json.loads(subprocess.run([str(ffprobe),'-v','error','-show_streams','-of','json',str(video)],check=True,capture_output=True,text=True,timeout=30).stdout)
        if not any(s.get('codec_type')=='video' for s in info['streams']):raise RuntimeError('media probe failed')
        subprocess.run([str(ffmpeg),'-v','error','-i',str(fixture),'-f','null','-'],check=True,capture_output=True,timeout=30)
        report['capability_fixture']['full_decode']=True
        report['media_roundtrip']=True
    report['qt_imports' if windows else 'cocoa_imports']=True
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
