"""Recognition freezing boundaries; no model download or user data."""
import hashlib
import json
from pathlib import Path
import runpy
import sys
import types

import pytest

from yijianjifa.os_integration import resources
from yijianjifa.webui import bundle_check

ROOT = Path(__file__).resolve().parents[3]


def test_frozen_engine_missing_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path),raising=False)
    monkeypatch.setattr(resources.shutil,'which',lambda name: pytest.fail('recognition may not search PATH'))
    with pytest.raises(RuntimeError,match='whisper'):
        bundle_check.check_recognition(tmp_path/'empty-models')
    assert not (tmp_path/'empty-models').exists()


def test_frozen_windows_chinese_space_path_is_preserved(monkeypatch, tmp_path):
    root=tmp_path/'一剪即发 私有测试'
    monkeypatch.setattr(sys,'_MEIPASS',str(root),raising=False)
    monkeypatch.setattr(sys,'platform','win32')
    monkeypatch.setattr(resources.shutil,'which',lambda name: pytest.fail('recognition may not search PATH'))
    assert resources.whisper_cli_path()==root/'bin'/'whisper-cli.exe'


@pytest.mark.parametrize('platform',['macos','windows'])
def test_frozen_ocr_worker_dispatch_precedes_native_host(monkeypatch, platform):
    calls=[]
    child=types.ModuleType('yijianjifa.features.downloads.ocr_worker')
    child.worker_main=lambda argv: calls.append(argv) or 23
    host=types.ModuleType('yijianjifa.webui.mac_app' if platform=='macos' else 'yijianjifa.webui.windows_app')
    host.main=lambda: pytest.fail('OCR worker imported or started native GUI')
    monkeypatch.setitem(sys.modules,child.__name__,child)
    monkeypatch.setitem(sys.modules,host.__name__,host)
    monkeypatch.setattr(sys,'argv',['frozen-app','--yjjf-ocr-worker','private-payload'])
    with pytest.raises(SystemExit) as exc: runpy.run_path(str(ROOT/'packaging'/platform/'launcher.py'),run_name='__main__')
    assert exc.value.code==23 and calls==[['private-payload']]


def test_selected_recognition_licenses_are_complete_and_hash_verified():
    directory=ROOT/'packaging/resources/licenses'
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    needed={'whisper.cpp','openai-whisper','rapidocr','rapidocr-models','onnxruntime'}
    selected=[row for row in manifest['files'] if row['component'] in needed]
    assert {row['component'] for row in selected}==needed
    assert any('ThirdPartyNotices' in row['file'] for row in selected if row['component']=='onnxruntime')
    for row in selected:
        path=directory/row['file']
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
    assert all(not path.is_absolute() and '..' not in path.parts for path in map(Path,(row['file'] for row in selected)))


