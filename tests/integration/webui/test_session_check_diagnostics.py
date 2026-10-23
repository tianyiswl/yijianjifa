"""Safe stage evidence from real provider code and supervised fake SDKs."""
import json
from pathlib import Path
import threading

import pytest

from test_session_check import check_fixture
from test_session_check_hang import real_worker, assert_released, records, wait_for


def diagnostic(stage='identity_first'):
    return {'v': 1, 'stage': stage, 'last_stage': stage, 'elapsed_ms': 20,
            'events': [{'stage': stage, 'state': 'started', 'elapsed_ms': 10}]}


@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_navigation_timeout_has_finite_stage_without_exception_or_credentials(check_fixture, monkeypatch):
    f = check_fixture
    def timeout(*args, **kwargs): raise TimeoutError('PRIVATE UID cookie /Users/private/profile https://secret.invalid/body')
    monkeypatch.setattr(f.page, 'goto', timeout)
    result = f.provider.check_session(f.account.id, f.cancel)
    assert result['state'] == 'unavailable' and result['reason'] == 'timeout'
    value = result.get('diagnostic')
    assert value and value['stage'] == 'navigation_first'
    assert any(e['stage'] == 'navigation_first' and e['state'] == 'failed' and e['error_kind'] == 'timeout' for e in value['events'])
    assert all(text not in json.dumps(value) for text in ('PRIVATE', 'UID', 'cookie', '/Users/', 'secret.invalid'))
    assert f.context.closed and f.service.get(f.account.id).login_state == 'valid'


@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_hard_deadline_retains_last_stage_even_when_sdk_never_returns(check_fixture, monkeypatch, tmp_path):
    from yijianjifa.webui import session_check
    f = check_fixture
    monkeypatch.setattr(session_check, 'CHECK_SECONDS', .8)
    trace = real_worker(f, monkeypatch, tmp_path, 'evaluate')
    result = f.provider.check_session(f.account.id, f.cancel)
    assert result['state'] == 'unavailable' and result['reason'] == 'timeout'
    value = result.get('diagnostic')
    assert value and value['stage'] == 'surface'
    assert any(e['stage'] == 'parent_deadline' and e['state'] == 'failed' for e in value['events'])
    assert value['last_stage'] == 'cleanup'
    assert_released(f, trace)


def test_result_reader_accepts_only_the_fixed_optional_diagnostic_schema(tmp_path):
    from yijianjifa.webui import session_check_process as process
    value = {'state': 'valid', 'platform_user_id': 'synthetic', 'diagnostic': diagnostic()}
    (tmp_path / 'result.json').write_text(json.dumps(value))
    assert process._result(tmp_path) == value
    value['diagnostic']['private_url'] = 'https://private.invalid'
    (tmp_path / 'result.json').write_text(json.dumps(value))
    assert process._result(tmp_path) == {'state': 'unavailable', 'reason': 'invalid_provider_result'}


@pytest.mark.parametrize('change', ['unknown-stage', 'unknown-key', 'bool-time', 'many-events', 'backwards', 'raw-error', 'wrong-version', 'none'])
def test_sanitizer_rejects_malformed_or_private_diagnostic_without_raising(change):
    from yijianjifa.webui.session_check_diagnostics import sanitize_diagnostic
    value = diagnostic()
    if change == 'unknown-stage': value['stage'] = 'https://private.invalid'
    if change == 'unknown-key': value['events'][0]['cookie'] = 'secret'
    if change == 'bool-time': value['elapsed_ms'] = True
    if change == 'many-events': value['events'] *= 33
    if change == 'backwards': value['events'].append({'stage': 'surface', 'state': 'started', 'elapsed_ms': 1})
    if change == 'raw-error': value['events'][0].update(state='failed', error_kind='TimeoutError private body')
    if change == 'wrong-version': value['v'] = 2
    if change == 'none': value = None
    assert sanitize_diagnostic(value) is None


def test_sanitizer_returns_an_independent_bounded_value():
    from yijianjifa.webui.session_check_diagnostics import sanitize_diagnostic
    value = diagnostic()
    cleaned = sanitize_diagnostic(value)
    assert cleaned == value and cleaned is not value and cleaned['events'] is not value['events']
    value['events'][0]['stage'] = 'private'
    assert cleaned['events'][0]['stage'] == 'identity_first'


