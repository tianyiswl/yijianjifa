"""The native login queue never performs media or platform publishing work."""
import importlib
import importlib.util
import threading
from types import SimpleNamespace

import pytest


def runtime_type():
    name = 'yijianjifa.app_services.daily_login_runtime'
    assert importlib.util.find_spec(name) is not None, 'daily login queue is not implemented'
    return importlib.import_module(name).DailyLoginRuntime


def target(account_id='a'):
    return {'account_id': account_id, 'platform': 'douyin', 'platform_user_id': '111',
            'session_fingerprint': 'original-version', 'nickname': '合成账号'}


def wait(event):
    assert event.wait(2), 'native login job did not reach the expected boundary'


def test_account_on_hold_is_not_opened_or_reported_as_verified():
    done=threading.Event();calls=[];observed=[]
    def check(account_id,cancel):calls.append(account_id);return {'state':'valid','platform_user_id':'111'}
    runtime=runtime_type()(SimpleNamespace(check_session=check),lambda task:[{**target('a'),'deferred_reason':'原结果待核对'},target('b')],
        lambda task,**data:(observed.extend(data['login_accounts']),done.set()),lambda *args:pytest.fail('暂缓账号不能冒充全部验证成功'),lambda task:True)
    try:
        runtime.start('batch');wait(done)
        assert calls==['b']
        assert [o['state'] for o in observed]==['deferred','valid']
    finally:assert runtime.close(2)


def test_all_accounts_are_checked_serially_and_only_complete_valid_results_are_accepted():
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    calls, accepted, updates = [], [], []
    def check(account_id, cancel):
        calls.append((account_id, threading.get_ident()))
        if account_id == 'a':
            entered.set()
            wait(release)
        return {'state': 'valid', 'platform_user_id': '111'}
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target('a'), target('b')],
                             lambda *a, **k: updates.append((a, k)),
                             lambda task, observations: (accepted.append((task, observations)), done.set()),
                             lambda task: True)
    try:
        assert runtime.start('original-batch') is True
        wait(entered)
        assert runtime.start('original-batch') is False
        assert accepted == []
        release.set()
        wait(done)
        assert [account for account, _ in calls] == ['a', 'b']
        assert len({thread for _, thread in calls}) == 1
        assert accepted[0][0] == 'original-batch'
        assert [row['account_id'] for row in accepted[0][1]] == ['a', 'b']
        assert accepted[0][1][0]['session_fingerprint'] == 'original-version'
        assert accepted[0][1][0]['nickname'] == '合成账号'
        assert updates == []
    finally:
        release.set()
        assert runtime.close(2)


@pytest.mark.parametrize(('result', 'phase', 'state'), [
    ({'state': 'login_required', 'reason': 'expired'}, 'needs_login', 'login_required'),
    ({'state': 'identity_mismatch', 'platform_user_id': '222'}, 'needs_attention', 'identity_mismatch'),
    ({'state': 'unavailable', 'reason': 'network'}, 'needs_attention', 'unavailable'),
    ({'state': 'valid', 'platform_user_id': '222'}, 'needs_attention', 'identity_mismatch'),
    ({'state': 'valid'}, 'needs_attention', 'unavailable'),
    ({'state': 'unexpected'}, 'needs_attention', 'unavailable'),
])
def test_one_unverified_account_never_accepts_the_batch_but_other_accounts_are_still_checked(result, phase, state):
    done = threading.Event()
    checked, accepted, updates = [], [], []
    def check(account_id, cancel):
        checked.append(account_id)
        return result if account_id == 'a' else {'state': 'valid', 'platform_user_id': '111'}
    def update(task, **values):
        updates.append((task, values))
        done.set()
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target('a'), target('b')],
                             update, lambda *args: accepted.append(args), lambda task: True)
    try:
        runtime.start('batch')
        wait(done)
        assert checked == ['a', 'b']
        assert accepted == []
        assert updates[0][0] == 'batch'
        assert updates[0][1]['phase'] == phase
        assert updates[0][1]['login_accounts'][0]['state'] == state
        assert updates[0][1]['login_accounts'][1]['state'] == 'valid'
    finally:
        assert runtime.close(2)


