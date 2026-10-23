"""Small, real media fixtures, confined to pytest's isolated directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from yijianjifa.features.downloads.contracts import DownloadError, OutputResource
from yijianjifa.os_integration.resources import ffmpeg_path, ffprobe_path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(path: Path) -> dict:
    result = subprocess.run(
        [str(ffprobe_path()), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
        capture_output=True, check=True, timeout=15,
    )
    return json.loads(result.stdout)


def make_video(root: Path, *, audio: bool = True, portrait: bool = False, codec: str = 'h264') -> Path:
    root.mkdir(parents=True, exist_ok=True)
    size = '96x160' if portrait else '160x96'
    suffix = '.mp4'
    destination = root / ('portrait' if portrait else 'landscape')
    destination = destination.with_name(destination.name + ('-audio' if audio else '-no-audio')).with_suffix(suffix)
    args = [str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=blue:s={size}:r=25:d=1.4']
    if audio:
        args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1.4']
    args += ['-c:v', 'libvpx-vp9' if codec == 'vp9' else 'libx264', '-pix_fmt', 'yuv420p']
    if audio:
        args += ['-c:a', 'aac', '-ac', '2']
    args += [str(destination)]
    subprocess.run(args, capture_output=True, check=True, timeout=20)
    return destination


def prefix_mp4_free_atom(path: Path, *, padding_size: int = 8192, extended_size: bool = False) -> Path:
    """Insert valid padding and adjust absolute MP4 chunk offsets recursively."""
    header_size = 16 if extended_size else 8
    assert header_size <= padding_size <= 1024 * 1024
    data = bytearray(path.read_bytes())
    containers = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}

    def adjust(start: int, end: int) -> None:
        offset = start
        while offset < end:
            size = int.from_bytes(data[offset:offset + 4], 'big')
            kind = bytes(data[offset + 4:offset + 8])
            assert size >= 8 and offset + size <= end
            if kind in containers:
                adjust(offset + 8, offset + size)
            elif kind in {b'stco', b'co64'}:
                count = int.from_bytes(data[offset + 12:offset + 16], 'big')
                width = 4 if kind == b'stco' else 8
                assert offset + 16 + count * width <= offset + size
                for index in range(count):
                    location = offset + 16 + index * width
                    value = int.from_bytes(data[location:location + width], 'big')
                    data[location:location + width] = (value + padding_size).to_bytes(width, 'big')
            offset += size
    adjust(0, len(data))
    prefix = ((1).to_bytes(4, 'big') + b'free' + padding_size.to_bytes(8, 'big')
              if extended_size else padding_size.to_bytes(4, 'big') + b'free')
    prefix += bytes(padding_size - header_size)
    destination = path.with_name('prefix-free-' + path.name)
    destination.write_bytes(prefix + data)
    return destination


class TestOwnedFiles:
    """Test owner implementing only the approved backend handoff contract."""
    __test__ = False

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.resources: dict[str, tuple[OutputResource, Path]] = {}
        self.allocated: list[Path] = []

    def register(self, path: Path, *, identity: str = 'fixture') -> OutputResource:
        path = path.resolve()
        assert path.is_relative_to(self.root)
        resource = OutputResource(identity, 'candidate-' + identity, 'item-' + identity, 'video', digest(path), path.stat().st_size, 1400)
        self.resources[identity] = resource, path
        return resource

    def path_for(self, resource_id: str) -> Path:
        resource, path = self.resources[resource_id]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != resource.size_bytes or digest(path) != resource.sha256:
            raise DownloadError('identity_mismatch')
        return path

    def temporary_for(self, source: OutputResource, kind: str) -> Path:
        resource, _ = self.resources[source.id]
        if resource != source:
            raise DownloadError('identity_mismatch')
        suffix = {'mp3': '.mp3', 'wav': '.wav', 'silent': '.mp4', 'asr_wav': '.wav'}[kind]
        temporary_dir = self.root / '.download-temporary'
        temporary_dir.mkdir(exist_ok=True)
        handle, filename = tempfile.mkstemp(prefix='owned-', suffix=suffix, dir=temporary_dir)
        os.close(handle)
        path = Path(filename)
        self.allocated.append(path)
        return path


class NeverCancel:
    def cancelled(self) -> bool:
        return False