@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
@pytest.mark.parametrize('stage', ['browser_launch', 'session_restore', 'surface', 'identity_first', 'navigation_second', 'identity_second'])
def test_local_timeouts_keep_the_exact_failed_stage_after_successful_cleanup(check_fixture, monkeypatch, stage):
    from yijianjifa.webui import session_check, chromium_publish, playwright_runtime
    f = check_fixture
    def timeout(*args, **kwargs): raise TimeoutError('secret cookie UID raw body /profile')
    if stage == 'browser_launch':
        monkeypatch.setattr(playwright_runtime, 'acquire_playwright', lambda: type('Driver', (), {
            'exclusive': True, 'chromium': type('Chromium', (), {'launch_persistent_context': staticmethod(timeout)})(), 'stop': lambda self: None})())
    elif stage == 'session_restore':
        monkeypatch.setattr(chromium_publish, 'restore_account_session', timeout)
    elif stage == 'navigation_second':
        original = f.page.goto; count = []
        def navigate(*args, **kwargs):
            count.append(True)
            return timeout() if len(count) == 2 else original(*args, **kwargs)
        monkeypatch.setattr(f.page, 'goto', navigate)
    else:
        original = f.page.evaluate; identities = []
        def evaluate(expression, budget):
            surface = 'p011ReadOnlySurface' in expression
            if not surface: identities.append(True)
            if ((stage == 'surface' and surface) or
                    (stage == 'identity_first' and not surface and len(identities) == 1) or
                    (stage == 'identity_second' and not surface and len(identities) == 2)):
                return timeout()
            return original(expression, budget)
        monkeypatch.setattr(f.page, 'evaluate', evaluate)
    result = f.provider.check_session(f.account.id, f.cancel)
    assert result['state'] == 'unavailable' and result['reason'] == 'timeout'
    assert result['diagnostic']['stage'] == stage
    assert result['diagnostic']['last_stage'] == 'cleanup'
    assert len(result['diagnostic']['events']) <= 32
    assert f.service.get(f.account.id).login_state == 'valid'


@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
@pytest.mark.parametrize('sdk_stage,reported', [('close', 'context_close'), ('stop', 'driver_stop')])
@pytest.mark.parametrize('worker_start_delay', [0, 1], ids=['normal-start', 'slow-start'])
def test_parent_deadline_reports_cleanup_hangs_without_accepting_prior_valid_identity(
        check_fixture, monkeypatch, tmp_path, sdk_stage, reported, worker_start_delay):
    import time
    from types import SimpleNamespace
    from yijianjifa.webui import session_check, session_check_process
    f = check_fixture
    trace = real_worker(f, monkeypatch, tmp_path, sdk_stage)
    command = session_check_process._command
    bootstrap = ('import runpy,sys,time;time.sleep(float(sys.argv.pop(1)));'
                 'sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name="__main__")')
    monkeypatch.setattr(session_check_process, '_command', lambda kind='worker':
                        [command(kind)[0], '-c', bootstrap, str(worker_start_delay), *command(kind)[1:]]
                        if kind == 'worker' else command(kind))
    # This checks a cleanup hang, not fresh-interpreter import speed. Keep the
    # real worker's normal startup budget, then cross the supervisor deadline
    # only after the fake SDK has actually entered the requested blocking call.
    # Patch this module's clock only; the child and diagnostic clock stay real.
    offset = 0
    def parent_time():
        nonlocal offset
        if not offset and any(row['event'] == sdk_stage for row in records(trace)):
            offset = session_check.CHECK_SECONDS + 1
        return time.monotonic() + offset
    monkeypatch.setattr(session_check_process, 'time',
                        SimpleNamespace(monotonic=parent_time, sleep=time.sleep))
    result = f.provider.check_session(f.account.id, f.cancel)
    assert result['state'] == 'unavailable' and result['reason'] == 'timeout'
    assert result['diagnostic']['stage'] == reported
    assert any(row['event'] == sdk_stage for row in records(trace))
    assert_released(f, trace)


@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_full_real_probe_stage_sequence_and_four_argument_seam_remain_supported(check_fixture):
    f = check_fixture
    result = f.provider.check_session(f.account.id, f.cancel)
    assert result['state'] == 'valid' and result['platform_user_id'] == f.uid
    value = result['diagnostic']
    assert value['stage'] == 'identity_second' and value['last_stage'] == 'cleanup'
    completed = [event['stage'] for event in value['events'] if event['state'] == 'completed']
    assert completed == ['session_read', 'profile_read', 'clone', 'driver_start', 'browser_launch',
                         'session_restore', 'navigation_first', 'surface', 'identity_first',
                         'navigation_second', 'surface', 'identity_second', 'context_close', 'driver_stop', 'cleanup']


