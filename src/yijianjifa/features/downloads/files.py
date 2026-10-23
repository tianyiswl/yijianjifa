"""Backend-owned files. Write grants come from native picks or fixed app cache.

Slow content verification happens outside SQLite write transactions. Settings
hold bounded grant/journal metadata; DownloadOutputRow remains authoritative
for output ownership. No public API accepts these backend paths.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid
import sys
import errno
import ctypes

from sqlalchemy import select, delete, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from yijianjifa.features.library.active_references import ensure_not_referenced
from yijianjifa.features.library.media_storage import media_mutation_lock
from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.storage.models import Setting, MediaAsset, DownloadCandidateRow, DownloadItemRow, DownloadOutputRow, DownloadStageRow, DownloadAuxiliaryJobRow
from .contracts import DownloadError, OutputResource
from .media import MediaProcessor, _run_process

PREFIX = 'download-owned:'
SUFFIX = {'source_video': '.mp4', 'video': '.mp4', 'preview': '.mp4', 'silent_video': '.mp4',
          'audio_mp3': '.mp3', 'audio_wav': '.wav', 'asr': '.txt', 'ocr': '.txt', 'cover': '.img',
          'asr_wav': '.wav', 'subtitle_txt': '.txt', 'subtitle_srt': '.srt', 'subtitle_vtt': '.vtt'}


class _NeverCancel:
    def cancelled(self): return False


def _id(): return uuid.uuid4().hex


def _metadata_prefix(kind):
    namespace = {'current':'access:current','temporary':'access:temporary','area':'access:area','deletion':'access:deletion'}.get(kind,kind)
    return PREFIX + namespace + ':'


def _metadata_key(kind, identity): return _metadata_prefix(kind) + identity


def _check_cancel(cancel):
    if cancel.cancelled(): raise DownloadError('cancelled')


def _identity(value, *, nullable=False):
    return (value is None and nullable) or (type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value) is not None)


def _owned_id(value):
    return type(value) is str and re.fullmatch(r'[a-f0-9]{32}',value) is not None


def _file_identity(value):
    return type(value) is list and len(value)==2 and all(type(n) is int for n in value)


def _safe_name(name):
    return (type(name) is str and re.fullmatch(r'[a-z0-9_-]{1,100}\.[a-z0-9]{1,5}', name) is not None
            and name.split('.')[0].upper() not in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))})


def _safe_final_name(name):
    """Finite native-selected basename; portable names cannot become paths."""
    try: size = len(name.encode('utf-8')) if type(name) is str else 0
    except UnicodeError: return False
    return (type(name) is str and 0 < size <= 240 and name not in ('.', '..')
            and not name.endswith(('.', ' ')) and not any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
            and name.split('.')[0].upper() not in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))})


def _rename_exclusive(source_fd, source_name, destination_fd, destination_name):
    """Atomic directory-entry capture, with no overwrite fallback.

    macOS SDK sys/stdio.h defines RENAME_EXCL=0x4. Linux renameat2 uses
    RENAME_NOREPLACE=0x1. Unsupported libc/filesystems reject finitely.
    """
    libc = ctypes.CDLL(None,use_errno=True)
    function = getattr(libc,'renameatx_np',None) if sys.platform=='darwin' else getattr(libc,'renameat2',None) if sys.platform.startswith('linux') else None
    if function is None: raise DownloadError('unsafe_source')
    function.argtypes = (ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint)
    function.restype = ctypes.c_int
    flag = 4 if sys.platform=='darwin' else 1
    if function(source_fd,os.fsencode(source_name),destination_fd,os.fsencode(destination_name),flag):
        code = ctypes.get_errno()
        if code==errno.EEXIST: raise FileExistsError(code,os.strerror(code))
        raise OSError(code,os.strerror(code))


class _Guard:
    """Pin/check every component. POSIX operations use the pinned directory FD.

    Windows keeps native no-delete-sharing handles and rejects reparse points.
    Its actual behavior must also be verified on a Windows host.
    """
    def __init__(self, path, *, directory=True):
        self.path = Path(path).absolute()
        self.directory = directory
        self.fds, self.handles, self.entries = [], [], []
        self.fd = None
        self.parent_fd = None
        self.expected_stamp = None
        self.deletion_owner = None
        self.deletion_context = None
        self.finalize_delete = None

    def __enter__(self):
        try:
            parts = self.path.parts
            current = Path(parts[0])
            components = [current]
            for part in parts[1:]:
                if part in ('', '.', '..') or any(ord(c) < 32 for c in part): raise DownloadError('unsafe_source')
                current /= part; components.append(current)
            previous = None
            for position, current in enumerate(components):
                is_dir = position < len(components) - 1 or self.directory
                if os.name == 'nt':
                    info = self._windows_open(current, is_dir)
                else:
                    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_dir else os.O_NONBLOCK)
                    fd = os.open(str(current) if previous is None else current.name, flags, dir_fd=previous)
                    self.fds.append(fd); info = os.fstat(fd)
                    self.parent_fd = previous; previous = fd; self.fd = fd
                if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                        or (not stat.S_ISDIR(info.st_mode) if is_dir else not stat.S_ISREG(info.st_mode))):
                    raise DownloadError('unsafe_source')
                self.entries.append((current, (info.st_dev, info.st_ino)))
            self.identity = self.entries[-1][1]
            self.verify()
            return self
        except DownloadError:
            self.__exit__(None, None, None); raise
        except OSError:
            self.__exit__(None, None, None); raise DownloadError('unsafe_source') from None

    def _windows_open(self, path, directory, *, delete_access=False):
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
        kernel.CreateFileW.restype = w.HANDLE
        handle = kernel.CreateFileW(str(path), 0x80000000 | (0x10000 if delete_access else 0), 3, None, 3, 0x00200000 | 0x02000000, None)
        if handle == w.HANDLE(-1).value: raise OSError()
        self.handles.append((kernel, handle))
        class Info(ctypes.Structure):
            _fields_ = [('attrs', w.DWORD), ('created', w.FILETIME), ('accessed', w.FILETIME), ('written', w.FILETIME),
                        ('volume', w.DWORD), ('size_high', w.DWORD), ('size_low', w.DWORD), ('links', w.DWORD),
                        ('index_high', w.DWORD), ('index_low', w.DWORD)]
        kernel.GetFileInformationByHandle.argtypes = (w.HANDLE, ctypes.POINTER(Info))
        kernel.GetFileInformationByHandle.restype = w.BOOL
        info = Info()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)) or info.attrs & 0x400: raise OSError()
        if bool(info.attrs & 0x10) != directory: raise OSError()
        # Python stat IDs are used consistently for durable re-open validation.
        return path.lstat()

    def reader_fd(self):
        if os.name != 'nt': return os.dup(self.fd)
        import ctypes
        import msvcrt
        from ctypes import wintypes as w
        kernel, handle = self.handles[-1]
        kernel.GetCurrentProcess.restype = w.HANDLE
        process = kernel.GetCurrentProcess(); duplicate = w.HANDLE()
        kernel.DuplicateHandle.argtypes = (w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD)
        kernel.DuplicateHandle.restype = w.BOOL
        if not kernel.DuplicateHandle(process,handle,process,ctypes.byref(duplicate),0,False,2):
            raise DownloadError('unsafe_source')
        try: return msvcrt.open_osfhandle(duplicate.value,os.O_RDONLY|os.O_BINARY)
        except OSError: self._close_handle(kernel,duplicate.value); raise DownloadError('unsafe_source') from None

    def file_stamp(self):
        descriptor = self.reader_fd()
        try:
            info = os.fstat(descriptor)
            return info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_nlink
        finally: os.close(descriptor)

    @staticmethod
    def _close_handle(kernel, handle):
        from ctypes import wintypes as w
        kernel.CloseHandle.argtypes = (w.HANDLE,); kernel.CloseHandle.restype = w.BOOL
        kernel.CloseHandle(handle)

    def unlink(self, *, cancel=None):
        cancel = cancel or _NeverCancel(); _check_cancel(cancel)
        if self.directory: raise DownloadError('unsafe_source')
        self.verify()
        if os.name != 'nt':
            if self.deletion_owner is None: raise DownloadError('unsafe_source')
            try: return self.deletion_owner._quarantine_unlink(self,cancel=cancel)
            except OSError: raise DownloadError('unsafe_source') from None
        # Delete the pinned object, not a newly resolved pathname. The native
        # handle holds DELETE access without permitting third-party replacement.
        import ctypes
        from ctypes import wintypes as w
        # Read guards admit ordinary media readers. Upgrade only this final
        # component after readers are revoked; pinned parent handles stay open.
        kernel, handle = self.handles.pop()
        self._close_handle(kernel,handle)
        try:
            info = self._windows_open(self.path,False,delete_access=True)
            if (info.st_dev,info.st_ino) != self.identity: raise DownloadError('identity_mismatch')
        except OSError: raise DownloadError('unsafe_source') from None
        self.verify()
        _check_cancel(cancel)
        kernel, handle = self.handles[-1]
        class Disposition(ctypes.Structure): _fields_ = [('delete', w.BOOL)]
        kernel.SetFileInformationByHandle.argtypes = (w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD)
        kernel.SetFileInformationByHandle.restype = w.BOOL
        info = Disposition(True)
        if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(info), ctypes.sizeof(info)):
            raise DownloadError('unsafe_source')

    def cleanup_owned(self, *, cancel=None):
        """Clean an already pinned owned temp even after its parent was renamed."""
        if os.name == 'nt': return self.unlink(cancel=cancel)
        current = os.stat(self.path.name,dir_fd=self.parent_fd,follow_symlinks=False)
        if (current.st_dev,current.st_ino) != self.identity or not stat.S_ISREG(current.st_mode):
            raise DownloadError('identity_mismatch')
        if self.deletion_owner is None: raise DownloadError('unsafe_source')
        try: return self.deletion_owner._quarantine_unlink(self,cancel=cancel)
        except OSError: raise DownloadError('unsafe_source') from None

    def verify(self):
        try:
            for path, identity in self.entries:
                info = path.lstat()
                if ((info.st_dev, info.st_ino) != identity or stat.S_ISLNK(info.st_mode)
                        or getattr(info, 'st_file_attributes', 0) & 0x400): raise DownloadError('identity_mismatch')
            if self.expected_stamp is not None and self.file_stamp() != self.expected_stamp:
                raise DownloadError('identity_mismatch')
        except OSError: raise DownloadError('identity_mismatch') from None

    def __exit__(self, *args):
        for fd in reversed(self.fds): os.close(fd)
        self.fds.clear()
        for kernel, handle in reversed(self.handles):
            self._close_handle(kernel,handle)
        self.handles.clear()


@dataclass(frozen=True)
class _BoundResource:
    resource: OutputResource
    journal: dict
    root: _Guard
    guard: _Guard

    @property
    def path(self): return self.guard.path

    def verify(self):
        self.root.verify(); self.guard.verify()


@dataclass(frozen=True)
class _BoundTemporary:
    identity: str
    allocation: dict
    root: _Guard
    area: _Guard
    guard: _Guard


class OwnedFiles:
    def __init__(self, repository, paths, *, clock=time.time, grant_ttl=30 * 86400):
        if type(grant_ttl) not in (int, float) or not math.isfinite(grant_ttl) or not 0 < grant_ttl <= 365 * 86400:
            raise DownloadError('input_limit')
        self.repository, self.engine, self.paths = repository, repository.engine, paths
        self.clock, self.grant_ttl = clock, grant_ttl
        self.lock = media_mutation_lock(self.engine)
        self._revoke_callbacks = []

    def _read(self, kind, identity, *, connection=None):
        if type(identity) is not str or not re.fullmatch(r'[a-z0-9_:-]{1,128}', identity): raise DownloadError('source_missing')
        if connection is None:
            with self.engine.connect() as connection: return self._read(kind, identity, connection=connection)
        raw = connection.execute(select(Setting.value_json).where(Setting.key == _metadata_key(kind,identity))).scalar_one_or_none()
        if raw is None: raise DownloadError('source_missing')
        try:
            if len(raw.encode()) > 8192: raise ValueError()
            result = json.loads(raw)
            if type(result) is not dict or type(result.get('v')) is not int or result['v'] != 1: raise ValueError()
            return result
        except (ValueError, TypeError): raise DownloadError('metadata_missing') from None

    def _save(self, connection, kind, identity, value):
        encoded = json.dumps(value, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode()) > 8192: raise DownloadError('input_limit')
        key = _metadata_key(kind,identity)
        statement = sqlite_insert(Setting).values(key=key, value_json=encoded)
        connection.execute(statement.on_conflict_do_update(index_elements=['key'], set_={'value_json': encoded}))

    def _values(self, kind):
        with self.engine.connect() as connection:
            keys = connection.execute(select(Setting.key).where(Setting.key.like(_metadata_prefix(kind)+'%'))).scalars().all()
        return [(key.removeprefix(_metadata_prefix(kind)), self._read(kind, key.removeprefix(_metadata_prefix(kind)))) for key in keys]

    def choose_output(self, native_directory):
        return self._choose_output(native_directory, set_current=True)

    def cover_cache_grant(self):
        return self._cache_grant('cover')

    def local_cache_grant(self):
        return self._cache_grant('local')

    def renew_cache_grants(self):
        """Refresh existing fixed-cache grants through the same metadata keys.

        Startup must not mint a replacement grant or adopt a changed directory.
        _cache_grant verifies the persisted path/identity before renewing TTL.
        """
        with self.lock:
            for category in ('cover','local'):
                try:self._read('current',category)
                except DownloadError as error:
                    if error.code=='source_missing':continue
                    raise
                self._cache_grant(category)

    def _cache_grant(self,category):
        """Fixed backend cache; refresh only the persisted original identity."""
        path = Path(self.paths.cache_dir).absolute()
        with self.lock:
            try: current = self._read('current',category)
            except DownloadError as error:
                if error.code != 'source_missing': raise
                result = self._choose_output(path,set_current=False)
                with self.engine.begin() as connection:
                    self._save(connection,'current',category,{'v':1,'grant_id':result['grant_id']})
                return result['grant_id']
            if set(current) != {'v','grant_id'} or not _owned_id(current['grant_id']):
                raise DownloadError('metadata_missing')
            identity = current['grant_id']; grant = self._read('grant',identity)
            self._grant_metadata(grant)
            if grant['path'] != str(path): raise DownloadError('identity_mismatch')
            with _Guard(path) as guard:
                if list(guard.identity) != grant['identity']: raise DownloadError('identity_mismatch')
                if self.clock() >= grant['expires']:
                    renewed = {**grant,'expires':self.clock()+self.grant_ttl}
                    with self.engine.begin() as connection:
                        guard.verify(); self._save(connection,'grant',identity,renewed)
                guard.verify()
            return identity

    @staticmethod
    def _grant_metadata(value):
        if (set(value) != {'v', 'path', 'identity', 'expires'} or type(value['path']) is not str
                or not Path(value['path']).is_absolute() or len(value['path']) > 4000
                or type(value['identity']) is not list or len(value['identity']) != 2
                or any(type(n) is not int for n in value['identity'])
                or type(value['expires']) not in (int, float) or not math.isfinite(value['expires'])):
            raise DownloadError('metadata_missing')

    def _choose_output(self, native_directory, *, set_current):
        path = Path(native_directory).absolute()
        if len(str(path)) > 4000: raise DownloadError('input_limit')
        with self.lock, _Guard(path) as guard:
            grants = self._values('grant')
            for _, value in grants: self._grant_metadata(value)
            identity = next((key for key, value in grants if value['path'] == str(path) and value['identity'] == list(guard.identity)), None)
            if identity is None:
                if len(grants) >= 32: raise DownloadError('input_limit')
                identity = _id()
            value = {'v': 1, 'path': str(path), 'identity': list(guard.identity), 'expires': self.clock() + self.grant_ttl}
            with self.engine.begin() as connection:
                self._save(connection, 'grant', identity, value)
                if set_current: self._save(connection, 'current', 'output', {'v': 1, 'grant_id': identity})
            guard.verify()
            return {'grant_id': identity, 'display_path': str(path)}

    def read_output(self):
        try:
            current = self._read('current', 'output')
            if set(current) != {'v','grant_id'} or not _owned_id(current['grant_id']): raise DownloadError('metadata_missing')
            identity = current['grant_id']
        except DownloadError as exc:
            if exc.code == 'source_missing': return {'grant_id': None, 'display_path': None, 'needs_selection': True}
            raise
        with self._grant(identity) as guard:
            return {'grant_id': identity, 'display_path': str(guard.path)}

    @contextmanager
    def _grant(self, identity):
        value = self._read('grant', identity)
        self._grant_metadata(value)
        if self.clock() >= value['expires']: raise DownloadError('source_missing')
        with _Guard(value['path']) as guard:
            if list(guard.identity) != value['identity']: raise DownloadError('identity_mismatch')
            yield guard
            guard.verify()

    def _candidate(self, candidate_id, item_id=None):
        with self.engine.connect() as connection:
            self.repository._ownership(connection, candidate_id, item_id)

    def _bind_delete_guard(self, guard, *, grant_id, candidate_id, item_id, resource_id=None, temporary_id=None, parent_kind='temporary'):
        guard.deletion_owner = self
        guard.deletion_context = {'grant_id':grant_id,'candidate_id':candidate_id,'item_id':item_id,
            'resource_id':resource_id,'temporary_id':temporary_id,'parent_kind':parent_kind,
            'parent_identity':list(guard.entries[-2][1])}
        return guard

    def _drop_deletion(self, connection, identity):
        connection.execute(delete(Setting).where(Setting.key==_metadata_key('deletion',identity)))

    def _deletion(self, identity):
        record = self._read('deletion',identity)
        fields = {'v','id','state','grant_id','candidate_id','item_id','resource_id','temporary_id','parent_kind',
            'parent_identity','source_name','quarantine_name','quarantine_identity','expected_identity',
            'expected_sha256','expected_size','observed_identity','created'}
        if (set(record)!=fields or not _owned_id(identity) or record['id']!=identity or not _owned_id(record['grant_id'])
                or not _identity(record['candidate_id']) or not _identity(record['item_id'],nullable=True)
                or record['resource_id'] is not None and not _owned_id(record['resource_id'])
                or record['temporary_id'] is not None and not _owned_id(record['temporary_id'])
                or record['state'] not in ('prepared','confirmed','retained','missing')
                or record['parent_kind'] not in ('output','temporary') or not _safe_final_name(record['source_name'])
                or record['quarantine_name']!='.p011-delete-'+identity or not _file_identity(record['parent_identity'])
                or not _file_identity(record['quarantine_identity']) or not _file_identity(record['expected_identity'])
                or record['observed_identity'] is not None and not _file_identity(record['observed_identity'])
                or type(record['expected_sha256']) is not str or not re.fullmatch(r'[a-f0-9]{64}',record['expected_sha256'])
                or type(record['expected_size']) is not int or record['expected_size']<0
                or type(record['created']) not in (int,float) or not math.isfinite(record['created'])
                or record['parent_kind']=='output' and (record['resource_id'] is None or record['temporary_id'] is not None)
                or record['parent_kind']=='temporary' and (record['temporary_id'] is None or record['resource_id'] is not None)):
            raise DownloadError('metadata_missing')
        return record

    def recover_deletions(self, *, cancel=None):
        """Restore captured objects with no overwrite; never infer delete success."""
        cancel = cancel or _NeverCancel(); restored = 0
        with self.lock:
            for identity,_ in self._values('deletion'):
                _check_cancel(cancel); record = self._deletion(identity)
                self._candidate(record['candidate_id'],record['item_id'])
                with self._grant(record['grant_id']) as root:
                    if record['parent_kind']=='temporary':
                        parent_context = self._area(root,record['grant_id'],expected_identity=record['parent_identity'])
                    else:
                        @contextmanager
                        def output_parent():
                            if list(root.identity)!=record['parent_identity']: raise DownloadError('identity_mismatch')
                            yield root
                        parent_context = output_parent()
                    with parent_context as parent:
                        with _Guard(parent.path/record['quarantine_name']) as quarantine:
                            if list(quarantine.identity)!=record['quarantine_identity']: raise DownloadError('identity_mismatch')
                            private = os.fstat(quarantine.fd)
                            if stat.S_IMODE(private.st_mode)!=0o700 or private.st_uid!=os.geteuid():
                                raise DownloadError('unsafe_source')
                            try: os.stat('object',dir_fd=quarantine.fd,follow_symlinks=False)
                            except FileNotFoundError:
                                try: info = os.stat(record['source_name'],dir_fd=parent.fd,follow_symlinks=False)
                                except FileNotFoundError: info = None
                                expected = record['observed_identity'] or record['expected_identity']
                                _check_cancel(cancel)
                                if info is not None and [info.st_dev,info.st_ino]==expected:
                                    with self.engine.begin() as connection:
                                        self._drop_deletion(connection,identity); _check_cancel(cancel)
                                    self._qempty(parent.fd,record['quarantine_name'],record['quarantine_identity'])
                                else:
                                    record['state']='missing'
                                    with self.engine.begin() as connection:
                                        self._save(connection,'deletion',identity,record); _check_cancel(cancel)
                                continue
                            parent.verify(); quarantine.verify(); _check_cancel(cancel)
                            if self._restore_quarantine(parent.fd,quarantine.fd,record,cancel=cancel): restored += 1
        return restored

    @staticmethod
    def _digest_pinned(descriptor, cancel=None):
        cancel = cancel or _NeverCancel(); _check_cancel(cancel)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink not in (1,2): raise DownloadError('identity_mismatch')
        digest = hashlib.sha256(); os.lseek(descriptor,0,os.SEEK_SET)
        try:
            while chunk := os.read(descriptor,1024*1024):
                _check_cancel(cancel); digest.update(chunk)
            _check_cancel(cancel)
            after = os.fstat(descriptor)
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_nlink) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink):
                raise DownloadError('identity_mismatch')
            return digest.hexdigest(),before.st_size,(after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink)
        finally: os.lseek(descriptor,0,os.SEEK_SET)

    @staticmethod
    def _qempty(parent_fd, name, identity):
        try:
            info = os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
            if [info.st_dev,info.st_ino] != identity or not stat.S_ISDIR(info.st_mode): return
            os.rmdir(name,dir_fd=parent_fd)
        except OSError: pass

    def _restore_quarantine(self, parent_fd, quarantine_fd, record, *, cancel=None):
        """Move captured data back without deleting or overwriting any object."""
        cancel = cancel or _NeverCancel(); _check_cancel(cancel)
        try:
            info = os.stat('object',dir_fd=quarantine_fd,follow_symlinks=False)
            observed = [info.st_dev,info.st_ino]
            if record['observed_identity'] is not None and observed != record['observed_identity']:
                raise DownloadError('identity_mismatch')
            record['observed_identity'] = observed
            _check_cancel(cancel)
            _rename_exclusive(quarantine_fd,'object',parent_fd,record['source_name'])
            os.fsync(parent_fd); os.fsync(quarantine_fd)
            restored = os.stat(record['source_name'],dir_fd=parent_fd,follow_symlinks=False)
            if [restored.st_dev,restored.st_ino] != observed: raise DownloadError('identity_mismatch')
            _check_cancel(cancel)
            with self.engine.begin() as connection:
                self._drop_deletion(connection,record['id']); _check_cancel(cancel)
            self._qempty(parent_fd,record['quarantine_name'],record['quarantine_identity'])
            return True
        except FileExistsError:
            record['state'] = 'retained'
        except FileNotFoundError:
            record['state'] = 'missing'
        except OSError:
            record['state'] = 'retained'
        _check_cancel(cancel)
        with self.engine.begin() as connection:
            self._save(connection,'deletion',record['id'],record); _check_cancel(cancel)
        return False

    def _quarantine_unlink(self, guard, *, cancel=None):
        """Capture a public entry atomically; only delete a verified private one.

        The fresh 0700 directory is unpredictable, backend-owned, and never
        issued as a frontend grant. A swapped public entry is restored or kept.
        """
        cancel = cancel or _NeverCancel(); _check_cancel(cancel)
        if len(self._values('deletion')) >= 64: raise DownloadError('input_limit')
        context = guard.deletion_context
        if context is None or guard.parent_fd is None: raise DownloadError('unsafe_source')
        parent = os.fstat(guard.parent_fd)
        if [parent.st_dev,parent.st_ino] != context['parent_identity']: raise DownloadError('identity_mismatch')
        # This check is preparatory only. Atomic capture and object comparison
        # below handle a replacement after this check.
        os.stat(guard.path.name,dir_fd=guard.parent_fd,follow_symlinks=False)
        descriptor = guard.reader_fd()
        try: sha,size,stamp = self._digest_pinned(descriptor,cancel)
        finally: os.close(descriptor)
        if guard.expected_stamp is not None and stamp!=guard.expected_stamp:
            raise DownloadError('identity_mismatch')
        identity = _id(); name = '.p011-delete-' + identity
        try: os.mkdir(name,mode=0o700,dir_fd=guard.parent_fd)
        except OSError: raise DownloadError('unsafe_source') from None
        quarantine_fd = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=guard.parent_fd)
        info = os.fstat(quarantine_fd)
        if stat.S_IMODE(info.st_mode)!=0o700 or info.st_uid!=os.geteuid():
            os.close(quarantine_fd); raise DownloadError('unsafe_source')
        record = {'v':1,'id':identity,'state':'prepared',**context,'source_name':guard.path.name,
            'quarantine_name':name,'quarantine_identity':[info.st_dev,info.st_ino],
            'expected_identity':list(guard.identity),'expected_sha256':sha,'expected_size':size,
            'observed_identity':None,'created':self.clock()}
        captured = False
        try:
            os.fsync(guard.parent_fd)
            _check_cancel(cancel)
            with self.engine.begin() as connection:
                self._save(connection,'deletion',identity,record); _check_cancel(cancel)
            _check_cancel(cancel)
            _rename_exclusive(guard.parent_fd,guard.path.name,quarantine_fd,'object')
            captured = True; os.fsync(guard.parent_fd); os.fsync(quarantine_fd)
            info = os.stat('object',dir_fd=quarantine_fd,follow_symlinks=False)
            record['observed_identity'] = [info.st_dev,info.st_ino]
            if record['observed_identity'] != list(guard.identity) or not stat.S_ISREG(info.st_mode):
                raise DownloadError('identity_mismatch')
            actual_fd = os.open('object',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=quarantine_fd)
            try:
                actual = os.fstat(actual_fd)
                if (actual.st_dev,actual.st_ino) != guard.identity: raise DownloadError('identity_mismatch')
                checked_sha,checked_size,stamp = self._digest_pinned(actual_fd,cancel)
                if (checked_sha,checked_size) != (sha,size): raise DownloadError('identity_mismatch')
                record['state'] = 'confirmed'
                _check_cancel(cancel)
                with self.engine.begin() as connection:
                    self._save(connection,'deletion',identity,record); _check_cancel(cancel)
                def remove():
                    _check_cancel(cancel)
                    current = os.stat('object',dir_fd=quarantine_fd,follow_symlinks=False)
                    pinned = os.fstat(actual_fd)
                    if ((current.st_dev,current.st_ino) != guard.identity
                            or (pinned.st_dev,pinned.st_ino) != guard.identity
                            or (pinned.st_size,pinned.st_mtime_ns,pinned.st_ctime_ns,pinned.st_nlink) != stamp):
                        raise DownloadError('identity_mismatch')
                    os.unlink('object',dir_fd=quarantine_fd); os.fsync(quarantine_fd)
                if guard.finalize_delete is not None: guard.finalize_delete(remove,record)
                else:
                    remove()
                    with self.engine.begin() as connection: self._drop_deletion(connection,identity)
            finally: os.close(actual_fd)
            self._qempty(guard.parent_fd,name,record['quarantine_identity'])
        except Exception:
            if captured:
                self._restore_quarantine(guard.parent_fd,quarantine_fd,record)
            else:
                with self.engine.begin() as connection: self._drop_deletion(connection,identity)
                self._qempty(guard.parent_fd,name,record['quarantine_identity'])
            raise
        finally: os.close(quarantine_fd)

    @contextmanager
    def _area(self, root, grant_id, *, create=False, expected_identity=None):
        name = '.yijianjifa-download-' + grant_id
        try: value = self._read('area',grant_id)
        except DownloadError as exc:
            if exc.code != 'source_missing' or not create: raise
            try:
                if os.name=='nt': (root.path/name).mkdir(mode=0o700)
                else: os.mkdir(name,mode=0o700,dir_fd=root.fd)
            except OSError: raise DownloadError('identity_mismatch') from None
            with _Guard(root.path/name) as fresh:
                if fresh.identity[0] != root.identity[0]: raise DownloadError('unsafe_source')
                value = {'v':1,'grant_id':grant_id,'area':name,'identity':list(fresh.identity)}
                with self.engine.begin() as connection: self._save(connection,'area',grant_id,value)
        if (set(value) != {'v','grant_id','area','identity'} or value['grant_id'] != grant_id
                or value['area'] != name or not _file_identity(value['identity'])
                or expected_identity is not None and value['identity'] != expected_identity):
            raise DownloadError('identity_mismatch')
        with _Guard(root.path/name) as area:
            if list(area.identity) != value['identity'] or area.identity[0] != root.identity[0]:
                raise DownloadError('identity_mismatch')
            yield area
            area.verify(); root.verify()

    def _temporary_area(self, root, allocation):
        return self._area(root,allocation['grant_id'],expected_identity=allocation['area_identity'])

    def allocate(self, candidate_id, kind, *, item_id=None, grant_id=None):
        if kind not in SUFFIX: raise DownloadError('unsupported_source')
        self._candidate(candidate_id, item_id)
        grant_id = grant_id or self._read('current', 'output')['grant_id']
        with self.lock, self._grant(grant_id) as root:
            self.reconcile_allocations()
            if len(self._values('temporary')) >= 512: raise DownloadError('input_limit')
            area = '.yijianjifa-download-' + grant_id
            with self._area(root,grant_id,create=True) as temporary:
                identity = _id(); name = 'temporary-' + identity + SUFFIX[kind]
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                descriptor = os.open(temporary.path / name if os.name == 'nt' else name, flags, 0o600,
                                     **({} if os.name == 'nt' else {'dir_fd': temporary.fd}))
                info = os.fstat(descriptor); os.close(descriptor)
                value = {'v': 1, 'grant_id': grant_id, 'area': area, 'name': name, 'candidate_id': candidate_id,
                         'item_id': item_id, 'kind': kind, 'created': self.clock(),
                         'area_identity':list(temporary.identity),'file_identity':[info.st_dev,info.st_ino]}
                with self.engine.begin() as connection: self._save(connection, 'temporary', identity, value)
                return temporary.path / name

    def temporary_for(self, source, kind):
        kinds = {'mp3': 'audio_mp3', 'wav': 'audio_wav', 'silent': 'silent_video', 'asr_wav': 'asr_wav'}
        if kind not in kinds or self.repository.get_output(source.id) != source: raise DownloadError('identity_mismatch')
        self.path_for(source.id)
        journal = self._journal(source.id)
        return self.allocate(source.candidate_id, kinds[kind], item_id=source.item_id, grant_id=journal['grant_id'])

    def _temporary(self, path):
        path = Path(path).absolute()
        match = re.fullmatch(r'temporary-([a-f0-9]{32})\.[a-z0-9]{1,5}', path.name)
        if not match: raise DownloadError('unsafe_source')
        identity = match[1]; value = self._allocation(identity,self._read('temporary',identity))
        if value['name'] != path.name: raise DownloadError('metadata_missing')
        return identity,value

    @staticmethod
    def _allocation(identity,value):
        if (set(value) != {'v', 'grant_id', 'area', 'name', 'candidate_id', 'item_id', 'kind', 'created','area_identity','file_identity'}
                or not _owned_id(identity)
                or not _owned_id(value['grant_id']) or not _identity(value['candidate_id']) or not _identity(value['item_id'],nullable=True)
                or not _file_identity(value['area_identity']) or not _file_identity(value['file_identity'])
                or type(value['created']) not in (int,float) or not math.isfinite(value['created'])
                or type(value['kind']) is not str
                or value['area'] != '.yijianjifa-download-' + value['grant_id'] or not _safe_name(value['name'])
                or value['kind'] not in SUFFIX or value['name'] != 'temporary-'+identity+SUFFIX[value['kind']]):
            raise DownloadError('metadata_missing')
        return value

    def reconcile_allocations(self, *, cancel=None):
        """Forget only absent files in identity-verified owned areas.

        MediaProcessor removes failed/cancelled outputs before returning an
        error. Its owner may therefore have no Path handed back for release.
        Present files and completed output receipts are never cleaned here.
        """
        count = 0
        cancel = cancel or _NeverCancel(); _check_cancel(cancel)
        with self.lock:
            for tid,value in self._values('temporary'):
                _check_cancel(cancel)
                allocation = self._allocation(tid,value)
                self._candidate(allocation['candidate_id'],allocation['item_id'])
                with self._grant(allocation['grant_id']) as root, self._temporary_area(root,allocation) as area:
                    path = area.path/allocation['name']
                    try:
                        if os.name=='nt': path.lstat()
                        else: os.stat(path.name,dir_fd=area.fd,follow_symlinks=False)
                    except FileNotFoundError:
                        area.verify(); root.verify()
                        _check_cancel(cancel)
                        with self.engine.begin() as connection:
                            if self._read('temporary',tid,connection=connection) != allocation:
                                raise DownloadError('identity_mismatch')
                            self.repository._ownership(connection,allocation['candidate_id'],allocation['item_id'])
                            connection.execute(delete(Setting).where(Setting.key==_metadata_key('temporary',tid)))
                            _check_cancel(cancel)
                        count += 1
        return count

    @contextmanager
    def open_temporary(self, temporary, *, accept_writer=False):
        with self.lock:
            tid,allocation = self._temporary(temporary)
            self._candidate(allocation['candidate_id'],allocation['item_id'])
            with self._grant(allocation['grant_id']) as root, self._temporary_area(root,allocation) as area:
                path = area.path/allocation['name']
                if Path(temporary).absolute() != path: raise DownloadError('unsafe_source')
                with _Guard(path,directory=False) as guard:
                    if not accept_writer and list(guard.identity) != allocation['file_identity']:
                        raise DownloadError('identity_mismatch')
                    descriptor = guard.reader_fd()
                    try: info = os.fstat(descriptor)
                    finally: os.close(descriptor)
                    if info.st_nlink != 1: raise DownloadError('identity_mismatch')
                    self._bind_delete_guard(guard,grant_id=allocation['grant_id'],candidate_id=allocation['candidate_id'],
                        item_id=allocation['item_id'],temporary_id=tid)
                    yield _BoundTemporary(tid,allocation,root,area,guard)

    def confirm_temporary(self, temporary, cancel=None):
        """Backend-only completion handoff from a successful trusted writer.

        The caller must be the download runtime after StreamDownloader or
        MediaProcessor returned successfully. This is not format acceptance.
        """
        cancel = cancel or _NeverCancel(); _check_cancel(cancel)
        with self.open_temporary(temporary,accept_writer=True) as bound:
            bound.guard.verify(); bound.area.verify(); bound.root.verify()
            _check_cancel(cancel)
            bound.allocation['file_identity'] = list(bound.guard.identity)
            with self.engine.begin() as connection:
                self._save(connection,'temporary',bound.identity,bound.allocation)

    def _hash(self, guard, cancel, *, links=1):
        descriptor = guard.reader_fd()
        try:
            result = self._hash_fd(descriptor,guard.identity,cancel,links=links)
            guard.verify()
            return result
        finally: os.close(descriptor)

    @staticmethod
    def _hash_fd(descriptor, identity, cancel, *, links=1):
        before = os.fstat(descriptor)
        if before.st_nlink != links or before.st_size <= 0 or (before.st_dev,before.st_ino) != identity:
            raise DownloadError('identity_mismatch')
        digest = hashlib.sha256(); os.lseek(descriptor,0,os.SEEK_SET)
        try:
            while data := os.read(descriptor,1024*1024):
                _check_cancel(cancel); digest.update(data)
            after = os.fstat(descriptor)
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise DownloadError('identity_mismatch')
            return digest.hexdigest(),before.st_size,list(identity)
        finally: os.lseek(descriptor,0,os.SEEK_SET)

    def verify_read_fd(self, bound, descriptor, *, cancel=None):
        cancel = cancel or _NeverCancel()
        resource = bound.resource
        expected = (resource.sha256,resource.size_bytes,bound.journal['identity'])
        if self._hash_fd(descriptor,tuple(bound.journal['identity']),cancel) != expected:
            raise DownloadError('identity_mismatch')
        bound.verify()

    def _validate(self, path, kind, cancel):
        if kind in ('asr', 'ocr', 'subtitle_txt', 'subtitle_srt', 'subtitle_vtt'):
            try:
                # SubtitleStore permits a 32 MiB source document. The physical
                # export ceiling includes timestamp/sequence formatting overhead.
                ceiling = 64 * 1024 * 1024 if kind.startswith('subtitle_') else 16 * 1024 * 1024
                if not 0 < path.stat().st_size <= ceiling: raise ValueError()
                text = path.read_text(encoding='utf-8')
                if '\x00' in text: raise ValueError()
                if kind in ('subtitle_srt','subtitle_vtt'): self._validate_subtitle(text,kind)
                return None, SUFFIX[kind]
            except (ValueError, OSError): raise DownloadError('media_invalid') from None
        processor = MediaProcessor(self)
        deadline = time.monotonic() + 30
        if kind == 'cover':
            if path.stat().st_size > 16 * 1024 * 1024: raise DownloadError('media_invalid')
            with path.open('rb') as stream: header = stream.read(16)
            ext = '.png' if header.startswith(b'\x89PNG\r\n\x1a\n') else '.jpg' if header.startswith(b'\xff\xd8') else '.webp' if header[:4] == b'RIFF' and header[8:12] == b'WEBP' else None
            if ext is None or path.stat().st_size > 16 * 1024 * 1024: raise DownloadError('media_invalid')
            args = ('-v','error','-protocol_whitelist','file,pipe','-f','image2pipe','-show_streams','-of','json',str(path))
            raw = _run_process(processor.ffprobe, args, cancel=cancel, deadline=deadline, grace=.2, failure_code='media_invalid')
            try:
                streams = json.loads(raw)['streams']
                if len(streams) != 1 or streams[0]['codec_name'] not in ('png','mjpeg','webp') or not 0 < streams[0]['width'] <= 16384 or not 0 < streams[0]['height'] <= 16384: raise ValueError()
            except (ValueError, KeyError, TypeError): raise DownloadError('media_invalid') from None
            _run_process(processor.ffmpeg, ('-nostdin','-v','error','-protocol_whitelist','file,pipe','-f','image2pipe','-i',str(path),'-frames:v','1','-f','null','-'), cancel=cancel, deadline=deadline, grace=.2, failure_code='media_invalid')
            return None, ext
        output_kind = 'mp3' if kind == 'audio_mp3' else 'wav' if kind in ('audio_wav','asr_wav') else None
        if output_kind is None: processor._verify_mp4_header(path)
        info = processor._probe(path, cancel, deadline, source=output_kind is None, output_kind=output_kind)
        audio, video = info.stream('audio'), info.stream('video')
        if (output_kind and (not audio or video) or not output_kind and not video
                or kind == 'silent_video' and audio
                or kind == 'asr_wav' and (audio.get('sample_rate') != '16000' or audio.get('channels') != 1 or audio.get('codec_name') != 'pcm_s16le')):
            raise DownloadError('media_invalid')
        return info.duration_ms, SUFFIX[kind]

    @staticmethod
    def _validate_subtitle(text,kind):
        sep = ',' if kind=='subtitle_srt' else '.'
        if kind=='subtitle_vtt':
            if not text.startswith('WEBVTT\n\n'): raise ValueError()
            text = text[8:]
        blocks = text.strip().split('\n\n')
        previous = -1
        pattern = r'(\d{2,6}):([0-5]\d):([0-5]\d)' + re.escape(sep) + r'(\d{3})'
        for index,block in enumerate(blocks,1):
            lines = block.splitlines()
            if kind=='subtitle_srt':
                if not lines or lines.pop(0)!=str(index): raise ValueError()
            if len(lines)<2: raise ValueError()
            matched = re.fullmatch(pattern+r' --> '+pattern,lines[0])
            if not matched: raise ValueError()
            nums = tuple(map(int,matched.groups()))
            start = ((nums[0]*60+nums[1])*60+nums[2])*1000+nums[3]
            end = ((nums[4]*60+nums[5])*60+nums[6])*1000+nums[7]
            if start<previous or end<=start or not '\n'.join(lines[1:]).strip(): raise ValueError()
            previous = start

    def promote(self, candidate_id, temporary, kind, *, item_id=None, cancel=None):
        return self._promote(candidate_id, temporary, kind, item_id=item_id, cancel=cancel)

    def _promote(self, candidate_id, temporary, kind, *, item_id=None, cancel=None, native_name=None):
        cancel = cancel or _NeverCancel(); _check_cancel(cancel)
        if native_name is not None and (kind not in ('subtitle_txt','subtitle_srt','subtitle_vtt')
                or not _safe_final_name(native_name) or Path(native_name).suffix.lower() != SUFFIX[kind]):
            raise DownloadError('unsafe_source')
        with self.lock:
            temp_id, allocation = self._temporary(temporary)
            if (allocation['candidate_id'], allocation['item_id'], allocation['kind']) != (candidate_id, item_id, kind) or kind == 'asr_wav':
                raise DownloadError('identity_mismatch')
            self._candidate(candidate_id, item_id)
            with self._grant(allocation['grant_id']) as root, self._temporary_area(root,allocation) as area:
                path = root.path / allocation['area'] / allocation['name']
                if Path(temporary).absolute() != path: raise DownloadError('unsafe_source')
                with _Guard(path, directory=False) as source:
                    if list(source.identity) != allocation['file_identity']: raise DownloadError('identity_mismatch')
                    self._bind_delete_guard(source,grant_id=allocation['grant_id'],candidate_id=candidate_id,item_id=item_id,temporary_id=temp_id)
                    sha, size, identity = self._hash(source, cancel)
                    duration, suffix = self._validate(path, kind, cancel)
                    if self._hash(source, cancel) != (sha, size, identity): raise DownloadError('identity_mismatch')
                    resource = OutputResource(_id(), candidate_id, item_id, kind, sha, size, duration)
                    name = native_name if native_name is not None else kind + '-' + resource.id + suffix
                    journal = {'v': 1, 'state': 'prepared', 'grant_id': allocation['grant_id'], 'area': allocation['area'],
                               'area_identity':allocation['area_identity'],
                               'temporary_name': allocation['name'], 'final_name': name, 'identity': identity,
                               'resource': asdict(resource), 'last_access': self.clock()}
                    with self.engine.begin() as connection: self._save(connection, 'journal', resource.id, journal)
                    source.verify(); root.verify(); _check_cancel(cancel)
                    try:
                        if os.name == 'nt': os.link(path, root.path / name, follow_symlinks=False)
                        else: os.link(path.name, name, src_dir_fd=source.parent_fd, dst_dir_fd=root.fd, follow_symlinks=False)
                    except FileExistsError: raise DownloadError('identity_mismatch') from None
                    except OSError: raise DownloadError('unsafe_source') from None
                    source.verify(); root.verify()
                    source.unlink()
                    if os.name != 'nt': os.fsync(root.fd); os.fsync(source.parent_fd)
                journal['state'] = 'committed'
                with _Guard(root.path/name,directory=False) as published:
                    if self._hash(published,cancel) != (sha,size,identity): raise DownloadError('identity_mismatch')
                    with self.engine.begin() as connection:
                        published.verify(); root.verify()
                        self.repository.save_output(connection, resource, backend_path=str(root.path / name))
                        self._save(connection, 'journal', resource.id, journal)
                        connection.execute(delete(Setting).where(Setting.key == _metadata_key('temporary',temp_id)))
                return resource

    def _journal(self, resource_id):
        value = self._read('journal', resource_id)
        if (set(value) != {'v','state','grant_id','area','area_identity','temporary_name','final_name','identity','resource','last_access'}
                or not _owned_id(value['grant_id'])
                or not _file_identity(value['area_identity'])
                or value['state'] not in ('prepared','committed','deleted') or not _safe_final_name(value['final_name'])
                or not _safe_name(value['temporary_name']) or value['area'] != '.yijianjifa-download-' + value['grant_id']
                or type(value['identity']) is not list or len(value['identity']) != 2
                or any(type(v) is not int for v in value['identity']) or type(value['last_access']) not in (int,float)
                or not math.isfinite(value['last_access'])): raise DownloadError('metadata_missing')
        try:
            if type(value['resource']) is not dict or set(value['resource']) != set(OutputResource.__dataclass_fields__): raise ValueError()
            resource = OutputResource(**value['resource'])
            if (resource.id != resource_id or not _owned_id(resource.id) or not _identity(resource.candidate_id)
                    or not _identity(resource.item_id,nullable=True) or type(resource.kind) is not str or resource.kind not in SUFFIX or resource.kind=='asr_wav'
                    or type(resource.sha256) is not str or not re.fullmatch(r'[a-f0-9]{64}',resource.sha256)
                    or type(resource.size_bytes) is not int or resource.size_bytes <= 0
                    or resource.duration_ms is not None and (type(resource.duration_ms) is not int or resource.duration_ms <= 0)
                    or Path(value['final_name']).suffix.lower() not in ({'.png','.jpg','.webp'} if resource.kind=='cover' else {SUFFIX[resource.kind]})):
                raise ValueError()
        except (TypeError, ValueError): raise DownloadError('metadata_missing') from None
        return value

    @contextmanager
    def open_resource(self, resource_id, *, cancel=None, touch=True):
        """Retain the verified object through the caller's operation.

        path_for remains the media processor's legacy path handoff. Operations
        which grant, reveal, register or delete use this binding instead.
        """
        cancel = cancel or _NeverCancel()
        with self.lock:
            resource = self.repository.get_output(resource_id); journal = self._journal(resource_id)
            if journal['state'] != 'committed' or asdict(resource) != journal['resource']: raise DownloadError('identity_mismatch')
            self._candidate(resource.candidate_id, resource.item_id)
            with self._grant(journal['grant_id']) as root:
                path = root.path / journal['final_name']
                if self.repository.output_path(resource_id) != str(path): raise DownloadError('identity_mismatch')
                with _Guard(path, directory=False) as guard:
                    if list(guard.identity) != journal['identity']: raise DownloadError('identity_mismatch')
                    guard.expected_stamp = guard.file_stamp()
                    sha, size, identity = self._hash(guard, cancel)
                    if (sha, size, identity) != (resource.sha256, resource.size_bytes, journal['identity']): raise DownloadError('identity_mismatch')
                    guard.verify(); root.verify()
                    self._bind_delete_guard(guard,grant_id=journal['grant_id'],candidate_id=resource.candidate_id,
                        item_id=resource.item_id,resource_id=resource_id,parent_kind='output')
                    if touch:
                        journal['last_access'] = self.clock()
                        with self.engine.begin() as connection: self._save(connection, 'journal', resource_id, journal)
                    yield _BoundResource(resource,journal,root,guard)

    def path_for(self, resource_id, *, cancel=None, touch=True):
        with self.open_resource(resource_id,cancel=cancel,touch=touch) as bound:
            bound.verify()
            return bound.path

    def recover(self, *, cancel=None):
        recovered = 0
        cancel = cancel or _NeverCancel(); _check_cancel(cancel)
        with self.lock:
            self.recover_deletions(cancel=cancel)
            self.reconcile_allocations(cancel=cancel)
            for rid, _ in self._values('journal'):
                _check_cancel(cancel)
                journal = self._journal(rid)
                if journal['state'] != 'prepared': continue
                resource = OutputResource(**journal['resource'])
                with self._grant(journal['grant_id']) as root, self._area(root,journal['grant_id'],expected_identity=journal['area_identity']) as area:
                    path = root.path / journal['final_name']
                    if not path.exists(): continue
                    temporary = root.path / journal['area'] / journal['temporary_name']
                    # Publication can stop between the exclusive link and
                    # removing its owned temporary. Never accept other links.
                    if temporary.exists():
                        tid, allocation = self._temporary(temporary)
                        if (allocation['grant_id'],allocation['candidate_id'],allocation['item_id'],allocation['kind']) != (journal['grant_id'],resource.candidate_id,resource.item_id,resource.kind):
                            raise DownloadError('identity_mismatch')
                        with _Guard(temporary,directory=False) as pending:
                            if allocation['area_identity'] != journal['area_identity'] or list(pending.identity) != allocation['file_identity']:
                                raise DownloadError('identity_mismatch')
                            info = path.lstat()
                            if (info.st_dev,info.st_ino) != pending.identity or stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                                raise DownloadError('identity_mismatch')
                            if self._hash(pending,cancel,links=2) != (resource.sha256,resource.size_bytes,journal['identity']):
                                raise DownloadError('identity_mismatch')
                            self._validate(temporary,resource.kind,cancel)
                            self._bind_delete_guard(pending,grant_id=allocation['grant_id'],candidate_id=resource.candidate_id,
                                item_id=resource.item_id,temporary_id=tid)
                            _check_cancel(cancel); root.verify(); pending.unlink(cancel=cancel)
                            if os.name != 'nt': os.fsync(pending.parent_fd)
                    with _Guard(path, directory=False) as guard:
                        sha, size, identity = self._hash(guard,cancel)
                        if (sha, size, identity) != (resource.sha256, resource.size_bytes, journal['identity']): raise DownloadError('identity_mismatch')
                        duration, _ = self._validate(path,resource.kind,cancel)
                        if duration != resource.duration_ms: raise DownloadError('media_invalid')
                        if self._hash(guard,cancel) != (sha,size,identity): raise DownloadError('identity_mismatch')
                    journal['state'] = 'committed'
                    _check_cancel(cancel)
                    with self.engine.begin() as connection:
                        existing = connection.execute(select(DownloadOutputRow.id).where(DownloadOutputRow.id == rid)).scalar_one_or_none()
                        if existing is None: self.repository.save_output(connection, resource, backend_path=str(path)); recovered += 1
                        else:
                            saved = self.repository._row(connection, DownloadOutputRow, rid)
                            if saved['backend_path'] != str(path) or any(saved[k] != v for k,v in journal['resource'].items()): raise DownloadError('identity_mismatch')
                        self._save(connection, 'journal', rid, journal)
                        tid = journal['temporary_name'].split('-')[1].split('.')[0]
                        connection.execute(delete(Setting).where(Setting.key == _metadata_key('temporary',tid)))
                        _check_cancel(cancel)
        return recovered

    def release_temporary(self, temporary):
        with self.lock:
            tid, allocation = self._temporary(temporary)
            self._candidate(allocation['candidate_id'],allocation['item_id'])
            with self._grant(allocation['grant_id']) as root, self._temporary_area(root,allocation) as area:
                path = root.path / allocation['area'] / allocation['name']
                if Path(temporary).absolute() != path: raise DownloadError('unsafe_source')
                if path.exists():
                    with _Guard(path, directory=False) as guard:
                        if list(guard.identity) != allocation['file_identity']: raise DownloadError('identity_mismatch')
                        self._bind_delete_guard(guard,grant_id=allocation['grant_id'],candidate_id=allocation['candidate_id'],
                            item_id=allocation['item_id'],temporary_id=tid)
                        guard.unlink()
                with self.engine.begin() as connection: connection.execute(delete(Setting).where(Setting.key == _metadata_key('temporary',tid)))

    def _references(self, connection, resource, path, *, auxiliary_job_id=None):
        session = Session(bind=connection)
        try:
            identity = tuple(self._journal(resource.id)['identity'])
            def physical_reference(raw):
                other = Path(raw)
                if other.absolute()==path: return True
                try: info = other.stat()
                except FileNotFoundError: return False
                return (info.st_dev,info.st_ino)==identity
            ensure_not_referenced(session, [resource.id, str(path), resource.sha256])
            for original, managed in connection.execute(select(MediaAsset.original_path, MediaAsset.managed_path)):
                for raw in (original, managed):
                    if not raw: continue
                    if physical_reference(raw): raise DownloadError('identity_mismatch')
            others = connection.execute(select(DownloadOutputRow).where(DownloadOutputRow.id != resource.id)).mappings()
            for row in others:
                try:
                    old = self._journal(row['id'])
                    if old['state']=='deleted' and all(row[k]==v for k,v in old['resource'].items()): continue
                except DownloadError:
                    # Unknown or damaged evidence remains conservative.
                    pass
                if physical_reference(row['backend_path']): raise DownloadError('identity_mismatch')
            if connection.execute(select(DownloadStageRow.id).join(DownloadItemRow,DownloadItemRow.id==DownloadStageRow.item_id)
                    .join(TaskCenterRow,TaskCenterRow.id==DownloadItemRow.task_id).where(DownloadStageRow.resource_id==resource.id,
                    TaskCenterRow.state.not_in(('succeeded','partial','failed','cancelled','skipped','completed','completed_with_errors')))).first(): raise DownloadError('identity_mismatch')
            if auxiliary_job_id is not None:
                if not _identity(auxiliary_job_id): raise DownloadError('input_limit')
                own = connection.execute(select(DownloadAuxiliaryJobRow.id).where(
                    DownloadAuxiliaryJobRow.id==auxiliary_job_id,DownloadAuxiliaryJobRow.kind=='resource_delete',
                    DownloadAuxiliaryJobRow.state=='running',DownloadAuxiliaryJobRow.output_resource_id==resource.id,
                    DownloadAuxiliaryJobRow.candidate_id==resource.candidate_id)).first()
                if own is None: raise DownloadError('identity_mismatch')
            active = select(DownloadAuxiliaryJobRow.id).where(or_(DownloadAuxiliaryJobRow.resource_id==resource.id,
                DownloadAuxiliaryJobRow.output_resource_id==resource.id),
                DownloadAuxiliaryJobRow.state.not_in(('succeeded','failed','cancelled','skipped')))
            if auxiliary_job_id is not None: active=active.where(DownloadAuxiliaryJobRow.id!=auxiliary_job_id)
            if connection.execute(active).first(): raise DownloadError('identity_mismatch')
            if connection.execute(select(DownloadCandidateRow.id).where(DownloadCandidateRow.cover_resource_id == resource.id)).first(): raise DownloadError('identity_mismatch')
        except RuntimeError: raise DownloadError('identity_mismatch') from None
        finally: session.close()

    def delete(self, resource_id, *, auxiliary_job_id=None):
        with self.open_resource(resource_id,touch=False) as bound:
            resource,path,journal = bound.resource,bound.path,bound.journal
            guard,root = bound.guard,bound.root
            with self.engine.connect() as connection:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    self._references(connection, resource, path,auxiliary_job_id=auxiliary_job_id)
                    guard.verify(); root.verify()
                    for revoke in self._revoke_callbacks: revoke(resource_id)
                    connection.commit()
                except Exception: connection.rollback(); raise
            def finalize(remove, record=None):
                with self.engine.connect() as connection:
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                    try:
                        self._references(connection,resource,path,auxiliary_job_id=auxiliary_job_id); root.verify()
                        remove()
                        journal['state']='deleted'
                        self._save(connection,'journal',resource_id,journal)
                        if record is not None: self._drop_deletion(connection,record['id'])
                        connection.commit()
                    except Exception: connection.rollback(); raise
            if os.name=='nt': finalize(guard.unlink)
            else:
                guard.finalize_delete = finalize
                guard.unlink()

    def cleanup_previews(self):
        count = 0
        with self.lock:
            for rid, _ in self._values('journal'):
                journal = self._journal(rid)
                if (journal['state'] != 'committed' or journal['resource']['kind'] not in ('preview','cover')
                        or journal['resource']['item_id'] is not None or self.clock() - journal['last_access'] < 86400): continue
                try: self.delete(rid); count += 1
                except DownloadError: continue
        return count
