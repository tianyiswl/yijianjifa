"""Experimental read-only capabilities. Only trusted Python may grant a file."""
import os
import re
import secrets
import stat
import threading
from pathlib import Path


class ResourceDenied(ValueError):
    pass


def byte_range(header, size):
    if size <= 0:
        raise ResourceDenied('empty_resource')
    if header is None:
        return 0, size - 1, 200
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()):
        raise ResourceDenied('invalid_range')
    left, right = match.groups()
    if not left:
        count = int(right)
        if count <= 0:
            raise ResourceDenied('invalid_range')
        return max(0, size - count), size - 1, 206
    start, end = int(left), min(int(right), size - 1) if right else size - 1
    if start >= size or end < start:
        raise ResourceDenied('invalid_range')
    return start, end, 206


class MediaRegistry:
    def __init__(self):
        self._files = {}
        self._lock = threading.RLock()
        self._closed = False

    def grant(self, path):
        with self._lock:
            if self._closed:
                raise ResourceDenied('closed')
            try:
                fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                raise ResourceDenied('invalid_file') from None
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                os.close(fd)
                raise ResourceDenied('invalid_file')
            token = secrets.token_urlsafe(24)
            self._files[token] = (fd, info.st_size, info.st_mtime_ns)
            return token

    def _get(self, token):
        entry = self._files.get(token)
        if entry is None or self._closed:
            raise ResourceDenied('unknown_resource')
        fd, size, mtime = entry
        info = os.fstat(fd)
        if info.st_size != size or info.st_mtime_ns != mtime:
            raise ResourceDenied('resource_changed')
        return fd, size

    def size(self, token):
        with self._lock:
            return self._get(token)[1]

    def read(self, token, offset, length):
        with self._lock:
            fd, size = self._get(token)
            if type(offset) is not int or type(length) is not int or offset < 0 or not 0 <= length <= 1048576 or offset + length > size:
                raise ResourceDenied('invalid_read')
            data = os.pread(fd, length, offset)
            if len(data) != length:
                raise ResourceDenied('resource_changed')
            return data

    def revoke(self, token):
        with self._lock:
            entry = self._files.pop(token, None)
            if entry:
                os.close(entry[0])

    def close(self):
        with self._lock:
            self._closed = True
            for fd, *_ in self._files.values():
                os.close(fd)
            self._files.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