def test_cancelled_generation_cannot_write_back_after_same_task_is_restarted():
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    checks, accepted, updates, cancellations = [], [], [], []
    def check(account_id, cancel):
        checks.append(account_id)
        if len(checks) == 1:
            cancellations.append(cancel)
            entered.set()
            wait(release)
            return {'state': 'valid', 'platform_user_id': '111', 'proof': 'stale'}
        return {'state': 'valid', 'platform_user_id': '111', 'proof': 'fresh'}
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target()],
                             lambda *a, **k: updates.append((a, k)),
                             lambda task, rows: (accepted.append(rows), done.set()), lambda task: True)
    try:
        runtime.start('batch')
        wait(entered)
        runtime.cancel('batch')
        assert cancellations[0].is_set()
        assert runtime.start('batch') is True
        release.set()
        wait(done)
        assert len(accepted) == 1 and accepted[0][0]['proof'] == 'fresh'
        assert updates == []
    finally:
        release.set()
        assert runtime.close(2)


def test_close_cancels_the_inflight_check_and_never_resurrects_a_task():
    entered, release = threading.Event(), threading.Event()
    writes, cancellation = [], []
    def check(account_id, cancel):
        cancellation.append(cancel)
        entered.set()
        wait(release)
        return {'state': 'valid', 'platform_user_id': '111'}
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target()],
                             lambda *a, **k: writes.append('update'),
                             lambda *a: writes.append('accept'), lambda task: True)
    try:
        runtime.start('batch')
        wait(entered)
        assert runtime.close(0) is False
        assert cancellation[0].is_set()
        assert runtime.start('later') is False
        release.set()
        assert runtime.close(2) is True
        assert writes == []
    finally:
        release.set()
        runtime.close(2)


def test_task_control_guard_discards_an_online_result_after_the_task_stops():
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    current, writes = [True], []
    def check(account_id, cancel):
        if account_id == 'batch':
            entered.set()
            wait(release)
        return {'state': 'valid', 'platform_user_id': '111'}
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target(task)],
                             lambda *a, **k: writes.append('update'),
                             lambda task, rows: done.set() if task == 'marker' else writes.append('accept'),
                             lambda task: task == 'marker' or current[0])
    try:
        runtime.start('batch')
        wait(entered)
        runtime.start('marker')
        current[0] = False
        release.set()
        wait(done)
    finally:
        release.set()
        assert runtime.close(2)
    assert writes == []


def test_checker_exception_is_safe_attention_and_does_not_stop_the_next_job():
    done = threading.Event()
    accepted, updates = [], []
    def check(account_id, cancel):
        if account_id == 'a':
            raise RuntimeError('PRIVATE_COOKIE must never appear in a callback')
        return {'state': 'valid', 'platform_user_id': '111'}
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target(task)],
                             lambda task, **values: updates.append((task, values)),
                             lambda task, rows: (accepted.append(task), done.set()), lambda task: True)
    try:
        runtime.start('a')
        runtime.start('b')
        wait(done)
        assert accepted == ['b']
        assert updates[0][1]['phase'] == 'needs_attention'
        assert updates[0][1]['login_accounts'][0]['reason'] == 'login_check_failed'
        assert 'PRIVATE_COOKIE' not in repr(updates)
    finally:
        assert runtime.close(2)


def test_queue_is_bounded_and_cancel_releases_capacity_without_stale_begin():
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    begun, accepted = [], []
    def begin(task):
        begun.append(task)
        return [target()]
    def check(account_id, cancel):
        entered.set()
        wait(release)
        return {'state': 'valid', 'platform_user_id': '111'}
    def accept(task, rows):
        accepted.append(task)
        if task == 'third':
            done.set()
    runtime = runtime_type()(SimpleNamespace(check_session=check), begin, lambda *a, **k: None,
                             accept, lambda task: True, max_pending=2)
    try:
        runtime.start('first')
        wait(entered)
        runtime.start('cancelled')
        with pytest.raises(ValueError, match='daily_login_busy'):
            runtime.start('third')
        runtime.cancel('cancelled')
        assert runtime.start('third')
        release.set()
        wait(done)
        assert begun == ['first', 'third']
        assert accepted == ['first', 'third']
    finally:
        release.set()
        assert runtime.close(2)