def test_recognition_license_collection_contains_only_locked_dependency_texts(monkeypatch,tmp_path):
    build=helper(); repository=tmp_path/'repository'; prefix=tmp_path/'isolated-python'
    directory=repository/'packaging/resources/licenses'; directory.mkdir(parents=True)
    lock_path=repository/'packaging/recognition-lock.json'
    lock_path.write_text(json.dumps({'platforms':{'fixture':{'artifacts':[{'name':'rapidocr'},{'name':'onnxruntime'}]}}}),encoding='utf-8')
    retained={'file':'recognition/fixed-LICENSE','component':'fixed','sha256':'preserved'}
    (directory/'manifest.json').write_text(json.dumps({'files':[retained]}),encoding='utf-8')
    source=prefix/'site-packages'; source.mkdir(parents=True)
    payload='Fixture license · 中文许可证\n'.encode('utf-8')
    valid=source/'LICENSE.txt'; valid.write_bytes(payload)
    outside=tmp_path/'external/LICENSE.txt'; outside.parent.mkdir(); outside.write_bytes(b'outside')
    code=source/'LICENSE.py'; code.write_bytes(b'not a license text')
    config=source/'config.yaml'; config.write_bytes(b'not a license')
    class Distribution:
        def __init__(self,name,files):self.metadata={'Name':name};self.version='fixture';self.files=list(files);self._files=files
        def locate_file(self,relative):return self._files[relative]
    distributions=[Distribution('RapidOCR',{Path('licenses/LICENSE.txt'):valid,Path('LICENSE.py'):code,Path('config.yaml'):config,
        Path('../licenses/LICENSE.txt'):valid}),Distribution('onnxruntime',{Path('LICENSE.txt'):outside}),
        Distribution('unlocked-package',{Path('LICENSE.txt'):valid})]
    monkeypatch.setattr(build,'ROOT',repository);monkeypatch.setattr(build,'LOCK',lock_path)
    monkeypatch.setattr(build.sys,'prefix',str(prefix))
    monkeypatch.setattr(build.importlib.metadata,'distributions',lambda:distributions)
    rows=build.collect_licenses()
    assert len(rows)==1 and rows[0]['component']=='rapidocr'
    collected=directory/rows[0]['file']
    assert collected.read_bytes()==payload and collected.read_text(encoding='utf-8').strip()
    assert rows[0]['sha256']==hashlib.sha256(payload).hexdigest()
    assert json.loads((directory/'manifest.json').read_text(encoding='utf-8'))['files']==[retained,*rows]
    assert outside.read_bytes()==b'outside' and valid.read_bytes()==payload


