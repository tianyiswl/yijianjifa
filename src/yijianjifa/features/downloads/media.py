"""Owned, local-only media conversions for the download processing lane.

Paths and executable overrides are backend-only dependencies. This module never
accepts UI paths, command strings, arbitrary tool arguments, or shell execution.
The caller holds the shared local processing slot and promotes successful paths;
the ASR stage releases its successfully returned temporary WAV after recognition.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import threading
import time
from typing import Callable, Protocol

from yijianjifa.os_integration.resources import ffmpeg_path, ffprobe_path
from .contracts import CancelToken, DownloadError, OutputResource, Progress, StageResult


MEDIA_ERROR_MESSAGES = {
    'no_audio': '原视频没有音轨，无法提取音频',
    'media_invalid': '视频或转换结果损坏，无法处理',
    'media_tool_missing': '本地媒体工具不可用，请检查客户端安装',
    'media_failed': '媒体处理失败，请重试',
    'timeout': '媒体处理超时，请重试',
    'cancelled': '已取消媒体处理',
    'identity_mismatch': '原视频已变化，请重新下载',
    'source_missing': '原视频已不存在，请重新下载',
}
_STAGES = {'mp3': 'audio', 'wav': 'audio', 'silent': 'silent', 'asr_wav': 'asr'}
_PROTOCOLS = 'file,pipe'
_MP4_FORMATS = {'mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2'}
_COPY_VIDEO_CODECS = {'h264', 'hevc', 'mpeg4', 'av1'}
_LOG = logging.getLogger(__name__)


class OwnedMediaFiles(Protocol):
    def path_for(self, resource_id: str) -> Path: ...
    def temporary_for(self, source: OutputResource, kind: str) -> Path: ...


def stage_result_for_error(kind: str, error: DownloadError) -> StageResult:
    stage = _stage(kind)
    return StageResult(stage, 'cancelled' if error.code == 'cancelled' else 'failed', error_code=error.code)


def _stage(kind: str) -> str:
    if type(kind) is not str or kind not in _STAGES:
        raise DownloadError('input_limit')
    return _STAGES[kind]


def _check_cancel(cancel: CancelToken, deadline: float) -> None:
    if cancel.cancelled():
        raise DownloadError('cancelled')
    if time.monotonic() >= deadline:
        raise DownloadError('timeout')


def _remove_temporary(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise DownloadError('media_failed') from None


def _spawn_options(platform: str | None = None) -> dict:
    if (platform or os.name) == 'nt':
        # Constants are spelled out so this policy is testable on POSIX hosts.
        return {'creationflags': 0x08000000 | 0x00000200 | 0x00000004}
    return {'start_new_session': True}


class _WindowsJob:
    """Kill-on-close job owns the tool and any child processes on Windows."""
    def __init__(self, process: subprocess.Popen):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(field, ctypes.c_uint64) for field in ('ReadOperationCount', 'WriteOperationCount',
                        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise DownloadError('media_failed')
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle))):
            self.close()
            raise DownloadError('media_failed')

    def stop(self) -> None:
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _resume_windows_process(process: subprocess.Popen) -> None:
    """Resume the unique initial thread only after the suspended tool owns a job.

    Popen closes CreateProcess's initial thread handle, so use documented Toolhelp
    APIs to reopen that thread. The process has never executed, and no descendants
    exist before job assignment. Failure leaves it suspended for caller teardown.
    """
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ThreadID', wintypes.DWORD), ('th32OwnerProcessID', wintypes.DWORD),
                    ('tpBasePri', wintypes.LONG), ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ('Thread32First', 'Thread32Next'):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        function.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
    kernel.GetProcessIdOfThread.restype = wintypes.DWORD
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise DownloadError('media_failed')
    thread_ids = []
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Thread32First(snapshot, ctypes.byref(entry)):
            raise DownloadError('media_failed')
        for _ in range(100000):
            if entry.dwSize < ThreadEntry.th32OwnerProcessID.offset + ctypes.sizeof(wintypes.DWORD):
                raise DownloadError('media_failed')
            if entry.th32OwnerProcessID == process.pid:
                thread_ids.append(entry.th32ThreadID)
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel.Thread32Next(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise DownloadError('media_failed')
                break
        else:
            raise DownloadError('media_failed')
    finally:
        kernel.CloseHandle(snapshot)
    if len(thread_ids) != 1:
        raise DownloadError('media_failed')
    thread = kernel.OpenThread(0x0002 | 0x0800, False, thread_ids[0])  # SUSPEND_RESUME | QUERY_LIMITED_INFORMATION
    if not thread:
        raise DownloadError('media_failed')
    try:
        if kernel.GetProcessIdOfThread(thread) != process.pid or kernel.ResumeThread(thread) != 1:
            raise DownloadError('media_failed')
    finally:
        kernel.CloseHandle(thread)


class _PipeCapture:
    def __init__(self, pipe, *, limit: int, events: queue.Queue | None = None):
        self.pipe = pipe
        self.limit = limit
        self.events = events
        self.data = bytearray()
        self.overflow = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        try:
            while chunk := self.pipe.readline(8192):
                room = max(0, self.limit - len(self.data))
                self.data.extend(chunk[:room])
                self.overflow |= len(chunk) > room
                if self.events is not None:
                    try:
                        self.events.put_nowait(chunk)
                    except queue.Full:
                        pass  # bounded progress; a later event reports current state
        finally:
            self.pipe.close()


def _stop_process(process: subprocess.Popen, job: _WindowsJob | None, grace: float) -> None:
    if os.name == 'nt':
        if job is not None:
            job.stop()
        elif process.poll() is None:
            process.kill()
        process.wait()
        return
    # Reap the direct child before signalling its owned session. macOS can deny
    # signal-0 group probes during exit; poll/wait does not have that race.
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        _reap_process(process)
        return
    except PermissionError:
        pass  # Still attempt group KILL and the direct-child fallback below.
    until = time.monotonic() + grace
    while time.monotonic() < until:
        process.poll()
        time.sleep(.01)
    group_unverified = False
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        group_unverified = True
        # Only the Popen-owned direct child may be used as a fallback. Never
        # infer a process tree or silently claim denied group cleanup succeeded.
        _LOG.warning('media_cleanup_group_unverified pid=%d', process.pid)
        if process.poll() is None:
            try:
                process.kill()
            except (OSError, subprocess.SubprocessError):
                _LOG.warning('media_cleanup_direct_signal_failed pid=%d', process.pid)
    _reap_process(process)
    if group_unverified:
        raise DownloadError('media_failed')


def _reap_process(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        _LOG.warning('media_cleanup_direct_unreaped pid=%d', process.pid)
        raise DownloadError('media_failed') from None


def _run_process(tool: Path, arguments: tuple[str, ...], *, cancel: CancelToken, deadline: float,
                 grace: float, on_line: Callable[[bytes], None] | None = None,
                 output_limit: int = 2 * 1024 * 1024, failure_code: str = 'media_failed') -> bytes:
    """Private runner: only this backend constructs the finite tool argument sets."""
    _check_cancel(cancel, deadline)
    process = None
    job = None
    readers = []
    stopped = False
    primary_failure = False
    events = queue.Queue(maxsize=64) if on_line else None

    def deliver() -> None:
        if events is not None:
            while True:
                try:
                    on_line(events.get_nowait())
                except queue.Empty:
                    break

    try:
        process = subprocess.Popen([str(tool), *arguments], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                   close_fds=True, **_spawn_options())
        if os.name == 'nt':
            job = _WindowsJob(process)
            _check_cancel(cancel, deadline)
            _resume_windows_process(process)
        stdout = _PipeCapture(process.stdout, limit=output_limit, events=events)
        stderr = _PipeCapture(process.stderr, limit=65536)
        readers = [stdout, stderr]
        while process.poll() is None:
            _check_cancel(cancel, deadline)
            deliver()
            time.sleep(.02)
        _check_cancel(cancel, deadline)
        # Descendants cannot outlive a tool, even on a successful direct exit.
        _stop_process(process, job, grace)
        stopped = True
        for reader in readers:
            reader.thread.join(timeout=2)
        deliver()
        if process.returncode != 0 or stdout.overflow and on_line is None:
            raise DownloadError(failure_code)
        return bytes(stdout.data)
    except (FileNotFoundError, PermissionError):
        primary_failure = True
        raise DownloadError('media_tool_missing') from None
    except OSError:
        primary_failure = True
        raise DownloadError('media_failed') from None
    except BaseException:
        primary_failure = True
        raise
    finally:
        cleanup_failed = False
        try:
            if process is not None and not stopped:
                _stop_process(process, job, grace)
        except (DownloadError, OSError, subprocess.SubprocessError):
            cleanup_failed = True
            _LOG.warning('media_cleanup_failed pid=%d', process.pid)
        try:
            if job is not None:
                job.close()
        except (DownloadError, OSError):
            cleanup_failed = True
            _LOG.warning('media_cleanup_job_failed pid=%d', process.pid)
        for reader in readers:
            reader.thread.join(timeout=2)
        if process is not None:
            # Job assignment/resume can fail before pipe readers are started.
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    if any(reader.pipe is pipe and reader.thread.is_alive() for reader in readers):
                        # Closing a pipe locked in read can block indefinitely
                        # when group signals were denied. Retain the daemon
                        # reader and report unresolved cleanup to the backend.
                        cleanup_failed = True
                        _LOG.warning('media_cleanup_pipe_pending pid=%d', process.pid)
                    else:
                        try:
                            pipe.close()
                        except OSError:
                            cleanup_failed = True
        if cleanup_failed and not primary_failure:
            raise DownloadError('media_failed') from None


@dataclass(frozen=True)
class _MediaInfo:
    format_names: frozenset[str]
    streams: tuple[dict, ...]
    duration_ms: int

    def stream(self, kind: str) -> dict | None:
        return next((row for row in self.streams if row.get('codec_type') == kind
                     and not row.get('disposition', {}).get('attached_pic')), None)


def _duration_ms(value) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return round(seconds * 1000) if math.isfinite(seconds) and seconds > 0 else None


class MediaProcessor:
    def __init__(self, files: OwnedMediaFiles, *, ffmpeg: Path | None = None, ffprobe: Path | None = None,
                 timeout: float = 3600, terminate_grace: float = .2):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(terminate_grace) not in (int, float) or not math.isfinite(terminate_grace)
                or not 0 <= terminate_grace <= 2):
            raise DownloadError('input_limit')
        self.files = files
        self.ffmpeg = Path(ffmpeg or ffmpeg_path()).absolute()
        self.ffprobe = Path(ffprobe or ffprobe_path()).absolute()
        self.timeout = timeout
        self.grace = terminate_grace

    def run(self, source: OutputResource, kind: str, *, cancel: CancelToken,
            progress: Callable[[Progress], None]) -> Path:
        stage = _stage(kind)
        deadline = time.monotonic() + self.timeout
        _check_cancel(cancel, deadline)
        source_path = self.files.path_for(source.id)
        before = self._verify_source(source_path, source, cancel, deadline)
        self._verify_mp4_header(source_path)
        original = self._probe(source_path, cancel, deadline, source=True)
        video = original.stream('video')
        if not video or not original.format_names.intersection(_MP4_FORMATS):
            raise DownloadError('media_invalid')
        audio = original.stream('audio')
        if kind != 'silent' and audio is None:
            raise DownloadError('no_audio')
        expected_ms = (_duration_ms((audio if kind != 'silent' else video).get('duration'))
                       or original.duration_ms)
        progress(Progress(stage, 0, expected_ms))
        temporary = None
        try:
            _check_cancel(cancel, deadline)
            temporary = self.files.temporary_for(source, kind)
            if temporary == source_path or temporary.exists() and temporary.samefile(source_path):
                # Never remove the source even if a faulty owner returns it.
                temporary = None
                raise DownloadError('identity_mismatch')
            if temporary.is_symlink():
                raise DownloadError('identity_mismatch')
            self._convert(source_path, temporary, kind, video, audio, cancel, deadline,
                          lambda line: self._progress(line, stage, expected_ms, progress))
            result = self._probe(temporary, cancel, deadline, output_kind=kind)
            self._verify_output(result, original, kind, expected_ms)
            if self._verify_source(self.files.path_for(source.id), source, cancel, deadline) != before:
                raise DownloadError('identity_mismatch')
            _check_cancel(cancel, deadline)
            progress(Progress(stage, expected_ms, expected_ms))
            return temporary
        except DownloadError:
            _remove_temporary(temporary)
            raise
        except Exception:
            _remove_temporary(temporary)
            raise DownloadError('media_failed') from None

    @staticmethod
    def _verify_source(path: Path, source: OutputResource, cancel: CancelToken, deadline: float) -> tuple:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                identity = os.fstat(stream.fileno())
                if not stat.S_ISREG(identity.st_mode) or identity.st_size != source.size_bytes or path.is_symlink():
                    raise DownloadError('identity_mismatch')
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    _check_cancel(cancel, deadline)
                    digest.update(chunk)
                if digest.hexdigest() != source.sha256:
                    raise DownloadError('identity_mismatch')
                return identity.st_dev, identity.st_ino, identity.st_size, digest.hexdigest()
        except FileNotFoundError:
            raise DownloadError('source_missing') from None
        except OSError:
            raise DownloadError('identity_mismatch') from None

    @staticmethod
    def _verify_mp4_header(path: Path) -> None:
        # Fixed mov demuxer and disabled external references prevent a disguised
        # MP4 from being interpreted as an HLS/concat playlist or another input.
        try:
            with path.open('rb') as stream:
                file_size = os.fstat(stream.fileno()).st_size
                cursor = 0
                # Seek across bounded legitimate padding; only atom headers are
                # read, so an 8 KiB (or larger) free block need not be buffered.
                for _ in range(128):
                    if cursor + 8 > file_size or cursor >= 16 * 1024 * 1024:
                        break
                    stream.seek(cursor)
                    header = stream.read(8)
                    if len(header) != 8:
                        break
                    size = int.from_bytes(header[:4], 'big')
                    atom = header[4:8]
                    header_size = 8
                    if size == 1:
                        extended = stream.read(8)
                        if len(extended) != 8:
                            break
                        size = int.from_bytes(extended, 'big')
                        header_size = 16
                    if size < header_size or cursor + size > file_size:
                        break
                    if atom == b'ftyp':
                        payload_size = size - header_size
                        if 8 <= payload_size <= 4096 and payload_size % 4 == 0:
                            return
                        break
                    if atom not in {b'free', b'skip', b'wide'}:
                        break
                    cursor += size
        except FileNotFoundError:
            raise DownloadError('source_missing') from None
        except OSError:
            raise DownloadError('identity_mismatch') from None
        raise DownloadError('media_invalid')

    def _probe(self, path: Path, cancel: CancelToken, deadline: float, *, source: bool = False,
               output_kind: str | None = None) -> _MediaInfo:
        try:
            usable = path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        except OSError:
            usable = False
        if not usable:
            raise DownloadError('media_invalid')
        args = ('-v', 'error', '-protocol_whitelist', _PROTOCOLS)
        if source or output_kind == 'silent':
            args += ('-f', 'mov', '-enable_drefs', '0', '-use_absolute_path', '0')
        elif output_kind is not None:
            args += ('-f', 'mp3' if output_kind == 'mp3' else 'wav')
        args += ('-show_streams', '-show_format', '-of', 'json', str(path))
        raw = _run_process(self.ffprobe, args, cancel=cancel, deadline=deadline, grace=self.grace,
                           failure_code='media_invalid')
        try:
            parsed = json.loads(raw)
            names = frozenset(parsed['format']['format_name'].split(','))
            duration = _duration_ms(parsed['format']['duration'])
            streams = parsed['streams']
            if (not duration or not isinstance(streams, list) or not 0 < len(streams) <= 64
                    or any(not isinstance(row, dict) or type(row.get('index')) is not int
                           or not 0 <= row['index'] < 1024 for row in streams)):
                raise ValueError
            return _MediaInfo(names, tuple(streams), duration)
        except (ValueError, TypeError, KeyError):
            raise DownloadError('media_invalid') from None

    def _convert(self, source: Path, output: Path, kind: str, video: dict, audio: dict | None, cancel: CancelToken,
                 deadline: float, on_line: Callable[[bytes], None]) -> None:
        args = ('-nostdin', '-hide_banner', '-v', 'error', '-y', '-protocol_whitelist', _PROTOCOLS,
                '-f', 'mov', '-enable_drefs', '0', '-use_absolute_path', '0', '-noautorotate', '-i', str(source),
                '-map_metadata', '0', '-progress', 'pipe:1', '-nostats')
        if kind == 'silent':
            args += ('-map', f"0:{video['index']}", '-an', '-sn', '-dn')
            if video.get('codec_name') in _COPY_VIDEO_CODECS:
                args += ('-c:v', 'copy')
            else:
                args += ('-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p')
            args += ('-movflags', '+faststart', '-f', 'mp4')
        else:
            args += ('-map', f"0:{audio['index']}", '-vn', '-sn', '-dn')
            if kind == 'mp3':
                args += ('-c:a', 'libmp3lame', '-q:a', '2', '-f', 'mp3')
            else:
                args += ('-c:a', 'pcm_s16le')
                if kind == 'asr_wav':
                    args += ('-ar', '16000', '-ac', '1')
                args += ('-f', 'wav')
        args += (str(output),)
        _run_process(self.ffmpeg, args, cancel=cancel, deadline=deadline, grace=self.grace,
                     on_line=on_line, output_limit=65536)

    @staticmethod
    def _progress(line: bytes, stage: str, total: int, callback: Callable[[Progress], None]) -> None:
        key, _, value = line.strip().partition(b'=')
        if key == b'out_time_us':
            try:
                current = max(0, min(total, int(value) // 1000))
            except ValueError:
                return
            callback(Progress(stage, current, total))

    @staticmethod
    def _verify_output(result: _MediaInfo, original: _MediaInfo, kind: str, expected_ms: int) -> None:
        if abs(result.duration_ms - expected_ms) > 100:
            raise DownloadError('media_invalid')
        if kind == 'silent':
            output = result.stream('video')
            source = original.stream('video')
            if (output is None or len(result.streams) != 1 or any(row.get('codec_type') == 'audio' for row in result.streams)
                    or not result.format_names.intersection(_MP4_FORMATS)
                    or (output.get('width'), output.get('height')) != (source.get('width'), source.get('height'))):
                raise DownloadError('media_invalid')
            source_rotation = next((row.get('rotation') for row in source.get('side_data_list', [])
                                    if row.get('side_data_type') == 'Display Matrix'), 0)
            output_rotation = next((row.get('rotation') for row in output.get('side_data_list', [])
                                    if row.get('side_data_type') == 'Display Matrix'), 0)
            if output_rotation != source_rotation:
                raise DownloadError('media_invalid')
        else:
            if len(result.streams) != 1 or result.streams[0].get('codec_type') != 'audio':
                raise DownloadError('media_invalid')
            output = result.streams[0]
            if (kind == 'mp3' and (output.get('codec_name') != 'mp3' or 'mp3' not in result.format_names)
                    or kind != 'mp3' and (output.get('codec_name') != 'pcm_s16le' or 'wav' not in result.format_names)
                    or kind == 'asr_wav' and (output.get('sample_rate') != '16000' or output.get('channels') != 1)):
                raise DownloadError('media_invalid')
