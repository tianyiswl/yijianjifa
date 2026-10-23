"""Build-only Windows collection rules; never download or alter dependencies."""
import json
from pathlib import Path


_QT_DEBUG_RESOURCES = frozenset({
    'qtwebengine_devtools_resources.debug.pak',
    'qtwebengine_resources.debug.pak',
    'qtwebengine_resources_100p.debug.pak',
    'qtwebengine_resources_200p.debug.pak',
    'v8_context_snapshot.debug.bin',
})
_QT_DEBUG_DESTINATIONS = frozenset('PySide6/resources/' + name for name in _QT_DEBUG_RESOURCES)


def selected_browser_roots(browser_root, manifest_path):
    """Keep the driver's complete Chromium and required helpers, not cache extras.

    The application uses ``channel='chromium'`` for both visible and new
    headless publishing. Its separate shell executable is not used. The
    complete selected directories retain all browser resources and licenses.
    Windows dependency revalidation also needs its small ``winldd`` helper,
    including after the SDK's cached 30-day validation marker expires.
    """
    root = Path(browser_root).resolve()
    try:
        browsers = json.loads(Path(manifest_path).read_text(encoding='utf-8'))['browsers']
        if not isinstance(browsers, list):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('Invalid bundled browser manifest') from exc
    selected = []
    for name, executable in (
        ('chromium', 'chrome-win64/chrome.exe'),
        ('ffmpeg', 'ffmpeg-win64.exe'),
        ('winldd', 'PrintDeps.exe'),
    ):
        rows = [row for row in browsers if isinstance(row, dict) and row.get('name') == name]
        if len(rows) != 1:
            raise RuntimeError('Ambiguous or missing browser manifest entry: ' + name)
        row = rows[0]
        overrides = row.get('revisionOverrides', {})
        if not isinstance(overrides, dict):
            raise RuntimeError('Invalid bundled browser revision overrides: ' + name)
        revision = overrides.get('win64', row.get('revision'))
        if not isinstance(revision, str) or not revision.isascii() or not revision.isdigit():
            raise RuntimeError('Invalid bundled browser revision: ' + name)
        directory = root / (name + '-' + revision)
        if not directory.is_dir() or not directory.resolve().is_relative_to(root):
            raise RuntimeError('Bundled browser revision missing: ' + directory.name)
        if not (directory / executable).is_file():
            raise RuntimeError('Bundled browser executable missing: ' + directory.name)
        if not all((directory / marker).is_file() for marker in ('INSTALLATION_COMPLETE', 'DEPENDENCIES_VALIDATED')):
            raise RuntimeError('Bundled browser installation incomplete: ' + directory.name)
        selected.append(directory)
    return selected


def qt_release_datas(datas, *, is_debug_build):
    """Remove only known debug twins, retaining release/QML/language resources."""
    if is_debug_build:
        raise RuntimeError('Windows bundle requires a release Qt build')
    return [row for row in datas if str(row[0]).replace('\\', '/') not in _QT_DEBUG_DESTINATIONS]
