"""A disposable interpreter owns one read-only browser check and its children.

Only fixed native provider factories are imported. No credentials, database URL,
page-selected programs or arbitrary scripts are accepted on the command line.
"""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from .session_check_diagnostics import CheckDiagnostics, MAX_BYTES, sanitize_diagnostic


_RETAINED = []
_POLL_SECONDS = .02
_CLEANUP_SECONDS = 2


def _command(kind='worker'):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--session-check-worker' if kind == 'worker' else '--session-browser-gate']
    command = [sys.executable, '-m', 'yijianjifa.webui.session_check_process']
    return command if kind == 'worker' else command + ['--browser-gate']


def _write_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            temporary.chmod(0o600)
            json.dump(value, output, separators=(',', ':'))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _install_gate(directory):
    """Keep detached browser launches in our group until ownership is recorded.

Playwright's Node driver normally detaches Chromium before returning from
spawn(). The gate inherits the checker group, registers, waits for host ACK,
then setsid+execs Chromium with the same PID/stdio. SDK kill(-browser.pid) keeps
its original meaning, including protocol pipe descriptors 3 and 4.
"""
    gate = directory / 'browser-gate.cjs'
    code = ''''use strict';
const cp=require('node:child_process');
const original=cp.spawn;
const gate=%s;
cp.spawn=function(command,args,options){
 if(options && options.detached===true){
   return original.call(this,gate[0],[...gate.slice(1),command,...(args||[])],{...options,detached:false});
 }
 return original.apply(this,arguments);
};
''' % json.dumps(_command('gate'))
    gate.write_text(code, encoding='utf-8'); gate.chmod(0o600)
    return gate


def browser_gate_main(arguments):
    """No browser can detach before the host has acknowledged its exact PID."""
    try:
        directory = Path(os.environ['P011_SESSION_CHECK_ROOT'])
        deadline = float(os.environ['P011_SESSION_CHECK_DEADLINE'])
        pid = os.getpid()
        if os.name == 'nt' or not directory.is_dir() or not arguments:
            return 2
        gates = directory / 'gates'
        _write_json(gates / (str(pid) + '.json'), {'pid': pid, 'parent': os.getppid()})
        acknowledgement = gates / (str(pid) + '.ack')
        while not acknowledgement.is_file():
            if time.monotonic() >= deadline or os.getppid() == 1:
                return 2
            time.sleep(_POLL_SECONDS)
        # All inherited protocol descriptors are preserved by exec. Python
        # otherwise marks descriptors non-inheritable on fresh opens only.
        os.setsid()
        os.execvpe(arguments[0], arguments, os.environ)
    except (KeyError, OSError, ValueError):
        return 2


def _acknowledge(directory, process, registered):
    for path in (directory / 'gates').glob('*.json'):
        try:
            value = json.loads(path.read_text())
            pid = value['pid']
            if type(pid) is not int or pid <= 1 or path.name != str(pid) + '.json':
                raise ValueError('invalid owned browser registration')
            if pid in registered:
                continue
            # This exact live child is still in our new session before ACK.
            # Registration cannot direct signals at a shared browser/process.
            if os.getpgid(pid) != process.pid:
                raise ValueError('browser gate left its owned session before ACK')
            registered.add(pid)
            _write_json(path.with_suffix('.ack'), {'registered': True})
        except ProcessLookupError:
            # A gate that exits before ACK has never launched a browser.
            continue


