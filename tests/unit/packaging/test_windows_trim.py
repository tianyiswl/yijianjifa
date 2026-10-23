"""The release trimmer removes reviewed bytes only, or leaves the bundle intact."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'packaging/windows/trim_bundle.py'


def fixture(tmp_path, entries):
    root = tmp_path / 'bundle'; root.mkdir()
    rows = []
    for relative, data in entries.items():
        file = root / relative; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(data)
        rows.append({'relative': relative, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'files': rows}))
    return root, manifest, tmp_path / 'report.json'


def run(root, manifest, report):
    return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root), '--manifest', str(manifest), '--report', str(report)], capture_output=True, text=True)


def test_removes_reviewed_language_bytes_but_preserves_video_fonts_and_unknown_files(tmp_path):
    root, manifest, report = fixture(tmp_path, {'_internal/PySide6/translations/qtbase_fr.qm': b'French'})
    keep = ['_internal/PySide6/Qt6Multimedia.dll', '_internal/PySide6/opengl32sw.dll', '_internal/PySide6/resources/icudtl.dat', '_internal/PySide6/translations/qtwebengine_locales/zh-CN.pak', '_internal/PySide6/translations/qtbase_en.qm', '_internal/bin/ffmpeg.exe', '_internal/font.ttf', '_internal/PySide6/future.dll']
    for relative in keep:
        p = root / relative; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'keep')
    result = run(root, manifest, report)
    assert result.returncode == 0, result.stderr
    assert not (root / '_internal/PySide6/translations/qtbase_fr.qm').exists()
    assert all((root / p).read_bytes() == b'keep' for p in keep)
    assert json.loads(report.read_text())['removed_bytes'] == 6


def test_changed_dependency_fails_before_any_removal(tmp_path):
    root, manifest, report = fixture(tmp_path, {'_internal/PySide6/Qt6Charts.dll': b'old1', '_internal/PySide6/Qt6Graphs.dll': b'old2'})
    (root / '_internal/PySide6/Qt6Graphs.dll').write_bytes(b'NEW2')
    result = run(root, manifest, report)
    assert result.returncode != 0
    assert (root / '_internal/PySide6/Qt6Charts.dll').read_bytes() == b'old1'
    assert not report.exists()


def test_protected_multimedia_cannot_be_in_removal_manifest(tmp_path):
    root, manifest, report = fixture(tmp_path, {'_internal/PySide6/Qt6Multimedia.dll': b'video'})
    assert run(root, manifest, report).returncode != 0
    assert (root / '_internal/PySide6/Qt6Multimedia.dll').exists()


def test_english_and_chinese_cannot_be_in_removal_manifest(tmp_path):
    root, manifest, report = fixture(tmp_path, {'_internal/PySide6/translations/qtwebengine_locales/zh-CN.pak': b'Chinese'})
    assert run(root, manifest, report).returncode != 0
    assert (root / '_internal/PySide6/translations/qtwebengine_locales/zh-CN.pak').exists()


def test_symlinked_directory_cannot_remove_files_outside_bundle(tmp_path):
    root, manifest, report = fixture(tmp_path, {'_internal/PySide6/translations/qtbase_fr.qm': b'French'})
    inside = root / '_internal/PySide6/translations'; outside = tmp_path / 'outside'; inside.rename(outside)
    try: inside.symlink_to(outside, target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip('Symlink privileges unavailable')
    assert run(root, manifest, report).returncode != 0
    assert (outside / 'qtbase_fr.qm').read_bytes() == b'French'


def test_reviewed_production_manifest_contains_only_permitted_destinations():
    import runpy
    allowed = runpy.run_path(str(SCRIPT))['allowed']
    manifest = json.loads(SCRIPT.with_name('trim-manifest.json').read_text(encoding='utf-8'))
    assert all(allowed(row['relative']) for row in manifest['files'])
