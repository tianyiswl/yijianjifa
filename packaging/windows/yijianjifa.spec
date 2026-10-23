# Windows x64 test bundle; current shared UI, not the legacy Qt forms.
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_submodules,collect_data_files
root=Path(SPECPATH).parents[1]
resources=root/'packaging/resources'
sys.path.insert(0,str(root/'packaging'))
from recognition_bundle import collect as collect_recognition
from release_configuration import cloud_voice_data
from windows.bundle_policy import selected_browser_roots,qt_release_datas
from PySide6.QtCore import QLibraryInfo
import playwright
recognition_datas,recognition_binaries,recognition_hidden=collect_recognition(resources,platform='windows_x64')
datas=[(str(root/'src/yijianjifa/webui/assets'),'yijianjifa/webui/assets'),
       (str(root/'LICENSE'),'.'),
       (str(root/'alembic.ini'),'.'),(str(root/'alembic'),'alembic'),
       (str(root/'packaging/windows/THIRD_PARTY_NOTICES.txt'),'.'),
       (str(resources/'licenses'),'licenses')]
datas+=collect_data_files('playwright')
datas += collect_data_files('yijianjifa.features.mixing.tts')
datas += recognition_datas
datas += cloud_voice_data(root)
browser_root=Path(os.environ['PLAYWRIGHT_BROWSERS_PATH'])
browser_manifest=Path(playwright.__file__).parent/'driver/package/browsers.json'
for folder in selected_browser_roots(browser_root,browser_manifest):
    datas.append((str(folder),'playwright-browsers/'+folder.name))
binaries=recognition_binaries
for name in ('ffmpeg','ffprobe'):
    path=Path(os.environ['P011_'+name.upper()]).resolve()
    if not path.is_file():raise SystemExit('Bundled media tool missing')
    binaries.append((str(path),'bin'))
hidden=recognition_hidden
for package in ('yijianjifa.app_services','yijianjifa.features','yijianjifa.storage','yijianjifa.webui','playwright'):
    hidden+=collect_submodules(package)
hidden+=['PySide6.QtWebEngineCore','PySide6.QtWebEngineWidgets','PySide6.QtWebChannel','sqlalchemy.dialects.sqlite','keyring.backends.Windows']
a=Analysis([str(root/'packaging/windows/launcher.py')],pathex=[str(root/'src')],binaries=binaries,datas=datas,hiddenimports=hidden,
    excludes=['AppKit','WebKit','Foundation','objc','PyObjCTools','webview','pytest','PyQt5','PyQt6','imageio_ffmpeg'])
a.datas=qt_release_datas(a.datas,is_debug_build=QLibraryInfo.isDebugBuild())
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='一剪即发',console=False,upx=False,
    icon=str(resources/'icons/yijianjifa.ico'))
coll=COLLECT(exe,a.binaries,a.datas,name='一剪即发',upx=False)