def _processes():
    # Command lines stay in memory and are never logged or returned to the UI.
    # Only exact private crash-database arguments are used for ownership.
    scanner = subprocess.Popen(['ps', '-axo', 'pid=,pgid=,stat=,command='], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    try:
        output, _ = scanner.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        scanner.kill(); scanner.communicate(timeout=1)
        raise
    if scanner.returncode:
        raise subprocess.CalledProcessError(scanner.returncode, ['ps'])
    rows = output.splitlines()
    result = []
    for line in rows:
        values = line.split(None, 3)
        if len(values) == 4 and values[0].isdigit() and values[1].isdigit() and int(values[0]) != scanner.pid:
            result.append((int(values[0]), int(values[1]), values[2], values[3]))
    return result


def _crashpad_executable(pid):
    if sys.platform == 'darwin':
        import ctypes
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        library.proc_pidpath.restype = ctypes.c_int
        output = ctypes.create_string_buffer(4096)
        if library.proc_pidpath(pid, output, len(output)) <= 0: return False
        return Path(os.fsdecode(output.value)).name == 'chrome_crashpad_handler'
    try: return Path(os.readlink('/proc/' + str(pid) + '/exe')).name == 'chrome_crashpad_handler'
    except FileNotFoundError: return False


def _crashpads(rows, database):
    # The random database lives directly in /tmp, so whitespace cannot make
    # ps's unquoted command serialization ambiguous. Confirm the executable
    # using the kernel, not a name/sub-string supplied by another command.
    pattern = re.compile(r'(?:^| )--database=' + re.escape(str(database)) + r'(?: |$)')
    return {(pid, group) for pid, group, state, command in rows
            if not state.startswith('Z') and pattern.search(command) and _crashpad_executable(pid)}


def _stop_posix(process, registered, database):
    # Freeze the driver and every gate that has not yet detached. The closed
    # registration set now includes every browser allowed to leave this group.
    try: os.killpg(process.pid, signal.SIGSTOP)
    except ProcessLookupError: pass
    groups = {process.pid, *registered}
    try:
        for pgid in groups:
            try: os.killpg(pgid, signal.SIGSTOP)
            except ProcessLookupError: pass
        # Chrome on macOS double-forks its two crash handlers into separate
        # groups. Both use this job's unique BREAKPAD_DUMP_LOCATION database.
        stopped_handlers = set()
        deadline = time.monotonic() + _CLEANUP_SECONDS
        while True:
            current = _crashpads(_processes(), database)
            new = current - stopped_handlers
            if not new: break
            if time.monotonic() >= deadline: return False
            for pid, pgid in new:
                if (pid, pgid) in _crashpads(_processes(), database):
                    try: os.kill(pid, signal.SIGSTOP)
                    except ProcessLookupError: continue
                    stopped_handlers.add((pid, pgid))
        # This proves only exact PIDs, never an otherwise unknown whole PGID.
        for pid, pgid in stopped_handlers:
            if (pid, pgid) in _crashpads(_processes(), database):
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
    finally:
        # A failed process enumeration must never leave known owned groups
        # frozen, including their lease watcher. Kill/reap regardless of ps.
        for pgid in groups:
            try: os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError: pass
        process.wait(timeout=_CLEANUP_SECONDS)
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while time.monotonic() < deadline:
        rows = _processes()
        remaining_handlers = _crashpads(rows, database)
        for pid, pgid in remaining_handlers:
            if (pid, pgid) in _crashpads(_processes(), database):
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
        if not remaining_handlers and not any(group in groups and not state.startswith('Z')
                                              for _, group, state, _ in rows):
            return True
        time.sleep(_POLL_SECONDS)
    return False


def _watch_parent(directory, database, deadline):
    """The host pipe disappearing cannot leave a normally schedulable worker.

This thread never calls the SDK. The host remains the hard deadline owner if
the SDK holds the interpreter GIL; unexpected host kill plus a permanently held
GIL is outside this cooperative orphan-cleanup fallback.
"""
    import select
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([0], [], [], min(.1, max(0, deadline - time.monotonic())))
            if readable and not os.read(0, 1): break
        root = os.getpgrp()
        own = os.getpid()
        registered = {int(path.stem) for path in (directory / 'gates').glob('*.ack') if path.stem.isdigit()}
        # Stopping our whole group would stop this watcher too. Freeze other
        # members individually, then close the already acknowledged groups.
        stopped = set()
        until = time.monotonic() + _CLEANUP_SECONDS
        while time.monotonic() < until:
            children = {pid for pid, group, state, _ in _processes()
                        if group == root and pid != own and not state.startswith('Z')}
            new = children - stopped
            if not new: break
            for pid in new:
                try: os.kill(pid, signal.SIGSTOP)
                except ProcessLookupError: pass
                stopped.add(pid)
        for group in registered:
            try: os.killpg(group, signal.SIGKILL)
            except ProcessLookupError: pass
        for pid in stopped:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
        until = time.monotonic() + _CLEANUP_SECONDS
        while time.monotonic() < until:
            rows = _processes()
            handlers = _crashpads(rows, database)
            for pid, group in handlers:
                if (pid, group) in _crashpads(_processes(), database):
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
            if not handlers and not any(group in {root, *registered} and pid != own and not state.startswith('Z')
                                        for pid, group, state, _ in rows):
                shutil.rmtree(directory)
                shutil.rmtree(database)
                break
            time.sleep(_POLL_SECONDS)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # Keep private evidence when OS cleanup cannot be confirmed.
    finally:
        # This new session contains only the owned checker/driver/pre-ACK gates.
        os.killpg(os.getpgrp(), signal.SIGKILL)


def _result(directory):
    path = directory / 'result.json'
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        return {'state': 'unavailable', 'reason': 'invalid_provider_result'}
    value = json.loads(path.read_text())
    reasons = {'cancelled', 'timeout', 'network_unavailable', 'verification_required', 'identity_unavailable',
               'session_unavailable', 'cleanup_failed', 'session_missing'}
    if (type(value) is not dict or set(value) - {'state', 'platform_user_id', 'reason', 'diagnostic'}
            or type(value.get('state')) is not str or value['state'] not in {'valid', 'login_required', 'identity_mismatch', 'unavailable'}
            or 'reason' in value and (type(value['reason']) is not str or value['reason'] not in reasons)):
        return {'state': 'unavailable', 'reason': 'invalid_provider_result'}
    uid = value.get('platform_user_id')
    if (uid is not None and (type(uid) is not str or not 1 <= len(uid) <= 255
                            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uid))
            or value['state'] == 'valid' and uid is None):
        return {'state': 'unavailable', 'reason': 'invalid_provider_result'}
    if 'diagnostic' in value:
        clean = sanitize_diagnostic(value['diagnostic'])
        if clean is None: return {'state': 'unavailable', 'reason': 'invalid_provider_result'}
        value = {**value, 'diagnostic': clean}
    return value


