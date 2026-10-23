"""Locked build-only media extraction; small archives never execute native tools."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import warnings
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[3]
MEMBERS = {'ffmpeg.exe': '一剪即发/_internal/bin/ffmpeg.exe',
           'ffprobe.exe': '一剪即发/_internal/bin/ffprobe.exe'}
PAYLOADS = {'ffmpeg.exe': b'controlled ffmpeg binary', 'ffprobe.exe': b'controlled ffprobe binary'}


@pytest.fixture
def media_module():
    spec = importlib.util.spec_from_file_location('p011_windows_media_inputs', ROOT / 'packaging/windows/media_inputs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_archive(tmp_path, *, extra=None, missing=None, duplicate=None, symlink=None):
    archive = tmp_path / 'original.zip'
    with warnings.catch_warnings(), zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        warnings.simplefilter('ignore', UserWarning)
        for name, member in MEMBERS.items():
            if name == missing:
                continue
            if name == symlink:
                info = zipfile.ZipInfo(member)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                target.writestr(info, PAYLOADS[name])
            else:
                target.writestr(member, PAYLOADS[name])
        for member, payload in (extra or {}).items():
            target.writestr(member, payload)
        if duplicate:
            target.writestr(duplicate, b'duplicate')
    return archive


def admit(module, monkeypatch, archive):
    monkeypatch.setattr(module, 'ARCHIVE_SIZE_BYTES', archive.stat().st_size)
    monkeypatch.setattr(module, 'ARCHIVE_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setattr(module, 'MEDIA_FILES', {name: {'member': member, 'size_bytes': len(PAYLOADS[name]),
        'sha256': hashlib.sha256(PAYLOADS[name]).hexdigest()} for name, member in MEMBERS.items()})


def assert_clean(tmp_path, output):
    assert not output.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ['original.zip']


def test_extracts_only_two_locked_tools_and_source_record(media_module, tmp_path, monkeypatch):
    archive = make_archive(tmp_path, extra={'../outside': b'do not extract',
        '一剪即发/private/account.db': b'do not extract', 'unused.txt': b'do not extract'})
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'new-media'
    report = media_module.extract_media(archive, output)
    assert {path.name for path in output.iterdir()} == {'ffmpeg.exe', 'ffprobe.exe', 'media-source.json'}
    assert not (tmp_path.parent / 'outside').exists()
    for name, payload in PAYLOADS.items():
        assert (output / name).read_bytes() == payload
    assert json.loads((output / 'media-source.json').read_text(encoding='utf-8')) == report
    assert report['source']['commit'] == 'f75b1aac06a2009982c45ff2daf46aa7c46da7e5'
    assert report['source']['asset_id'] == 639964820
    assert {item['name'] for item in report['files']} == set(PAYLOADS)
    assert {path.name for path in tmp_path.iterdir()} == {'original.zip', 'new-media'}


@pytest.mark.parametrize('kind', ['size', 'hash'])
def test_rejects_bad_whole_archive_without_creating_output(media_module, tmp_path, monkeypatch, kind):
    archive = make_archive(tmp_path)
    admit(media_module, monkeypatch, archive)
    monkeypatch.setattr(media_module, 'ARCHIVE_SIZE_BYTES' if kind == 'size' else 'ARCHIVE_SHA256',
                        archive.stat().st_size + 1 if kind == 'size' else '0' * 64)
    output = tmp_path / 'new-media'
    with pytest.raises(ValueError, match='archive'):
        media_module.extract_media(archive, output)
    assert_clean(tmp_path, output)


@pytest.mark.parametrize(('name', 'kind'), [('ffmpeg.exe', 'hash'), ('ffprobe.exe', 'hash'),
                                         ('ffmpeg.exe', 'size'), ('ffprobe.exe', 'size')])
def test_rejects_bad_tool_and_removes_all_staging(media_module, tmp_path, monkeypatch, name, kind):
    archive = make_archive(tmp_path)
    admit(media_module, monkeypatch, archive)
    media_module.MEDIA_FILES[name]['sha256' if kind == 'hash' else 'size_bytes'] = (
        '0' * 64 if kind == 'hash' else len(PAYLOADS[name]) + 1)
    output = tmp_path / 'new-media'
    with pytest.raises(ValueError, match='media'):
        media_module.extract_media(archive, output)
    assert_clean(tmp_path, output)


@pytest.mark.parametrize('duplicate', [MEMBERS['ffmpeg.exe'], 'unused.txt'])
def test_duplicate_archive_entries_are_rejected(media_module, tmp_path, monkeypatch, duplicate):
    archive = make_archive(tmp_path, extra={'unused.txt': b'first'}, duplicate=duplicate)
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'new-media'
    with pytest.raises(ValueError, match='duplicate'):
        media_module.extract_media(archive, output)
    assert_clean(tmp_path, output)


def test_missing_tool_is_rejected_without_publishing_partial_directory(media_module, tmp_path, monkeypatch):
    archive = make_archive(tmp_path, missing='ffprobe.exe')
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'new-media'
    with pytest.raises(ValueError, match='media'):
        media_module.extract_media(archive, output)
    assert_clean(tmp_path, output)


def test_link_instead_of_locked_tool_is_rejected(media_module, tmp_path, monkeypatch):
    archive = make_archive(tmp_path, symlink='ffprobe.exe')
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'new-media'
    with pytest.raises(ValueError, match='media'):
        media_module.extract_media(archive, output)
    assert_clean(tmp_path, output)


@pytest.mark.parametrize('kind', ['directory', 'file'])
def test_existing_output_is_untouched(media_module, tmp_path, monkeypatch, kind):
    archive = make_archive(tmp_path)
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'existing'
    if kind == 'directory':
        output.mkdir()
        marker = output / 'original.txt'
    else:
        marker = output
    marker.write_bytes(b'original')
    with pytest.raises(FileExistsError):
        media_module.extract_media(archive, output)
    assert marker.read_bytes() == b'original'
    assert {path.name for path in tmp_path.iterdir()} == {'original.zip', 'existing'}


def test_failed_final_rename_leaves_no_partial_output(media_module, tmp_path, monkeypatch):
    archive = make_archive(tmp_path)
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'new-media'
    def denied(*args):
        raise PermissionError('controlled rename failure')
    monkeypatch.setattr(media_module.os, 'rename', denied)
    with pytest.raises(PermissionError):
        media_module.extract_media(archive, output)
    assert_clean(tmp_path, output)


def test_existing_empty_output_is_not_replaced(media_module, tmp_path, monkeypatch):
    archive = make_archive(tmp_path)
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'existing'
    output.mkdir()
    with pytest.raises(FileExistsError):
        media_module.extract_media(archive, output)
    assert output.is_dir() and list(output.iterdir()) == []
    assert {path.name for path in tmp_path.iterdir()} == {'original.zip', 'existing'}


def test_final_absence_check_preserves_output_created_during_verification(media_module, tmp_path, monkeypatch):
    archive = make_archive(tmp_path)
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'existing'
    original = media_module._absent
    checks = []
    def another_owner(path):
        checks.append(path)
        if len(checks) == 2:
            output.mkdir()
            (output / 'original.txt').write_bytes(b'keep original')
        original(path)
    monkeypatch.setattr(media_module, '_absent', another_owner)
    with pytest.raises(FileExistsError):
        media_module.extract_media(archive, output)
    assert len(checks) == 2
    assert (output / 'original.txt').read_bytes() == b'keep original'
    assert {path.name for path in output.iterdir()} == {'original.txt'}
    assert {path.name for path in tmp_path.iterdir()} == {'original.zip', 'existing'}


def test_cli_console_json_works_with_non_utf8_stdout(media_module, tmp_path, monkeypatch):
    archive = make_archive(tmp_path)
    admit(media_module, monkeypatch, archive)
    output = tmp_path / 'new-media'
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding='ascii')
    with monkeypatch.context() as context:
        context.setattr(sys, 'stdout', console)
        context.setattr(sys, 'argv', ['media_inputs.py', '--archive', str(archive), '--output-dir', str(output)])
        assert media_module.main() == 0
        console.flush()
    displayed = json.loads(raw.getvalue().decode('ascii'))
    saved = json.loads((output / 'media-source.json').read_text(encoding='utf-8'))
    assert displayed == saved
    assert '一剪即发' in (output / 'media-source.json').read_text(encoding='utf-8')