@pytest.mark.parametrize('failure', ['begin', 'update', 'accept'])
def test_callback_failure_cannot_kill_later_jobs_or_expose_private_exception_text(failure):
    done = threading.Event()
    accepted, updates = [], []
    def begin(task):
        if task == 'broken' and failure == 'begin':
            raise RuntimeError('PRIVATE_CALLBACK_DATA')
        return [target(task)]
    def check(account_id, cancel):
        if account_id == 'broken' and failure == 'update':
            return {'state': 'login_required'}
        return {'state': 'valid', 'platform_user_id': '111'}
    def update(task, **values):
        updates.append((task, values))
        if task == 'broken' and failure == 'update':
            raise RuntimeError('PRIVATE_CALLBACK_DATA')
    def accept(task, rows):
        if task == 'broken' and failure == 'accept':
            raise RuntimeError('PRIVATE_CALLBACK_DATA')
        accepted.append(task)
        done.set()
    runtime = runtime_type()(SimpleNamespace(check_session=check), begin, update, accept, lambda task: True)
    try:
        runtime.start('broken')
        runtime.start('healthy')
        wait(done)
        assert accepted == ['healthy']
        assert updates and updates[-1][1]['phase'] == 'needs_attention'
        assert 'PRIVATE_CALLBACK_DATA' not in repr(updates)
    finally:
        assert runtime.close(2)


@pytest.mark.parametrize(('reason', 'state'), [
    ('daily_login_identity_mismatch', 'identity_mismatch'),
    ('daily_target_changed', 'unavailable'),
    ('daily_schedule_invalid', 'unavailable'),
    ('daily_authorization_required', 'unavailable'),
])
def test_native_failure_keeps_its_finite_reason_instead_of_claiming_network_failure(reason, state):
    done = threading.Event()
    updates = []
    class IdentityChanged(ValueError):
        code = reason
    def begin(task):
        raise IdentityChanged('PRIVATE_IDENTITY_TEXT')
    def update(task, **values):
        updates.append(values)
        done.set()
    runtime = runtime_type()(SimpleNamespace(check_session=lambda *a: None), begin, update,
                             lambda *a: pytest.fail('identity failure must not accept'), lambda task: True)
    try:
        runtime.start('batch')
        wait(done)
        assert updates[0]['phase'] == 'needs_attention'
        assert updates[0]['login_accounts'] == [{'state': state, 'reason': reason}]
        assert 'PRIVATE_IDENTITY_TEXT' not in repr(updates)
    finally:
        assert runtime.close(2)


def test_next_phase_requested_inside_accept_runs_once_after_the_current_callback():
    done = threading.Event()
    checks, accepted, starts = [], [], []
    def check(account_id, cancel):
        checks.append(account_id)
        return {'state': 'valid', 'platform_user_id': '111'}
    def accept(task, rows):
        accepted.append(task)
        if len(accepted) == 1:
            starts.extend([runtime.start(task), runtime.start(task)])
        else:
            done.set()
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target()],
                             lambda *a, **k: None, accept, lambda task: True)
    try:
        runtime.start('one-batch-two-phases')
        wait(done)
        assert starts == [True, True]
        assert checks == ['a', 'a'] and accepted == ['one-batch-two-phases'] * 2
    finally:
        assert runtime.close(2)


def test_cancelling_during_accept_discards_its_requested_next_phase():
    entered, release = threading.Event(), threading.Event()
    checks = []
    def check(account_id, cancel):
        checks.append(account_id)
        return {'state': 'valid', 'platform_user_id': '111'}
    def accept(task, rows):
        runtime.start(task)
        entered.set()
        wait(release)
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target()],
                             lambda *a, **k: None, accept, lambda task: True)
    try:
        runtime.start('batch')
        wait(entered)
        runtime.cancel('batch')
        release.set()
        assert runtime.close(2)
        assert checks == ['a']
    finally:
        release.set()
        runtime.close(2)