def _progress(directory):
    """Never accept unbounded or partly written child evidence."""
    try:
        path = directory / 'progress.json'
        if path.stat().st_size > MAX_BYTES: return None
        return sanitize_diagnostic(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def run_check(request, cancel):
    directory = Path(request['directory'])
    (directory / 'gates').mkdir(mode=0o700)
    request_file = directory / 'request.json'
    _write_json(request_file, request)
    diagnostics = CheckDiagnostics(initial=request.get('diagnostic'),
                                   publish=lambda value: _write_json(directory / 'progress.json', value))
    diagnostics.record('worker_start', 'started')
    environment = os.environ.copy()
    if getattr(sys, 'frozen', False):
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    else:
        source = str(Path(__file__).resolve().parents[2])
        environment['PYTHONPATH'] = source + os.pathsep + environment.get('PYTHONPATH', '')
    environment['P011_SESSION_CHECK_ROOT'] = str(directory)
    environment['P011_SESSION_CHECK_DEADLINE'] = str(request['deadline'])
    environment['P011_SESSION_CHECK_DIAGNOSTIC_STARTED'] = str(diagnostics.started)
    # Chrome 145's Mac crash client uses this exact environment override for
    # DIR_CRASH_DUMPS, including the monitor-self handler. Scope it to this
    # owned interpreter; never touch a shared crash-report/profile directory.
    crashpad = Path(tempfile.mkdtemp(prefix='p011-check-crash-', dir='/tmp' if os.name != 'nt' else None))
    crashpad.chmod(0o700)
    environment['BREAKPAD_DUMP_LOCATION'] = str(crashpad)
    if os.name != 'nt':
        environment['NODE_OPTIONS'] = '--require ' + json.dumps(str(_install_gate(directory)), ensure_ascii=False)
    process = job = None
    registered = set()
    cleaned = True
    result = {'state': 'unavailable', 'reason': 'network_unavailable'}
    parent_deadline = False
    try:
        if cancel.is_set():
            shutil.rmtree(crashpad)
            return {'state': 'unavailable', 'reason': 'cancelled'}, True
        options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': 0x08000000 | 0x00000004}
        process = subprocess.Popen([*_command(), str(request_file)], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   close_fds=True, env=environment, **options)
        if os.name == 'nt':
            job = _WindowsCheckJob(process)
        while True:
            if cancel.is_set():
                result = {'state': 'unavailable', 'reason': 'cancelled'}; break
            if time.monotonic() >= request['deadline']:
                result = {'state': 'unavailable', 'reason': 'timeout'}; parent_deadline = True; break
            if os.name != 'nt': _acknowledge(directory, process, registered)
            if process.poll() is not None:
                result = _result(directory) if process.returncode == 0 else result
                break
            time.sleep(_POLL_SECONDS)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        result = {'state': 'unavailable', 'reason': 'network_unavailable'}
    finally:
        observed = sanitize_diagnostic(result.get('diagnostic')) or _progress(directory) or diagnostics.snapshot()
        diagnostics = CheckDiagnostics(started=diagnostics.started, initial=observed)
        if parent_deadline:
            diagnostics.record('parent_deadline', 'failed', 'timeout', preserve_stage=True)
        diagnostics.record('cleanup', 'started', preserve_stage=True)
        if process is not None:
            try:
                cleaned = job.stop() if job else _stop_posix(process, registered, crashpad) if os.name != 'nt' else _stop_direct(process)
            except (OSError, subprocess.SubprocessError):
                cleaned = False
            if not cleaned: _RETAINED.append((process, job, registered, directory, crashpad))
            if process.stdin is not None: process.stdin.close()
        if cleaned:
            try: shutil.rmtree(crashpad)
            except FileNotFoundError: pass
            except OSError: cleaned = False
        diagnostics.record('cleanup', 'completed' if cleaned else 'failed', None if cleaned else 'cleanup_failed', preserve_stage=True)
    if not cleaned: result = {'state': 'unavailable', 'reason': 'cleanup_failed'}
    if cancel.is_set(): result = {'state': 'unavailable', 'reason': 'cancelled'}
    elif time.monotonic() >= request['deadline'] and result.get('state') == 'valid':
        result = {'state': 'unavailable', 'reason': 'timeout'}
        # Identity already completed. Only native cleanup crossed the deadline;
        # do not attribute this late result to the successful identity read.
        diagnostics.record('parent_deadline', 'failed', 'timeout')
    return {**result, 'diagnostic': diagnostics.snapshot()}, cleaned


def pending_directory(directory):
    return any(item[3] == directory for item in _RETAINED)


def retry_cleanup(timeout=0):
    deadline = time.monotonic() + max(0, timeout)
    for item in list(_RETAINED):
        # A final retry is intentionally bounded, never a shutdown busy loop.
        if time.monotonic() + _CLEANUP_SECONDS >= deadline: break
        process, job, registered, directory, crashpad = item
        try:
            cleaned = job.stop() if job else _stop_posix(process, registered, crashpad)
            if cleaned:
                try: shutil.rmtree(crashpad)
                except FileNotFoundError: pass
                _RETAINED.remove(item)
        except (OSError, subprocess.SubprocessError):
            pass
    return not _RETAINED


def _stop_direct(process):
    if process.poll() is None: process.kill()
    process.wait(timeout=_CLEANUP_SECONDS)
    return True


class _WindowsCheckJob:
    def __init__(self, process):
        # Reuse the already-reviewed native suspended/job/resume primitives.
        from yijianjifa.features.downloads.media import _WindowsJob, _resume_windows_process
        self.process = process
        self.job = None
        self._member_handles = {}
        try:
            self.job = _WindowsJob(process)
            _resume_windows_process(process)
        except Exception:
            if self.job is not None:
                self.job.stop(); self.job.close()
            _stop_direct(process)
            raise OSError('isolated check job could not be started') from None

    def _capture_members(self, kernel, deadline):
        import ctypes
        from ctypes import wintypes
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        for capacity in (16, 64, 256, 1024, 4096):
            if time.monotonic() >= deadline: return False
            class ProcessIds(ctypes.Structure):
                _fields_ = [('assigned', wintypes.DWORD), ('listed', wintypes.DWORD),
                            ('pids', ctypes.c_size_t * capacity)]
            members = ProcessIds()
            if not kernel.QueryInformationJobObject(self.job.handle, 3, ctypes.byref(members), ctypes.sizeof(members), None):
                if ctypes.get_last_error() == 234: continue  # ERROR_MORE_DATA
                return False
            if members.assigned > capacity or members.listed > capacity: continue
            for pid in members.pids[:members.listed]:
                if time.monotonic() >= deadline: return False
                if pid in self._member_handles: continue
                # 先持有原始进程对象，再核对 Job；后续只等待句柄，不依赖可复用 PID。
                handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
                if not handle:
                    if ctypes.get_last_error() == 87: continue  # 已退出的进程
                    return False
                keep = False
                try:
                    belongs = wintypes.BOOL()
                    if not kernel.IsProcessInJob(handle, self.job.handle, ctypes.byref(belongs)): return False
                    if belongs.value:
                        self._member_handles[pid] = handle
                        keep = True
                finally:
                    if not keep: kernel.CloseHandle(handle)
            return True
        return False

    def stop(self):
        import ctypes
        from ctypes import wintypes
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
                (name, wintypes.DWORD) for name in ('faults', 'total', 'active', 'terminated')]
        kernel = self.job.kernel
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        deadline = time.monotonic() + _CLEANUP_SECONDS
        captured = self._capture_members(kernel, deadline)
        self.job.stop()
        if not captured: return False
        while time.monotonic() < deadline:
            info = Accounting()
            if not kernel.QueryInformationJobObject(self.job.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return False
            # Job 的活动计数可先归零；真实解释器和浏览器的进程对象仍须确认退出。
            states = [kernel.WaitForSingleObject(handle, 0) for handle in self._member_handles.values()]
            if any(state not in (0, 258) for state in states): return False
            if info.active == 0 and all(state == 0 for state in states):
                remaining = deadline - time.monotonic()
                if remaining <= 0: return False
                self.process.wait(timeout=remaining)
                for pid, handle in list(self._member_handles.items()):
                    if not kernel.CloseHandle(handle): return False
                    del self._member_handles[pid]
                self.job.close()
                return True
            time.sleep(_POLL_SECONDS)
        return False


def worker_main(arguments):
    try:
        if len(arguments) != 1: return 2
        request_file = Path(arguments[0])
        if request_file.stat().st_size > 16384: return 2
        request = json.loads(request_file.read_text())
        directory = request_file.parent
        fields = {'version', 'platform', 'data_dir', 'account', 'directory', 'deadline'}
        if (set(request) not in (fields, fields | {'diagnostic'})
                or request['version'] != 1 or str(directory) != request['directory']
                or request['platform'] not in {'douyin', 'kuaishou', 'channels', 'bilibili'}
                or 'diagnostic' in request and sanitize_diagnostic(request['diagnostic']) is None):
            return 2
        from types import SimpleNamespace
        import threading
        paths = SimpleNamespace(data_dir=Path(request['data_dir']))
        diagnostics = CheckDiagnostics(started=float(os.environ.get('P011_SESSION_CHECK_DIAGNOSTIC_STARTED', time.monotonic())),
            initial=_progress(directory) or request.get('diagnostic'),
            publish=lambda value: _write_json(directory / 'progress.json', value))
        if os.name != 'nt':
            threading.Thread(target=_watch_parent,
                             args=(directory, Path(os.environ['BREAKPAD_DUMP_LOCATION']), request['deadline']),
                             name='p011-check-parent-lease', daemon=True).start()
        if request['platform'] == 'douyin':
            from .chromium_publish import make_chromium_login
            provider = make_chromium_login(None, paths)
        else:
            from .creator_providers import make_creator_login_providers
            provider = make_creator_login_providers(None, paths)[request['platform']]
        diagnostics.record('worker_start', 'completed')
        result = provider.check_session._probe(SimpleNamespace(**request['account']), directory / 'browser',
                                               threading.Event(), request['deadline'], diagnostics)
        result = {**result, 'diagnostic': diagnostics.snapshot()}
        _write_json(directory / 'result.json', result)
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        return 2


if __name__ == '__main__':
    arguments = sys.argv[1:]
    if arguments[:1] == ['--browser-gate']:
        raise SystemExit(browser_gate_main(arguments[1:]))
    raise SystemExit(worker_main(arguments))
