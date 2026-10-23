"""Extract only the locked Windows FFmpeg 7.1.1 pair from a verified old delivery.

This build helper performs no network requests and never executes native tools.
Its output must be a new directory in the runner's exclusively owned workspace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile


ARCHIVE_SIZE_BYTES = 639459722
ARCHIVE_SHA256 = 'a44fbf889101cd3b6b514b4ba8c7ce66d1b87c9fe6be87befd20705ab99bc986'
SOURCE_COMMIT = 'f75b1aac06a2009982c45ff2daf46aa7c46da7e5'
SOURCE_ASSET_ID = 639964820
MEDIA_FILES = {
    'ffmpeg.exe': {
        'member': '一剪即发/_internal/bin/ffmpeg.exe',
        'size_bytes': 87429632,
        'sha256': 'b90225987bdd042cca09a1efb5e34e9848f2d1dbf5fbcd388753a44145522997',
    },
    'ffprobe.exe': {
        'member': '一剪即发/_internal/bin/ffprobe.exe',
        'size_bytes': 87291904,
        'sha256': '05e8fa639450f8191635192871ae37a3ec3e4638fa12f3b7d49c6522ba16a8ed',
    },
}
CHUNK_BYTES = 4 * 1024 * 1024


def _absent(path: Path) -> None:
    if os.path.lexists(path):
        raise FileExistsError('Media output already exists: ' + str(path))


def extract_media(archive: str | Path, output_dir: str | Path) -> dict:
    """Verify the whole archive and each allowed member before publishing output.

    Extraction takes place in an exclusive sibling staging directory. A final
    absence check and same-filesystem rename expose the complete result at once.
    The build runner owns the output parent; concurrent external creation of an
    empty destination between the final check and rename is outside this contract.
    """
    archive = Path(archive).absolute()
    output = Path(output_dir).absolute()
    _absent(output)
    if not output.parent.is_dir():
        raise FileNotFoundError('Media output parent directory is missing')
    archive_stat = archive.lstat()
    if not stat.S_ISREG(archive_stat.st_mode):
        raise ValueError('Media archive must be a regular file')
    if archive_stat.st_size != ARCHIVE_SIZE_BYTES:
        raise ValueError('Media archive size does not match the locked source')
    staging = None
    try:
        with archive.open('rb') as raw:
            before = os.fstat(raw.fileno())
            archive_hash = hashlib.sha256()
            for chunk in iter(lambda: raw.read(CHUNK_BYTES), b''):
                archive_hash.update(chunk)
            if archive_hash.hexdigest() != ARCHIVE_SHA256:
                raise ValueError('Media archive SHA256 does not match the locked source')
            raw.seek(0)
            with zipfile.ZipFile(raw) as bundle:
                entries = bundle.infolist()
                names = [entry.filename for entry in entries]
                if len(names) != len(set(names)):
                    raise ValueError('Media archive contains duplicate entries')
                selected = []
                for name, expected in MEDIA_FILES.items():
                    matches = [entry for entry in entries if entry.filename == expected['member']]
                    if len(matches) != 1:
                        raise ValueError('Locked media member is missing: ' + name)
                    entry = matches[0]
                    mode = stat.S_IFMT(entry.external_attr >> 16)
                    if (entry.is_dir() or entry.flag_bits & 1 or mode not in (0, stat.S_IFREG)
                            or entry.file_size != expected['size_bytes']):
                        raise ValueError('Locked media member type or size differs: ' + name)
                    selected.append((name, expected, entry))
                staging = Path(tempfile.mkdtemp(prefix='.' + output.name + '-staging-', dir=output.parent))
                records = []
                for name, expected, entry in selected:
                    actual_hash, written = hashlib.sha256(), 0
                    with bundle.open(entry) as source, (staging / name).open('xb') as target:
                        for chunk in iter(lambda: source.read(CHUNK_BYTES), b''):
                            written += len(chunk)
                            if written > expected['size_bytes']:
                                raise ValueError('Locked media member exceeds its size: ' + name)
                            actual_hash.update(chunk)
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    if written != expected['size_bytes'] or actual_hash.hexdigest() != expected['sha256']:
                        raise ValueError('Locked media member size or SHA256 differs: ' + name)
                    records.append({'name': name, 'member': expected['member'], 'size_bytes': written,
                                    'sha256': actual_hash.hexdigest()})
            after = os.fstat(raw.fileno())
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise ValueError('Media archive changed during verification')
        report = {
            'schema_version': 1,
            'source': {'repository': 'tianyiswl/yijianjifa-desktop', 'commit': SOURCE_COMMIT,
                       'asset_id': SOURCE_ASSET_ID,
                       'archive': {'size_bytes': ARCHIVE_SIZE_BYTES, 'sha256': ARCHIVE_SHA256}},
            'ffmpeg_version': '7.1.1-essentials_build-www.gyan.dev',
            'files': records,
        }
        with (staging / 'media-source.json').open('x', encoding='utf-8', newline='\n') as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        _absent(output)
        os.rename(staging, output)
        staging = None
        return report
    finally:
        if staging is not None:
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    result = extract_media(args.archive, args.output_dir)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
