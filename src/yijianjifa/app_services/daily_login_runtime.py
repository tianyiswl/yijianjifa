"""Bounded native-only account checks, with no database or publishing ownership.

Callbacks own their short transactions and must recheck task control/identity
inside those transactions. The worker never holds its lock across a callback or
an online check; a blocked platform must not block cancellation or app shutdown.
"""
from collections import deque
import copy
from dataclasses import dataclass, field
import threading

from yijianjifa.webui.session_check_diagnostics import sanitize_diagnostic


_ERROR_REASONS = {'daily_login_identity_mismatch', 'daily_login_check_failed', 'daily_target_changed',
                  'daily_schedule_invalid', 'daily_authorization_required'}
_PROGRESS_REASONS = {'timeout', 'network_unavailable', 'verification_required', 'session_unavailable',
                     'platform_unavailable', 'identity_unavailable', 'session_missing', 'provider_unavailable',
                     'cleanup_failed', 'cancelled', 'session_changed', 'account_busy', 'login_in_progress',
                     'invalid_provider_result', 'login_check_failed', 'login_check_invalid_response',
                     'account_mismatch', 'login_required'}


@dataclass(eq=False)
class _Job:
    task_id: str
    generation: int
    cancel: threading.Event = field(default_factory=threading.Event)
    delivering: bool = False
    followup_requested: bool = False


class DailyLoginRuntime:
    def __init__(self, login_runtime, begin, update, accept, is_current, *, max_pending=16, progress=None):
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError('invalid daily login queue limit')
        self.login_runtime = login_runtime
        self.begin = begin
        self.update = update
        self.accept = accept
        self.is_current = is_current
        self.progress = progress
        self.max_pending = max_pending
        self._condition = threading.Condition()
        self._queue = deque()
        self._jobs = {}
        self._generation = 0
        self._closed = False
        self._thread = None

    def start(self, task_id):
        if type(task_id) is not str or not 1 <= len(task_id) <= 64:
            raise ValueError('invalid daily login task')
        with self._condition:
            if self._closed:
                return False
            previous = self._jobs.get(task_id)
            if previous is not None:
                # Accepting the first check can finish a tiny making batch and
                # request its before-publication check before this job returns.
                if previous.delivering:
                    previous.followup_requested = True
                    return True
                return False
            if len(self._jobs) >= self.max_pending:
                raise ValueError('daily_login_busy')
            self._generation += 1
            job = _Job(task_id, self._generation)
            self._jobs[task_id] = job
            self._queue.append(job)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='p011-daily-login', daemon=False)
                self._thread.start()
            self._condition.notify()
            return True

    def cancel(self, task_id):
        with self._condition:
            job = self._jobs.pop(task_id, None)
            if job is not None:
                job.cancel.set()
            self._queue = deque(queued for queued in self._queue if queued.task_id != task_id)
            self._condition.notify_all()

    def close(self, timeout=0):
        with self._condition:
            self._closed = True
            for job in self._jobs.values():
                job.cancel.set()
            self._jobs.clear()
            self._queue.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _active(self, job):
        with self._condition:
            return not self._closed and not job.cancel.is_set() and self._jobs.get(job.task_id) is job

    def _current(self, job):
        # No runtime lock is held while a service reads its task-control state.
        return self._active(job) and self.is_current(job.task_id) and self._active(job)

    @staticmethod
    def _observation(target, result):
        if type(result) is not dict or result.get('state') not in {'valid', 'login_required', 'identity_mismatch', 'unavailable'}:
            result = {'state': 'unavailable', 'reason': 'login_check_invalid_response'}
        elif 'diagnostic' in result:
            diagnostic = sanitize_diagnostic(result['diagnostic'])
            result = ({**result, 'diagnostic': diagnostic} if diagnostic is not None else
                      {'state': 'unavailable', 'reason': 'login_check_invalid_response'})
        observation = {**copy.deepcopy(target), **copy.deepcopy(result)}
        # The checker cannot redirect a result to another selected account.
        observation.update(account_id=target['account_id'], platform=target['platform'])
        if result['state'] == 'valid':
            uid = result.get('platform_user_id')
            if type(uid) is not str or not uid:
                observation.update(state='unavailable', reason='login_check_invalid_response')
            elif uid != target['platform_user_id']:
                observation.update(state='identity_mismatch', reason='account_mismatch')
        return observation

    def _run_job(self, job):
        if not self._current(job):
            return
        targets = self.begin(job.task_id)
        if targets is None or not self._current(job):
            return
        if (type(targets) is not list or not 1 <= len(targets) <= 200
                or any(type(target) is not dict or any(type(target.get(key)) is not str or not target[key]
                       for key in ('account_id', 'platform', 'platform_user_id', 'session_fingerprint'))
                       for target in targets)):
            raise ValueError('invalid daily login targets')
        targets = copy.deepcopy(targets)
        observations = []
        for index, target in enumerate(targets, 1):
            if not self._current(job):
                return
            if target.get('deferred_reason'):
                observations.append({**target,'state':'deferred'})
                continue
            self._progress(job, index, len(targets), target['account_id'], 'started')
            if not self._current(job):
                return
            try:
                result = self.login_runtime.check_session(target['account_id'], job.cancel)
            except Exception:
                result = {'state': 'unavailable', 'reason': 'login_check_failed'}
            if not self._current(job):
                return
            observation = self._observation(target, result)
            self._progress(job, index, len(targets), target['account_id'], 'finished',
                           state=observation['state'], reason=observation.get('reason', ''),
                           diagnostic=observation.get('diagnostic'))
            observations.append(observation)
        if not self._current(job):
            return
        with self._condition:
            if not self._active(job):
                return
            job.delivering = True
        if all(observation['state'] == 'valid' for observation in observations):
            self.accept(job.task_id, observations)
        else:
            phase = ('needs_attention' if any(row['state'] in {'identity_mismatch', 'unavailable'} for row in observations)
                     else 'needs_login')
            self.update(job.task_id, phase=phase, login_accounts=observations)

    def _progress(self, job, index, total, account_id, stage, *, state='', reason='', diagnostic=None):
        if self.progress is None or not self._current(job):
            return
        try:
            reason = reason if type(reason) is str and reason in _PROGRESS_REASONS else ''
            safe = sanitize_diagnostic(diagnostic)
            self.progress(job.task_id, index=index, total=total, account_id=account_id,
                          stage=stage, state=state, reason=reason, guard=lambda: self._active(job),
                          **({'diagnostic': safe} if safe is not None else {}))
        except Exception:
            # Display evidence cannot change the check result or stop other accounts.
            pass

    def _run(self):
        while True:
            with self._condition:
                while not self._closed and not self._queue:
                    self._condition.wait()
                if self._closed:
                    return
                job = self._queue.popleft()
            try:
                self._run_job(job)
            except Exception as error:
                try:
                    if self._current(job):
                        code = getattr(error, 'code', None)
                        reason = code if code in _ERROR_REASONS else 'login_check_failed'
                        state = 'identity_mismatch' if reason == 'daily_login_identity_mismatch' else 'unavailable'
                        self.update(job.task_id, phase='needs_attention',
                                    login_accounts=[{'state': state, 'reason': reason}])
                except Exception:
                    # A failed status write cannot terminate other native jobs.
                    pass
            finally:
                with self._condition:
                    if self._jobs.get(job.task_id) is job:
                        del self._jobs[job.task_id]
                        if job.followup_requested and not self._closed and not job.cancel.is_set():
                            self._generation += 1
                            following = _Job(job.task_id, self._generation)
                            self._jobs[job.task_id] = following
                            self._queue.append(following)
