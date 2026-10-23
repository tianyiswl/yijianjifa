"""Finite check-stage evidence only: no browser data or exception messages."""
import time

STAGES = frozenset({'session_read', 'profile_read', 'clone', 'driver_start', 'browser_launch',
    'session_restore', 'navigation_first', 'navigation_second', 'surface', 'identity_first',
    'identity_second', 'context_close', 'driver_stop', 'cleanup', 'worker_start', 'parent_deadline'})
ERROR_KINDS = frozenset({'timeout', 'cancelled', 'network', 'verification_required',
    'identity_unavailable', 'session_missing', 'session_unavailable', 'cleanup_failed', 'navigation', 'other'})
MAX_MS = 120000
MAX_BYTES = 16384
_CLEANUP = frozenset({'context_close', 'driver_stop', 'cleanup'})


def _milliseconds(value):
    return type(value) is int and 0 <= value <= MAX_MS


def sanitize_diagnostic(value):
    """A strict optional DTO: invalid present evidence is None, never partly trusted."""
    if (type(value) is not dict or set(value) != {'v', 'stage', 'last_stage', 'elapsed_ms', 'events'}
            or type(value['v']) is not int or value['v'] != 1
            or any(type(value[k]) is not str or value[k] not in STAGES for k in ('stage', 'last_stage'))
            or not _milliseconds(value['elapsed_ms']) or type(value['events']) is not list
            or len(value['events']) > 32):
        return None
    events = []; previous = 0
    for event in value['events']:
        if (type(event) is not dict or set(event) not in (
                {'stage', 'state', 'elapsed_ms'}, {'stage', 'state', 'elapsed_ms', 'error_kind'})
                or type(event['stage']) is not str or event['stage'] not in STAGES
                or type(event['state']) is not str or event['state'] not in {'started', 'completed', 'failed'}
                or not _milliseconds(event['elapsed_ms']) or not previous <= event['elapsed_ms'] <= value['elapsed_ms']
                or 'error_kind' in event and (event['state'] != 'failed' or type(event['error_kind']) is not str
                                              or event['error_kind'] not in ERROR_KINDS)):
            return None
        previous = event['elapsed_ms']; events.append(dict(event))
    return {**value, 'events': events}


def error_kind(error):
    # This classifier reads a few known markers but never exports exception text.
    reason = getattr(error, 'reason', None)
    if type(reason) is str and reason in ERROR_KINDS:
        return reason
    message = str(error)
    if isinstance(error, TimeoutError) or type(error).__name__ == 'TimeoutError' or 'session-check-timeout' in message:
        return 'timeout'
    if any(marker in message for marker in ('Execution context was destroyed', 'Cannot find context with specified id')):
        return 'navigation'
    return 'network' if type(error).__module__.startswith('playwright.') else 'other'


class CheckDiagnostics:
    """Record before a potentially stuck SDK call; publishing is best effort IO."""
    def __init__(self, *, started=None, initial=None, publish=None):
        clean = sanitize_diagnostic(initial)
        self.started = time.monotonic() - (clean['elapsed_ms'] / 1000 if clean else 0) if started is None else started
        self.events = clean['events'] if clean else []
        self.stage = clean['stage'] if clean else 'session_read'
        self.last_stage = clean['last_stage'] if clean else self.stage
        self.main_stage = self.stage
        self.failed = any(e['state'] == 'failed' for e in self.events)
        self.publish = publish

    def record(self, stage, state, error_kind=None, *, preserve_stage=False):
        if stage not in STAGES or state not in {'started', 'completed', 'failed'}:
            raise ValueError('invalid native diagnostic stage')
        elapsed = self.snapshot()['elapsed_ms']
        event = {'stage': stage, 'state': state, 'elapsed_ms': elapsed}
        if error_kind is not None:
            if state != 'failed' or error_kind not in ERROR_KINDS:
                raise ValueError('invalid native diagnostic kind')
            event['error_kind'] = error_kind
        self.last_stage = stage
        if not self.failed and not preserve_stage:
            self.stage = self.main_stage if stage in _CLEANUP and state == 'completed' else stage
            if stage not in _CLEANUP: self.main_stage = stage
        if state == 'failed': self.failed = True
        self.events = [*self.events[-31:], event]
        if self.publish is not None:
            try: self.publish(self.snapshot())
            except (OSError, ValueError, TypeError): pass  # Evidence cannot change an observed login result.

    __call__ = record

    def snapshot(self):
        elapsed = max(0, min(MAX_MS, int((time.monotonic() - self.started) * 1000)))
        if self.events: elapsed = max(elapsed, self.events[-1]['elapsed_ms'])
        return {'v': 1, 'stage': self.stage, 'last_stage': self.last_stage,
                'elapsed_ms': elapsed, 'events': [dict(e) for e in self.events]}

    def call(self, stage, operation):
        self.record(stage, 'started')
        try: result = operation()
        except Exception as error:
            self.record(stage, 'failed', error_kind(error))
            raise
        self.record(stage, 'completed')
        return result
