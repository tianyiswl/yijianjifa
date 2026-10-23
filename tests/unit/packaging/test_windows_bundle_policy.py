"""Exercise the Windows spec's collection policy without compiling PE binaries."""
import json
from pathlib import Path
import runpy
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def windows_inputs(monkeypatch, tmp_path):
    hooks = pytest.importorskip('PyInstaller.utils.hooks')
    qt_core = pytest.importorskip('PySide6.QtCore')
    monkeypatch.setattr(hooks, 'collect_submodules', lambda *a, **kw: [])
    monkeypatch.setattr(hooks, 'collect_data_files', lambda *a, **kw: [])
    monkeypatch.setattr(sys, 'path', list(sys.path))
    recognition = types.ModuleType('recognition_bundle')
    recognition_dll = tmp_path / 'whisper.dll'
    recognition_dll.write_bytes(b'locked recognition runtime')
    recognition.collect = lambda *a, **kw: ([], [(str(recognition_dll), 'bin')], [])
    monkeypatch.setitem(sys.modules, 'recognition_bundle', recognition)
    playwright = types.ModuleType('playwright')
    playwright.__file__ = str(tmp_path / 'playwright' / '__init__.py')
    monkeypatch.setitem(sys.modules, 'playwright', playwright)
    manifest = tmp_path / 'playwright' / 'driver' / 'package' / 'browsers.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'browsers': [
        {'name': 'chromium', 'revision': '1208', 'browserVersion': '145.0.7632.6', 'installByDefault': True},
        {'name': 'chromium-headless-shell', 'revision': '1208', 'installByDefault': True},
        {'name': 'ffmpeg', 'revision': '1011', 'installByDefault': True},
        {'name': 'winldd', 'revision': '1007', 'installByDefault': False},
    ]}), encoding='utf-8')
    browsers = tmp_path / 'browsers'
    browsers.mkdir()
    for folder, executable in (
        ('chromium-1208', 'chrome-win64/chrome.exe'),
        ('chromium_headless_shell-1208', 'chrome-headless-shell-win64/chrome-headless-shell.exe'),
        ('ffmpeg-1011', 'ffmpeg-win64.exe'),
        ('chromium-1207', 'chrome-win64/chrome.exe'),
        ('firefox-1509', 'firefox/firefox.exe'),
        ('winldd-1007', 'PrintDeps.exe'),
    ):
        root = browsers / folder
        path = root / executable
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'controlled Windows build input')
        (root / 'INSTALLATION_COMPLETE').touch()
        (root / 'DEPENDENCIES_VALIDATED').touch()
        (root / 'LICENSE').write_text('retained browser license', encoding='utf-8')
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(browsers))
    tools = []
    for name in ('ffmpeg', 'ffprobe'):
        tool = tmp_path / (name + '.exe')
        tool.write_bytes(('formal ' + name).encode())
        monkeypatch.setenv('P011_' + name.upper(), str(tool))
        tools.append(tool)
    debug_names = (
        'qtwebengine_devtools_resources.debug.pak',
        'qtwebengine_resources.debug.pak',
        'qtwebengine_resources_100p.debug.pak',
        'qtwebengine_resources_200p.debug.pak',
        'v8_context_snapshot.debug.bin',
    )
    normal_names = (
        'qtwebengine_devtools_resources.pak',
        'qtwebengine_resources.pak',
        'qtwebengine_resources_100p.pak',
        'qtwebengine_resources_200p.pak',
        'v8_context_snapshot.bin',
        'icudtl.dat',
        'future_resource.debug.pak',
    )
    normal = ['PySide6/resources/' + name for name in normal_names]
    normal += ['PySide6/translations/qtwebengine_locales/zh-CN.pak',
               'PySide6/translations/qtwebengine_locales/fr.pak',
               'PySide6/qml/QtQuick/qmldir', 'licenses/qt/debug-license.txt']
    collected_datas = [(destination, str(tmp_path / 'qt-input' / destination), 'DATA')
                       for destination in normal + ['PySide6/resources/' + name for name in debug_names]]
    qt_debug = {'value': False}
    monkeypatch.setattr(qt_core, 'QLibraryInfo', types.SimpleNamespace(isDebugBuild=lambda: qt_debug['value']))
    captured = {}

    def analysis(*args, **kwargs):
        captured['analysis'] = kwargs
        return types.SimpleNamespace(pure=[], scripts=[], binaries=kwargs['binaries'], datas=list(collected_datas))

    def collect(exe, binaries, datas, **kwargs):
        captured['binaries'] = binaries
        captured['datas'] = datas

    def run():
        runpy.run_path(str(ROOT / 'packaging/windows/yijianjifa.spec'), init_globals={
            'SPECPATH': str(ROOT / 'packaging/windows'), 'Analysis': analysis,
            'PYZ': lambda *a, **kw: None, 'EXE': lambda *a, **kw: None, 'COLLECT': collect,
        })
        return captured

    return types.SimpleNamespace(run=run, browsers=browsers, manifest=manifest,
        debug_names=debug_names, normal=normal, qt_debug=qt_debug, tools=tools,
        recognition_dll=recognition_dll, collected_datas=collected_datas)


