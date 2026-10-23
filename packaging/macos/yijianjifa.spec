# Approved Mac WebView architecture. Development artifact; release gates remain open.
from pathlib import Path
import os
import runpy
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

root = Path(SPECPATH).parents[1]
app_version = runpy.run_path(str(root/'src/yijianjifa/__init__.py'))['APP_VERSION']
resources = root / 'packaging/resources'
sys.path.insert(0,str(root/'packaging'))
from recognition_bundle import collect as collect_recognition
from release_configuration import cloud_voice_data
recognition_datas,recognition_binaries,recognition_hidden=collect_recognition(resources,platform='macos_arm64')
datas = [(str(root/'src/yijianjifa/webui/assets'),'yijianjifa/webui/assets'),
         (str(root/'LICENSE'),'.'),
         (str(root/'alembic.ini'),'.'),(str(root/'alembic'),'alembic'),
         (str(resources/'THIRD_PARTY_NOTICES.txt'),'.'),
         (str(resources/'licenses'),'licenses')]
datas += collect_data_files('webview')
datas += collect_data_files('playwright')
datas += collect_data_files('yijianjifa.features.mixing.tts')
datas += recognition_datas
datas += cloud_voice_data(root)
# External browser .app/.framework trees are staged intact after BUNDLE.
# Analysis would reclassify Mach-O data and split/sign their containers.
binaries = recognition_binaries
for name in ('ffmpeg','ffprobe'):
    source=Path(os.environ.get('P011_'+name.upper(),str(resources/'bin'/name))).resolve()
    if not source.is_file():raise SystemExit('Missing bundled media tool: '+str(source))
    if source.name!=name:raise SystemExit('Media tool needs canonical '+name+' name; use packaging/macos/build.sh')
    binaries.append((str(source),'bin'))
hidden=recognition_hidden
for package in ('yijianjifa.app_services','yijianjifa.features','yijianjifa.storage','yijianjifa.webui'):
    hidden+=collect_submodules(package)
hidden+=collect_submodules('playwright')
hidden+=['webview.platforms.cocoa','sqlalchemy.dialects.sqlite','alembic','AppKit','WebKit','Foundation']
a=Analysis([str(root/'packaging/macos/launcher.py')],pathex=[str(root/'src')],
    binaries=binaries,datas=datas,hiddenimports=hidden,
    excludes=['PySide6','PySide2','PyQt5','PyQt6','webview.platforms.qt','webview.platforms.gtk','webview.platforms.winforms','pytest'])
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='一剪即发',console=False,upx=False)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,name='一剪即发',upx=False)
app=BUNDLE(coll,name='一剪即发.app',bundle_identifier='com.yijianjifa.desktop',
    icon=str(resources/'icons/yijianjifa.icns'),
    info_plist={'NSHighResolutionCapable':True,'CFBundleShortVersionString':app_version,'CFBundleVersion':app_version})