def test_progress_reports_each_real_check_boundary_and_cannot_interrupt_checks_when_writes_fail():
    done = threading.Event()
    events, checks, accepted = [], [], []
    def progress(task, **event):
        assert event['guard']()
        events.append((task, {k: v for k, v in event.items() if k != 'guard'}))
        if event['stage'] == 'started':
            raise RuntimeError('PRIVATE_PROGRESS_WRITE_FAILED')
    def check(account, cancel):
        checks.append(account)
        return {'state': 'valid', 'platform_user_id': '111'}
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target('a'), target('b')],
                             lambda *a, **k: pytest.fail('progress writes cannot fail the batch'),
                             lambda task, rows: (accepted.append(task), done.set()), lambda task: True,
                             progress=progress)
    try:
        runtime.start('batch'); wait(done)
        assert checks == ['a', 'b'] and accepted == ['batch']
        assert [(v['index'], v['total'], v['account_id'], v['stage']) for _, v in events] == [
            (1, 2, 'a', 'started'), (1, 2, 'a', 'finished'), (2, 2, 'b', 'started'), (2, 2, 'b', 'finished')]
        assert events[1][1]['state'] == 'valid'
        assert all('platform_user_id' not in v and 'session_fingerprint' not in v for _, v in events)
    finally:
        assert runtime.close(2)


def test_cancelled_check_has_no_finished_progress_and_its_progress_guard_expires():
    entered, release = threading.Event(), threading.Event()
    events, guards = [], []
    def progress(task, **event):
        events.append(event['stage']); guards.append(event['guard'])
    def check(account, cancel):
        entered.set(); wait(release)
        return {'state': 'valid', 'platform_user_id': '111'}
    runtime = runtime_type()(SimpleNamespace(check_session=check), lambda task: [target()],
                             lambda *a, **k: pytest.fail('cancelled check cannot update'),
                             lambda *a: pytest.fail('cancelled check cannot accept'), lambda task: True,
                             progress=progress)
    try:
        runtime.start('batch'); wait(entered); runtime.cancel('batch')
        assert guards[0]() is False
        release.set(); assert runtime.close(2)
        assert events == ['started']
    finally:
        release.set(); runtime.close(2)


def test_progress_reason_is_finite_even_if_a_checker_returns_private_text():
    done = threading.Event(); events = []
    runtime = runtime_type()(SimpleNamespace(check_session=lambda *a: {'state': 'unavailable', 'reason': 'PRIVATE_LONG_ERROR'}),
                             lambda task: [target()], lambda *a, **k: done.set(), lambda *a: None,
                             lambda task: True, progress=lambda task, **event: events.append(event))
    try:
        runtime.start('batch'); wait(done)
        assert events[-1]['state'] == 'unavailable'
        assert events[-1]['reason'] == ''
    finally:
        assert runtime.close(2)


def test_finite_diagnostic_reaches_progress_and_attention_without_changing_identity_refs():
    done = threading.Event(); events, updates = [], []
    evidence = {'v': 1, 'stage': 'navigation_first', 'last_stage': 'cleanup', 'elapsed_ms': 25000,
                'events': [{'stage': 'navigation_first', 'state': 'failed', 'elapsed_ms': 24000, 'error_kind': 'timeout'}]}
    result = {'state': 'unavailable', 'reason': 'timeout', 'diagnostic': evidence}
    runtime = runtime_type()(SimpleNamespace(check_session=lambda *a: result), lambda task: [target()],
        lambda task, **values: (updates.append(values), done.set()), lambda *a: pytest.fail('timeout cannot accept'),
        lambda task: True, progress=lambda task, **event: events.append(event))
    try:
        runtime.start('batch'); wait(done)
        assert events[-1]['diagnostic'] == evidence
        row = updates[-1]['login_accounts'][0]
        assert row['diagnostic'] == evidence and row['session_fingerprint'] == 'original-version'
        evidence['events'][0]['elapsed_ms'] = 23000
        assert row['diagnostic']['events'][0]['elapsed_ms'] == 24000
    finally:
        assert runtime.close(2)


@pytest.mark.parametrize('evidence', [None, {}, {'v': 1, 'stage': 'PRIVATE_STAGE'}])
def test_invalid_diagnostic_from_direct_checker_cannot_become_a_valid_observation(evidence):
    observation = runtime_type()._observation(target(),
        {'state': 'valid', 'platform_user_id': '111', 'diagnostic': evidence})
    assert observation['state'] == 'unavailable'
    assert observation['reason'] == 'login_check_invalid_response'
    assert 'diagnostic' not in observation