def test_checkpoint_reader_discards_invalid_or_oversized_private_progress(tmp_path):
    from yijianjifa.webui.session_check_process import _progress
    path = tmp_path / 'progress.json'
    path.write_text(json.dumps({**diagnostic(), 'url': 'https://private.invalid'}))
    assert _progress(tmp_path) is None
    path.write_text(' ' * 16385)
    assert _progress(tmp_path) is None


def test_progress_history_remains_bounded_and_preserves_the_primary_failure():
    from yijianjifa.webui.session_check_diagnostics import CheckDiagnostics, sanitize_diagnostic
    records = []; value = CheckDiagnostics(publish=records.append)
    for _ in range(40):
        value.record('surface', 'started'); value.record('surface', 'completed')
    value.record('identity_second', 'failed', 'timeout')
    value.record('cleanup', 'started'); value.record('cleanup', 'completed')
    final = value.snapshot()
    assert len(final['events']) == 32
    assert final['stage'] == 'identity_second' and final['last_stage'] == 'cleanup'
    assert all(sanitize_diagnostic(record) is not None for record in records)


@pytest.mark.parametrize('check_fixture', ['douyin'], indirect=True)
def test_cancellation_keeps_safe_checkpoint_and_reaps_the_owned_worker(check_fixture, monkeypatch, tmp_path):
    from yijianjifa.webui import session_check
    f = check_fixture
    monkeypatch.setattr(session_check, 'CHECK_SECONDS', 3)
    trace = real_worker(f, monkeypatch, tmp_path, 'evaluate')
    results = []
    worker = threading.Thread(target=lambda: results.append(f.provider.check_session(f.account.id, f.cancel)))
    worker.start()
    try:
        wait_for(lambda: any(row['event'] == 'evaluate' for row in records(trace)))
        f.cancel.set(); worker.join(3)
        assert not worker.is_alive()
        result = results[0]
        assert result['state'] == 'unavailable' and result['reason'] == 'cancelled'
        assert result['diagnostic']['stage'] == 'surface'
        assert result['diagnostic']['last_stage'] == 'cleanup'
        assert_released(f, trace)
    finally:
        f.cancel.set(); worker.join(3)


def test_completed_valid_identity_is_not_blamed_when_only_parent_cleanup_crosses_deadline(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from yijianjifa.webui import session_check_process as owned
    clock = SimpleNamespace(now=10.0)
    value = {'state': 'valid', 'platform_user_id': 'synthetic', 'diagnostic': {
        'v': 1, 'stage': 'identity_second', 'last_stage': 'cleanup', 'elapsed_ms': 3,
        'events': [{'stage': 'identity_second', 'state': 'started', 'elapsed_ms': 0},
                   {'stage': 'identity_second', 'state': 'completed', 'elapsed_ms': 2},
                   {'stage': 'cleanup', 'state': 'completed', 'elapsed_ms': 3}]}}
    (tmp_path / 'result.json').write_text(json.dumps(value))
    class Process:
        pid = 12345
        returncode = 0
        stdin = None
        def poll(self): return 0
    def finished_cleanup(*args):
        clock.now = 12.0
        return True
    class Job:
        def __init__(self, process): pass
        stop = staticmethod(finished_cleanup)
    monkeypatch.setattr(owned, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _: None))
    monkeypatch.setattr(owned.subprocess, 'Popen', lambda *args, **kwargs: Process())
    monkeypatch.setattr(owned, '_stop_posix', finished_cleanup)
    monkeypatch.setattr(owned, '_WindowsCheckJob', Job)
    result, cleaned = owned.run_check({'directory': str(tmp_path), 'deadline': 11.0}, threading.Event())
    assert cleaned and result['state'] == 'unavailable' and result['reason'] == 'timeout'
    assert any(e['stage'] == 'identity_second' and e['state'] == 'completed' for e in result['diagnostic']['events'])
    assert result['diagnostic']['stage'] == 'parent_deadline'
    assert result['diagnostic']['last_stage'] == 'parent_deadline'