def selected_browser_rows(result):
    return [(Path(source), destination) for source, destination in result['analysis']['datas']
            if destination.startswith('playwright-browsers/')]


def test_windows_bundle_selects_only_pinned_browser_and_required_helpers(windows_inputs):
    result = windows_inputs.run()
    assert [(source.name, target) for source, target in selected_browser_rows(result)] == [
        ('chromium-1208', 'playwright-browsers/chromium-1208'),
        ('ffmpeg-1011', 'playwright-browsers/ffmpeg-1011'),
        ('winldd-1007', 'playwright-browsers/winldd-1007'),
    ]
    assert all((source / 'LICENSE').read_text(encoding='utf-8') == 'retained browser license'
               for source, _ in selected_browser_rows(result))


@pytest.mark.parametrize('missing', [
    'chromium-1208/chrome-win64/chrome.exe',
    'chromium-1208/INSTALLATION_COMPLETE',
    'chromium-1208/DEPENDENCIES_VALIDATED',
    'ffmpeg-1011/ffmpeg-win64.exe',
    'ffmpeg-1011/INSTALLATION_COMPLETE',
    'winldd-1007/PrintDeps.exe',
    'winldd-1007/INSTALLATION_COMPLETE',
    'winldd-1007/DEPENDENCIES_VALIDATED',
])
def test_windows_bundle_rejects_incomplete_pinned_inputs(windows_inputs, missing):
    (windows_inputs.browsers / missing).unlink()
    with pytest.raises(RuntimeError, match='missing|incomplete'):
        windows_inputs.run()


def test_windows_bundle_does_not_fall_back_to_an_old_browser_revision(windows_inputs):
    import shutil
    shutil.rmtree(windows_inputs.browsers / 'chromium-1208')
    with pytest.raises(RuntimeError, match='revision|missing'):
        windows_inputs.run()


@pytest.mark.parametrize('invalid', ['../1208', '', None])
def test_windows_bundle_rejects_invalid_driver_revision(windows_inputs, invalid):
    data = json.loads(windows_inputs.manifest.read_text(encoding='utf-8'))
    data['browsers'][0]['revision'] = invalid
    windows_inputs.manifest.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(RuntimeError, match='revision'):
        windows_inputs.run()


def test_windows_bundle_rejects_ambiguous_driver_browser_row(windows_inputs):
    data = json.loads(windows_inputs.manifest.read_text(encoding='utf-8'))
    data['browsers'].append({'name': 'chromium', 'revision': '1207'})
    windows_inputs.manifest.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(RuntimeError, match='manifest'):
        windows_inputs.run()


def test_windows_bundle_honors_driver_windows_revision_override(windows_inputs):
    data = json.loads(windows_inputs.manifest.read_text(encoding='utf-8'))
    data['browsers'][0]['revision'] = '9000'
    data['browsers'][0]['revisionOverrides'] = {'win64': '1208', 'mac12': '1207'}
    windows_inputs.manifest.write_text(json.dumps(data), encoding='utf-8')
    assert selected_browser_rows(windows_inputs.run())[0][0].name == 'chromium-1208'


@pytest.mark.parametrize('separator', ['/', '\\'])
def test_windows_bundle_strips_only_confirmed_debug_qt_resources(windows_inputs, separator):
    windows_inputs.collected_datas[:] = [(destination.replace('/', separator), source, kind)
                                        for destination, source, kind in windows_inputs.collected_datas]
    destinations = {destination.replace('\\', '/') for destination, _, _ in windows_inputs.run()['datas']}
    assert destinations == set(windows_inputs.normal)


def test_windows_bundle_rejects_selected_browser_directory_outside_cache(windows_inputs, tmp_path):
    import shutil
    directory = windows_inputs.browsers / 'chromium-1208'
    outside = tmp_path / 'outside-browser'
    shutil.move(directory, outside)
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlink privileges unavailable on this host')
    with pytest.raises(RuntimeError, match='revision|missing'):
        windows_inputs.run()


def test_windows_bundle_rejects_debug_qt_without_removing_release_resources(windows_inputs):
    windows_inputs.qt_debug['value'] = True
    with pytest.raises(RuntimeError, match='release Qt'):
        windows_inputs.run()


def test_windows_bundle_preserves_formal_media_and_recognition_tools(windows_inputs):
    result = windows_inputs.run()
    assert result['binaries'] == [(str(windows_inputs.recognition_dll), 'bin'),
                                  (str(windows_inputs.tools[0]), 'bin'),
                                  (str(windows_inputs.tools[1]), 'bin')]
    assert [path.read_bytes() for path in windows_inputs.tools] == [b'formal ffmpeg', b'formal ffprobe']


def test_windows_bundle_excludes_development_ffmpeg_without_excluding_qt(windows_inputs):
    excluded = set(windows_inputs.run()['analysis']['excludes'])
    assert 'imageio_ffmpeg' in excluded
    assert not any(name.startswith('PySide6.') for name in excluded)