def helper():
    import importlib.util
    spec=importlib.util.spec_from_file_location('p011_recognition_bundle',ROOT/'packaging/recognition_bundle.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_inventory_cannot_reauthorize_a_replaced_engine(monkeypatch,tmp_path):
    build=helper(); source=tmp_path/'source'; output=tmp_path/'bundle'; source.mkdir()
    original=b'approved CPU artifact'; (source/'whisper-cli').write_bytes(original)
    digest=hashlib.sha256(original).hexdigest()
    lock={'whisper':{'version':'fixed','source_commit':'a'*40,'macos_arm64':{'filename':'whisper-cli','size_bytes':len(original),'sha256':digest}}}
    lock_path=tmp_path/'lock.json'; lock_path.write_text(json.dumps(lock), encoding='utf-8')
    monkeypatch.setattr(build,'_versions',lambda *a: {'rapidocr':'3.9.2','onnxruntime':'1.22.1'})
    monkeypatch.setattr(build,'_licenses',lambda *a: [])
    inventory=build.prepare(source,output,platform='macos_arm64',lock_path=lock_path)
    replacement=b'unapproved CPU artifact'; (output/'bin/whisper-cli').write_bytes(replacement)
    inventory['engine_files'][0].update(size_bytes=len(replacement),sha256=hashlib.sha256(replacement).hexdigest())
    (output/'recognition-inventory.json').write_text(json.dumps(inventory), encoding='utf-8')
    with pytest.raises(RuntimeError,match='identity|lock'):
        build.check(output,platform='macos_arm64',lock_path=lock_path)


def test_prepared_source_recognition_check_never_uses_default_models(monkeypatch,tmp_path):
    import socket
    import urllib.request
    tool=resources.whisper_cli_path()
    if not tool.is_file(): pytest.skip('explicitly prepared native tool required; not frozen acceptance')
    rapidocr=pytest.importorskip('rapidocr'); pytest.importorskip('onnxruntime')
    def blocked(*args,**kwargs): pytest.fail('bundle check attempted network or implicit model construction')
    monkeypatch.setattr(socket.socket,'connect',blocked)
    monkeypatch.setattr(urllib.request,'urlopen',blocked)
    monkeypatch.setattr(rapidocr,'RapidOCR',blocked)
    report=bundle_check.check_recognition(tmp_path/'empty-models')
    assert report['models_bundled'] is False and report['onnx_cpu_provider_available']
    assert set(report['model_states'].values())=={'missing'}


def test_actual_collection_omits_implicit_weights_and_keeps_cpu_metadata():
    if not resources.whisper_cli_path().is_file(): pytest.skip('explicitly prepared build resources required')
    pytest.importorskip('rapidocr'); pytest.importorskip('PyInstaller')
    platform='windows_x64' if sys.platform=='win32' else 'macos_arm64'
    datas,binaries,hidden=helper().collect(ROOT/'packaging/resources',platform=platform)
    assert not any(Path(path).suffix.lower()=='.onnx' for path,_ in datas+binaries)
    assert any(Path(path).name=='config.yaml' for path,_ in datas)
    assert any('onnxruntime-1.22.1.dist-info' in path for path,_ in datas)
    assert 'onnxruntime.capi.onnxruntime_pybind11_state' in hidden
    assert any('onnxruntime' in path for path,_ in binaries)


def test_windows_prepare_requires_complete_msvc_openmp_before_copy(monkeypatch,tmp_path):
    build=helper(); source=tmp_path/'official-release'; source.mkdir(); output=tmp_path/'bundle'
    pe=bytearray(70); pe[:2]=b'MZ'; pe[60:64]=(64).to_bytes(4,'little'); pe[64:70]=b'PE\x00\x00\x64\x86'
    binary=source/'whisper-cli.exe'; binary.write_bytes(pe)
    row={'name':binary.name,'size_bytes':len(pe),'sha256':hashlib.sha256(pe).hexdigest()}
    lock={'whisper':{'version':'fixed','source_commit':'a'*40,'windows_x64':{'candidate_engine_files':[row]}}}
    lock_path=tmp_path/'lock.json'; lock_path.write_text(json.dumps(lock), encoding='utf-8'); vc=tmp_path/'empty-vc'; vc.mkdir()
    monkeypatch.setattr(build,'_versions',lambda *a: {})
    monkeypatch.setattr(build,'_licenses',lambda *a: [])
    with pytest.raises(RuntimeError,match='runtime.*missing|missing.*runtime'):
        build.prepare(source,output,platform='windows_x64',vc_root=vc,lock_path=lock_path)
    assert not output.exists()


def windows_dependency_fixture(monkeypatch,tmp_path,installed='4.9.3'):
    """Pure Windows admission metadata/PE fixture; never executes Windows code."""
    build=helper(); lock=json.loads((ROOT/'packaging/recognition-lock.json').read_text(encoding='utf-8'))
    expected={row['name']:row['version'] for row in lock['platforms']['windows_x64']['artifacts']}
    source_only=lock['platforms']['windows_x64']['source_only_dependency']
    assert (source_only['name'],source_only['version'])==('antlr4-python3-runtime','4.9.3')
    expected[source_only['name']]=source_only['version']
    def version(name):
        if name==source_only['name']:
            if installed is None: raise build.importlib.metadata.PackageNotFoundError(name)
            return installed
        return expected[name]
    monkeypatch.setattr(build.importlib.metadata,'version',version)
    source=tmp_path/'source'; source.mkdir(); vc=tmp_path/'vc'; vc.mkdir()
    pe=bytearray(70); pe[:2]=b'MZ'; pe[60:64]=(64).to_bytes(4,'little'); pe[64:70]=b'PE\x00\x00\x64\x86'
    tool=source/'whisper-cli.exe'; tool.write_bytes(pe)
    row={'name':tool.name,'size_bytes':len(pe),'sha256':hashlib.sha256(pe).hexdigest()}
    lock['whisper']['windows_x64']['candidate_engine_files']=[row]
    for name in build.VC_RUNTIME: (vc/name).write_bytes(pe)
    lock_path=tmp_path/'lock.json'; lock_path.write_text(json.dumps(lock), encoding='utf-8')
    monkeypatch.setattr(build,'_licenses',lambda *args: [])
    return build,lock,lock_path,source,vc,expected


@pytest.mark.parametrize('installed',['4.9.2',None,'4.9.3'])
def test_windows_source_dependency_admission_and_inventory(monkeypatch,tmp_path,installed):
    build,lock,lock_path,source,vc,expected=windows_dependency_fixture(monkeypatch,tmp_path,installed)
    output=tmp_path/'bundle'
    if installed!='4.9.3':
        with pytest.raises(RuntimeError,match='dependency.*(mismatch|missing)'):
            build.prepare(source,output,platform='windows_x64',vc_root=vc,lock_path=lock_path)
        assert not output.exists()
    else:
        inventory=build.prepare(source,output,platform='windows_x64',vc_root=vc,lock_path=lock_path)
        assert inventory['packages']==expected
        assert build.check(output,platform='windows_x64',lock_path=lock_path)['packages']==expected


@pytest.mark.parametrize('inventory_version',['4.9.2',None])
def test_windows_source_dependency_inventory_cannot_disagree_with_lock(monkeypatch,tmp_path,inventory_version):
    build,lock,lock_path,source,vc,expected=windows_dependency_fixture(monkeypatch,tmp_path)
    output=tmp_path/'bundle'
    inventory=build.prepare(source,output,platform='windows_x64',vc_root=vc,lock_path=lock_path)
    assert inventory['packages']['antlr4-python3-runtime']=='4.9.3'
    if inventory_version is None: inventory['packages'].pop('antlr4-python3-runtime')
    else: inventory['packages']['antlr4-python3-runtime']=inventory_version
    (output/'recognition-inventory.json').write_text(json.dumps(inventory), encoding='utf-8')
    with pytest.raises(RuntimeError,match='inventory.*lock'):
        build.check(output,platform='windows_x64',lock_path=lock_path)


@pytest.mark.parametrize(('installed','inventory_version'),[
    ('4.9.2','4.9.3'),(None,'4.9.3'),('4.9.3','4.9.2'),('4.9.3',None),('4.9.3','4.9.3')])
def test_frozen_windows_source_dependency_matches_lock_and_inventory(monkeypatch,tmp_path,installed,inventory_version):
    build,lock,lock_path,source,vc,expected=windows_dependency_fixture(monkeypatch,tmp_path,installed)
    # A metadata fixture bypasses preparation only to exercise the frozen guard.
    bin_root=tmp_path/'bin'; bin_root.mkdir()
    files=[]
    for path in [source/'whisper-cli.exe',*vc.iterdir()]:
        target=bin_root/path.name; target.write_bytes(path.read_bytes()); files.append(build._record(target))
    inventory={'platform':'windows_x64','models_bundled':False,'engine_files':files,'packages':dict(expected)}
    if inventory_version is None: inventory['packages'].pop('antlr4-python3-runtime')
    else: inventory['packages']['antlr4-python3-runtime']=inventory_version
    (tmp_path/'recognition-inventory.json').write_text(json.dumps(inventory), encoding='utf-8')
    monkeypatch.setattr(sys,'platform','win32'); monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(bundle_check,'whisper_cli_path',lambda:bin_root/'whisper-cli.exe')
    monkeypatch.setattr(bundle_check,'recognition_lock_path',lambda:lock_path)
    monkeypatch.setattr(bundle_check,'resource_path',lambda relative:tmp_path/relative)
    monkeypatch.setattr(bundle_check.subprocess,'run',lambda *args,**kwargs:types.SimpleNamespace(returncode=0))
    rapid=types.ModuleType('rapidocr'); rapid.__file__=str(tmp_path/'rapidocr/__init__.py')
    ort=types.ModuleType('onnxruntime'); ort.get_available_providers=lambda:['CPUExecutionProvider']
    monkeypatch.setitem(sys.modules,'rapidocr',rapid); monkeypatch.setitem(sys.modules,'onnxruntime',ort)
    from yijianjifa.features.downloads import models
    model_calls=[]
    class EmptyStore:
        def __init__(self,path): model_calls.append(path)
        def list(self): return []
    monkeypatch.setattr(models,'ModelStore',EmptyStore)
    if installed=='4.9.3' and inventory_version=='4.9.3':
        assert bundle_check.check_recognition(tmp_path/'models')['packages']['antlr4-python3-runtime']=='4.9.3'
        assert len(model_calls)==1
    else:
        with pytest.raises(RuntimeError,match='(dependency|package|inventory).*(missing|mismatch|lock)'):
            bundle_check.check_recognition(tmp_path/'models')
        assert not model_calls


def browser_helper():
    import importlib.util
    spec=importlib.util.spec_from_file_location('p011_mac_browser_bundle',ROOT/'packaging/macos/browser_bundle.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def media_helper():
    import importlib.util
    spec=importlib.util.spec_from_file_location('p011_mac_media_bundle',ROOT/'packaging/macos/media_bundle.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def media_fixture(tmp_path):
    source=tmp_path/'source';source.mkdir()
    ffmpeg=source/'ffmpeg-macos-aarch64-v7.1';ffmpeg.write_bytes(b'ffmpeg fixture');ffmpeg.chmod(0o755)
    ffprobe=source/'ffprobe-8.1.1';ffprobe.write_bytes(b'ffprobe fixture');ffprobe.chmod(0o751)
    return ffmpeg,ffprobe


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS media staging only')
def test_mac_media_stages_canonical_names_and_real_pyinstaller_toc(tmp_path):
    from PyInstaller.building.utils import format_binaries_and_datas
    ffmpeg,ffprobe=media_fixture(tmp_path);build=media_helper();output=tmp_path/'staging'
    before={path:build._record(path) for path in (ffmpeg,ffprobe)}
    inventory=build.stage(ffmpeg,ffprobe,output)
    assert {target for target,_ in format_binaries_and_datas([(str(output/name),'bin') for name in ('ffmpeg','ffprobe')])}=={'bin/ffmpeg','bin/ffprobe'}
    for name,source in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]:
        copy=output/name
        assert copy.is_file() and not copy.is_symlink()
        assert build._record(source)==before[source]
        for key in ('size_bytes','mode','sha256'):
            assert inventory['tools'][name]['source'][key]==inventory['tools'][name]['copy'][key]==before[source][key]
    assert json.loads((output/'media-inventory.json').read_text(encoding='utf-8'))==inventory


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS media staging only')
@pytest.mark.parametrize('changed',['copy','source'])
def test_mac_media_staging_rejects_changed_identity(monkeypatch,tmp_path,changed):
    ffmpeg,ffprobe=media_fixture(tmp_path);build=media_helper();actual=build._ditto
    def modified_copy(source,target):
        actual(source,target)
        (target if changed=='copy' else source).write_bytes(b'replaced')
    monkeypatch.setattr(build,'_ditto',modified_copy)
    with pytest.raises(RuntimeError,match='identity'):build.stage(ffmpeg,ffprobe,tmp_path/'staging')
    assert not (tmp_path/'staging/media-inventory.json').exists()


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS media staging only')
def test_mac_media_staging_refuses_reused_directory(tmp_path):
    ffmpeg,ffprobe=media_fixture(tmp_path);output=tmp_path/'staging';output.mkdir()
    previous=output/'ffmpeg';previous.write_bytes(b'previous build')
    with pytest.raises(RuntimeError,match='empty'):media_helper().stage(ffmpeg,ffprobe,output)
    assert previous.read_bytes()==b'previous build'


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS media spec only')
@pytest.mark.parametrize('noncanonical',['ffmpeg','ffprobe'])
def test_mac_spec_rejects_noncanonical_media_basename(monkeypatch,tmp_path,noncanonical):
    from PyInstaller.building.utils import format_binaries_and_datas
    hooks=pytest.importorskip('PyInstaller.utils.hooks')
    for name in ['collect_data_files','collect_submodules']:monkeypatch.setattr(hooks,name,lambda *a,**kw:[])
    recognition=types.ModuleType('recognition_bundle');recognition.collect=lambda *a,**kw:([],[],[])
    monkeypatch.setitem(sys.modules,'recognition_bundle',recognition);monkeypatch.setattr(sys,'path',list(sys.path))
    for name in ('ffmpeg','ffprobe'):
        tool=tmp_path/(name+'-upstream-version' if name==noncanonical else name);tool.touch()
        monkeypatch.setenv('P011_'+name.upper(),str(tool))
        if name==noncanonical:
            assert {target for target,_ in format_binaries_and_datas([(str(tool),'bin')])}=={'bin/'+tool.name}
    empty=lambda *args,**kwargs:None
    analysis=lambda *args,**kwargs:types.SimpleNamespace(pure=[],scripts=[],binaries=[],datas=[])
    with pytest.raises(SystemExit,match='canonical'):
        runpy.run_path(str(ROOT/'packaging/macos/yijianjifa.spec'),init_globals={'SPECPATH':str(ROOT/'packaging/macos'),
            'Analysis':analysis,'PYZ':empty,'EXE':empty,'COLLECT':empty,'BUNDLE':empty})


def browser_fixture(tmp_path):
    import plistlib
    root=tmp_path/'browsers';root.mkdir()
    manifest=tmp_path/'browsers.json'
    manifest.write_text(json.dumps({'browsers':[{'name':name,'revision':revision,'browserVersion':'145.0.7632.6'}
        for name,revision in [('chromium','1208'),('chromium-headless-shell','1208'),('ffmpeg','1011')]]}), encoding='utf-8')
    app=root/'chromium-1208/chrome-mac-arm64/Google Chrome for Testing.app'
    (app/'Contents/MacOS').mkdir(parents=True)
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString':'145.0.7632.6','CFBundleExecutable':'Google Chrome for Testing'}))
    (app/'Contents/MacOS/Google Chrome for Testing').write_bytes(b'fixture')
    for relative in ['chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell','ffmpeg-1011/ffmpeg-mac']:
        path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture');path.chmod(0o755)
    (app/'Contents/MacOS/Google Chrome for Testing').chmod(0o755)
    for name in ['chromium-1208','chromium_headless_shell-1208','ffmpeg-1011']:
        (root/name/'INSTALLATION_COMPLETE').touch();(root/name/'DEPENDENCIES_VALIDATED').touch()
    (root/'chromium-1228').mkdir()
    return root,manifest


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS browser packaging only')
def test_mac_spec_excludes_browser_trees_and_stamps_current_app_version(monkeypatch,tmp_path):
    hooks=pytest.importorskip('PyInstaller.utils.hooks')
    for name in ['collect_data_files','collect_submodules']: monkeypatch.setattr(hooks,name,lambda *a,**kw:[])
    recognition=types.ModuleType('recognition_bundle');recognition.collect=lambda *a,**kw:([],[],[])
    monkeypatch.setitem(sys.modules,'recognition_bundle',recognition)
    monkeypatch.setattr(sys,'path',list(sys.path))
    browsers,_=browser_fixture(tmp_path);monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(browsers))
    for name in ('ffmpeg','ffprobe'):
        tool=tmp_path/name;tool.touch();monkeypatch.setenv('P011_'+name.upper(),str(tool))
    captured=[];bundle_options={}
    def analysis(*args,**kwargs):
        captured.extend(kwargs['datas']);return types.SimpleNamespace(pure=[],scripts=[],binaries=[],datas=[])
    empty=lambda *args,**kwargs:None
    def bundle(*args,**kwargs):bundle_options.update(kwargs)
    runpy.run_path(str(ROOT/'packaging/macos/yijianjifa.spec'),init_globals={'SPECPATH':str(ROOT/'packaging/macos'),
        'Analysis':analysis,'PYZ':empty,'EXE':empty,'COLLECT':empty,'BUNDLE':bundle})
    assert not any(destination.startswith('playwright-browsers') for _,destination in captured)
    from yijianjifa import APP_VERSION
    assert bundle_options['info_plist']['CFBundleShortVersionString']==APP_VERSION
    assert bundle_options['info_plist']['CFBundleVersion']==APP_VERSION


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS browser packaging only')
def test_mac_browser_selection_uses_exact_driver_revision(monkeypatch,tmp_path):
    browsers,manifest=browser_fixture(tmp_path); build=browser_helper()
    assert [path.name for path in build.selected_roots(browsers,manifest)]==['chromium-1208','chromium_headless_shell-1208','ffmpeg-1011']
    data=json.loads(manifest.read_text(encoding='utf-8'));data['browsers'][0]['revision']='1228';manifest.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(RuntimeError,match='revision'): build.selected_roots(browsers,manifest)


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS browser packaging only')
def test_mac_browser_missing_pinned_revision_rejects(tmp_path):
    browsers,manifest=browser_fixture(tmp_path); (browsers/'ffmpeg-1011/INSTALLATION_COMPLETE').unlink()
    with pytest.raises(RuntimeError,match='incomplete'): browser_helper().selected_roots(browsers,manifest)


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS browser packaging only')
def test_mac_browser_fingerprint_checks_links_files_and_modes(tmp_path):
    build=browser_helper();root=tmp_path/'tree';root.mkdir();exe=root/'engine';exe.write_bytes(b'engine');exe.chmod(0o755)
    (root/'Current').symlink_to('engine')
    before=build.fingerprint(root)
    exe.chmod(0o644);assert build.fingerprint(root)!=before
    exe.chmod(0o755);exe.write_bytes(b'replaced');assert build.fingerprint(root)!=before
    (root/'Current').unlink();(root/'Current').symlink_to('../outside')
    with pytest.raises(RuntimeError,match='symlink'): build.fingerprint(root)


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS browser packaging only')
def test_mac_browser_copy_rejects_changed_clone_before_signing(monkeypatch,tmp_path):
    import shutil
    browsers,manifest=browser_fixture(tmp_path);build=browser_helper()
    def changed_copy(source,target):
        shutil.copytree(source,target,symlinks=True)
        (target/'INSTALLATION_COMPLETE').write_bytes(b'changed')
    monkeypatch.setattr(build,'_ditto',changed_copy)
    monkeypatch.setattr(build,'_signature',lambda path:{'valid':False})
    monkeypatch.setattr(build,'_sign',lambda root:pytest.fail('changed clone must reject before re-signing'))
    with pytest.raises(RuntimeError,match='identity'): build.stage(browsers,tmp_path/'Probe.app',driver_manifest=manifest)


def process_alive(pid):
    import os
    try: os.kill(pid,0)
    except ProcessLookupError: return False
    return True


@pytest.mark.skipif(sys.platform!='darwin',reason='macOS smoke owned-session cleanup')
@pytest.mark.parametrize('mode',['timeout','abnormal','early_success','normal','overflow'])
def test_browser_smoke_real_parent_child_stop_before_profile_removal(monkeypatch,tmp_path,mode):
    import os,shutil,signal
    build=browser_helper();profile=tmp_path/'profile';profile.mkdir();pids=tmp_path/'pids.json'
    script=f'''
import json,os,pathlib,signal,subprocess,sys,time
profile=pathlib.Path({str(profile)!r})
(profile/'owner.json').write_text(json.dumps({{'pid':os.getpid()}}))
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
pathlib.Path({str(pids)!r}).write_text(json.dumps({{'parent':os.getpid(),'child':child.pid,'group':os.getpgrp()}}))
if {mode!r}=='timeout':time.sleep(60)
elif {mode!r}=='abnormal':sys.exit(7)
elif {mode!r}=='normal':child.kill();child.wait();print('{{"closed":true}}')
elif {mode!r}=='overflow':print('x'*32768)
else:print('{{"closed":true}}')
'''
    removed=[];actual_remove=shutil.rmtree
    def checked_remove(path,*args,**kwargs):
        if Path(path)==profile:
            values=json.loads(pids.read_text(encoding='utf-8'))
            assert not any(process_alive(values[key]) for key in ('parent','child'))
            removed.append(True)
        return actual_remove(path,*args,**kwargs)
    monkeypatch.setattr(shutil,'rmtree',checked_remove)
    try:
        if mode in ('timeout','abnormal','overflow'):
            with pytest.raises(RuntimeError,match='browser.*(timeout|media_failed)'):
                build._run_owned_smoke(Path(sys.executable),('-c',script),profile,timeout=.4)
        else:
            assert json.loads(build._run_owned_smoke(Path(sys.executable),('-c',script),profile,timeout=2))['closed']
        values=json.loads(pids.read_text(encoding='utf-8'));assert values['parent']==values['group']
        assert not any(process_alive(values[key]) for key in ('parent','child'))
        assert removed==[True] and not profile.exists()
    finally:
        if pids.exists():
            values=json.loads(pids.read_text(encoding='utf-8'))
            for key in ('parent','child'):
                if process_alive(values[key]):
                    try:os.kill(values[key],signal.SIGKILL)
                    except ProcessLookupError:pass


@pytest.mark.skipif(sys.platform!='darwin',reason='actual Node smoke ownership instrumentation')
@pytest.mark.parametrize('unexpected',[False,True])
def test_smoke_node_guard_owns_only_canonical_browser_children(tmp_path,unexpected):
    import os,signal
    playwright=pytest.importorskip('playwright');node=Path(playwright.__file__).parent/'driver/node'
    if not node.is_file():pytest.skip('bundled Node preparation required')
    build=browser_helper();profile=tmp_path/'profile';profile.mkdir();browser_root=tmp_path/'browser';package=tmp_path/'package';package.mkdir()
    pid_file=tmp_path/'child.json'
    child=browser_root/'chromium-1208/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
    child.parent.mkdir(parents=True)
    child.write_text(f'#!{sys.executable}\nimport json,os,pathlib,signal,time\npathlib.Path({str(pid_file)!r}).write_text(json.dumps({{"pid":os.getpid(),"group":os.getpgrp()}}))\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(60)\n', encoding='utf-8');child.chmod(0o755)
    second=browser_root/'chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell'
    second.parent.mkdir(parents=True);second.symlink_to(child)
    command='/usr/bin/true' if unexpected else str(child)
    (package/'index.js').write_text(f'''const cp=require('child_process');module.exports={{chromium:{{launchPersistentContext:async()=>{{
cp.spawn({json.dumps(command)},[],{{detached:true,stdio:'ignore'}});
return {{route:async()=>{{}},pages:()=>[{{goto:async()=>{{}},url:()=> 'about:blank'}}],browser:()=>({{version:()=> '145.0.7632.6'}}),close:()=>new Promise(()=>{{}})}};
}}}}}};''', encoding='utf-8')
    try:
        with pytest.raises(RuntimeError,match='browser.*'+('media_failed' if unexpected else 'timeout')):
            # 此处验证自有进程清理，须先允许合成Python浏览器完成启动。
            # 本机原生解释器冷启动可超过1秒；仍保持有界超时及下面全部归属断言。
            build._run_owned_smoke(node,('-e',build.SMOKE_JS,str(package),str(profile),str(browser_root)),profile,timeout=4)
        assert not profile.exists()
        if unexpected:assert not pid_file.exists()
        else:
            values=json.loads(pid_file.read_text(encoding='utf-8'));assert values['group']!=values['pid']
            assert not process_alive(values['pid'])
    finally:
        if pid_file.exists():
            pid=json.loads(pid_file.read_text(encoding='utf-8'))['pid']
            if process_alive(pid):
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
