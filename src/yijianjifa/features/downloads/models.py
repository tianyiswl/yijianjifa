"""Explicit local models; importing/constructing/requiring never fetches a model."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import threading
from typing import Callable

from .contracts import CancelToken, DownloadError, Progress
from .input import validate_url
from .network import MAX_BYTES, SPACE_MARGIN, SecureFetcher, SourceRegistry

_DEFAULT_NETWORK_SLOTS = threading.BoundedSemaphore(2)
_CHUNK_BYTES = 64 * 1024


def _check_cancel(cancel: CancelToken | None) -> None:
    if cancel is not None and cancel.cancelled():
        raise DownloadError('cancelled')


@contextmanager
def _acquire(slot, cancel: CancelToken | None):
    """The Host supplies the same semaphore to model and media leaf operations."""
    _check_cancel(cancel)
    while not slot.acquire(timeout=.05):
        _check_cancel(cancel)
    try:
        _check_cancel(cancel)
        yield
    finally:
        slot.release()


class ModelStore:
    def __init__(self, root: Path | None = None, *, manifest: dict | None = None,
                 registry: SourceRegistry | None = None,
                 fetcher: SecureFetcher | None = None, network_slots=None,
                 free_space: Callable[[Path], int] | None = None):
        if root is None:
            from platformdirs import user_data_dir
            from yijianjifa import APP_NAME
            data = os.getenv('YJJF_TEST_DATA_DIR') or user_data_dir(APP_NAME, appauthor=False)
            root = Path(data) / 'models'
        self.root = Path(root)
        if manifest is None:
            try:
                manifest = json.loads(files(__package__).joinpath('model_manifest.json').read_text('utf-8'))
            except (OSError, ValueError):
                raise DownloadError('model_invalid') from None
        self._models = self._read_manifest(manifest)
        self.registry = registry or (fetcher.registry if fetcher is not None else SourceRegistry())
        if fetcher is not None and fetcher.registry is not self.registry:
            raise DownloadError('model_invalid')
        self.fetcher = fetcher
        self.network_slots = network_slots if network_slots is not None else _DEFAULT_NETWORK_SLOTS
        self.free_space = free_space or (fetcher.free_space if fetcher is not None else lambda p: shutil.disk_usage(p).free)
        self._locks = {model_id: threading.Lock() for model_id in self._models}
        self._verified: dict[str, tuple[tuple, bool]] = {}
        self._pending_cleanup: set[Path] = set()
        self._cleanup_lock = threading.Lock()

    @staticmethod
    def _read_manifest(manifest: dict) -> dict[str, dict]:
        try:
            if manifest['schema_version'] != 1 or not isinstance(manifest['models'], list):
                raise ValueError
            result, names = {}, set()
            for raw in manifest['models']:
                entry = copy.deepcopy(raw)
                model_id, filename = entry['id'], entry['filename']
                if (type(model_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', model_id)
                        or model_id in result or type(filename) is not str
                        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', filename) or filename in names
                        or type(entry['size_bytes']) is not int or not 0 < entry['size_bytes'] <= MAX_BYTES
                        or type(entry['sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
                        or type(entry['name']) is not str or not entry['name']
                        or type(entry['version']) is not str or not entry['version']
                        or entry['kind'] not in ('asr', 'ocr_det', 'ocr_rec', 'ocr_cls')
                        or entry['license']['spdx'] not in ('MIT', 'Apache-2.0')):
                    raise ValueError
                if validate_url(entry['url']).scheme != 'https':
                    raise ValueError
                result[model_id] = entry
                names.add(filename)
            if not result:
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError, DownloadError):
            raise DownloadError('model_invalid') from None

    def _entry(self, model_id: str) -> dict:
        if type(model_id) is not str or model_id not in self._models:
            raise DownloadError('model_unknown')
        return self._models[model_id]

    @staticmethod
    def _signature(path: Path) -> tuple:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise DownloadError('model_invalid')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _verify(self, path: Path, entry: dict, *, cancel: CancelToken | None = None,
                progress: Callable[[Progress], None] | None = None) -> tuple:
        try:
            _check_cancel(cancel)
            before = self._signature(path)
            if before[2] != entry['size_bytes']:
                raise DownloadError('model_invalid')
            digest, current = hashlib.sha256(), 0
            with path.open('rb') as source:
                while True:
                    _check_cancel(cancel)
                    chunk = source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    current += len(chunk)
                    if current > entry['size_bytes']:
                        raise DownloadError('model_invalid')
                    digest.update(chunk)
                    if progress is not None:
                        progress(Progress('model_verify', current, entry['size_bytes']))
            _check_cancel(cancel)
            after = self._signature(path)
            if (before != after or current != entry['size_bytes']
                    or digest.hexdigest() != entry['sha256']):
                raise DownloadError('model_invalid')
            return after
        except OSError:
            raise DownloadError('model_invalid') from None

    def list(self) -> list[dict]:
        result = []
        for entry in self._models.values():
            model_id = entry['id']
            try:
                self.require(model_id)
                state = 'ready'
            except DownloadError as error:
                state = 'missing' if error.code == 'model_required' else 'invalid'
            result.append({key: copy.deepcopy(entry[key]) for key in
                           ('id', 'name', 'version', 'kind', 'size_bytes', 'license')} | {'state': state})
        return result

    def require(self, model_id: str) -> Path:
        entry = self._entry(model_id)
        path = self.root / entry['filename']
        try:
            signature = self._signature(path)
        except FileNotFoundError:
            raise DownloadError('model_required') from None
        except OSError:
            raise DownloadError('model_invalid') from None
        cached = self._verified.get(model_id)
        # CPython 3.12 on Windows reports creation time as st_ctime_ns.
        # An in-place edit can preserve this signature, so always hash there.
        if sys.platform != 'win32' and cached is not None and cached[0] == signature:
            if not cached[1]:
                raise DownloadError('model_invalid')
            return path
        try:
            self._verify(path, entry)
        except DownloadError:
            self._verified[model_id] = (signature, False)
            raise
        self._verified[model_id] = (signature, True)
        return path

    def _temporary(self, entry: dict) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or self.free_space(self.root) < entry['size_bytes'] + SPACE_MARGIN:
            raise DownloadError('model_invalid' if self.root.is_symlink() else 'disk_space')
        descriptor, name = tempfile.mkstemp(prefix='.model-', suffix='.part', dir=self.root)
        os.close(descriptor)
        return Path(name)

    def pending_cleanup(self) -> tuple[Path, ...]:
        """Backend-only record of this instance's owned, unremoved temporaries.

        This is an in-memory record, not permission to scan or delete the root.
        """
        with self._cleanup_lock:
            return tuple(sorted(self._pending_cleanup))

    def _cleanup_temporary(self, temporary: Path, *, preserve_error: bool) -> None:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            with self._cleanup_lock:
                self._pending_cleanup.add(temporary)
            if not preserve_error:
                raise DownloadError('model_invalid') from None

    def _activate(self, temporary: Path, entry: dict, cancel: CancelToken | None,
                  progress: Callable[[Progress], None] | None) -> None:
        self._verify(temporary, entry, cancel=cancel, progress=progress)
        # Windows _commit (os.fsync) requires a writable descriptor. This is
        # still the owned, verified temporary; publication happens below.
        with temporary.open('r+b') as source:
            os.fsync(source.fileno())
        _check_cancel(cancel)  # Includes cancellation after the final hash/progress callback.
        destination = self.root / entry['filename']
        temporary.replace(destination)
        self._verified[entry['id']] = (self._signature(destination), True)

    def install(self, model_id: str, *, cancel: CancelToken,
                progress: Callable[[Progress], None]) -> None:
        entry = self._entry(model_id)
        temporary = None
        failed = False
        try:
            with _acquire(self._locks[model_id], cancel):
                temporary = self._temporary(entry)
                with _acquire(self.network_slots, cancel):
                    source = self.registry.put(entry['url'], headers={'Accept': 'application/octet-stream'})
                    try:
                        options = {'max_bytes': entry['size_bytes'], 'free_space': self.free_space}
                        if self.fetcher is not None:
                            options.update(transport=self.fetcher.transport, clock=self.fetcher.clock,
                                           max_bytes=min(entry['size_bytes'], self.fetcher.max_bytes))
                        SecureFetcher(self.registry, **options).download(
                            source, temporary, cancel=cancel,
                            progress=lambda event: progress(Progress('model_download', event.current, entry['size_bytes'])))
                    finally:
                        self.registry.revoke(source)
                self._activate(temporary, entry, cancel, progress)
                temporary = None
        except OSError:
            failed = True
            raise DownloadError('model_invalid') from None
        except BaseException:
            failed = True
            raise
        finally:
            if temporary is not None:
                self._cleanup_temporary(temporary, preserve_error=failed)

    def import_file(self, model_id: str, selected: Path, *, cancel: CancelToken | None = None,
                    progress: Callable[[Progress], None] | None = None) -> None:
        entry = self._entry(model_id)
        selected, temporary = Path(selected), None
        failed = False
        try:
            with _acquire(self._locks[model_id], cancel):
                if self._signature(selected)[2] != entry['size_bytes']:
                    raise DownloadError('model_invalid')
                temporary = self._temporary(entry)
                current = 0
                with selected.open('rb') as source, temporary.open('wb') as output:
                    while True:
                        _check_cancel(cancel)
                        chunk = source.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        current += len(chunk)
                        if current > entry['size_bytes']:
                            raise DownloadError('model_invalid')
                        if self.free_space(self.root) < len(chunk) + SPACE_MARGIN:
                            raise DownloadError('disk_space')
                        output.write(chunk)
                        if progress is not None:
                            progress(Progress('model_import', current, entry['size_bytes']))
                self._activate(temporary, entry, cancel, progress)
                temporary = None
        except OSError:
            failed = True
            raise DownloadError('model_invalid') from None
        except BaseException:
            failed = True
            raise
        finally:
            if temporary is not None:
                self._cleanup_temporary(temporary, preserve_error=failed)
