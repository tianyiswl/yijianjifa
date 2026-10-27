"""Finite checker evidence reaches saved batch logs, panel, detail and clipboard."""
import copy
import json

import pytest

from yijianjifa.app_services.task_center_access import TaskCenterAccess
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_daily_login_flow import flow, submit, phase, until
from test_daily_mixing import call


def diagnostic(stage='navigation_first'):
    return {'v': 1, 'stage': stage, 'last_stage': 'cleanup', 'elapsed_ms': 25000,
            'events': [{'stage': 'browser_launch', 'state': 'started', 'elapsed_ms': 10},
                       {'stage': 'browser_launch', 'state': 'completed', 'elapsed_ms': 100},
                       {'stage': stage, 'state': 'started', 'elapsed_ms': 150},
                       {'stage': stage, 'state': 'failed', 'elapsed_ms': 24900, 'error_kind': 'timeout'}]}


@pytest.mark.parametrize(('stage', 'message'), [
    ('navigation_first', '打开抖音后台超时'),
    ('identity_first', '读取账号身份超时'),
    ('browser_launch', '浏览器启动超时'),
    ('identity_second', '再次读取账号身份超时'),
])
def test_real_safe_diagnostic_has_specific_reason_and_persisted_truthful_steps(flow, stage, message):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': 'unavailable', 'reason': 'timeout', 'diagnostic': diagnostic(stage)}
    identity = submit(flow)
    result = phase(api, identity, 'needs_attention')
    assert message in result['reason']
    assert result['can_continue'] and not result['publish_task_ids']
    assert api._mix.executor.renderer.index == 0
    assert AccountService(api._engine).get(account.id).login_state == 'valid'
    with session_scope(api._engine) as session:
        saved = json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)
    steps = [row for row in saved['login_log'] if row['step'] == 'daily.login_check_step']
    assert len(steps) == 4
    assert [row['elapsed_ms'] for row in steps] == [10, 100, 150, 24900]
    assert all(row['time'] and row['stage'] == 'before_make' for row in steps)
    assert all('检查通过' not in row['message'] for row in steps)
    assert '完成' in steps[1]['message'] and '累计' in steps[1]['message']
    assert message in saved['login_current']['message']
    events = [row for row in result['execution']['events'] if row['step'] == 'daily.login_check_step']
    assert events == []
    assert any('超时' in event['message'] for event in result['execution']['events'])
    detail = call(api, 'task.detail', {'id': identity})
    assert detail['ok'], detail
    assert not any(row['step'] == 'daily.login_check_step' for row in detail['data']['execution']['events'])
    assert any('超时' in row['message'] for row in detail['data']['execution']['events'])
    center = call(api, 'task.center.detail', {'id': identity})
    assert center['ok'] and message in center['data']['daily_publish']['reason']
    copied = TaskCenterAccess(api._engine).execution_log_text(identity)
    assert copied.count('daily.login_check_step') == 4 and message in copied
    assert '累计 0.1 秒' in copied
    assert all(value not in json.dumps(result['execution']) + copied for value in
               ('platform_user_id', 'session_fingerprint', 'original-synthetic-session'))


def test_one_timeout_with_diagnostic_still_checks_remaining_accounts(flow):
    api, provider, params, first = flow
    second = AccountService(api._engine).create('douyin', '222', '第二账号')
    with session_scope(api._engine) as session:
        session.get(AccountRow, second.id).session_ref = 'synthetic-second-session'
    params['publishing']['targets'].append({'account_id': second.id, 'platform': 'douyin'})
    provider.results[first.id] = {'state': 'unavailable', 'reason': 'timeout', 'diagnostic': diagnostic()}
    identity = submit(flow)
    result = phase(api, identity, 'making')
    assert provider.calls == [first.id, second.id]
    assert any('打开抖音后台超时' in row['message'] for row in result['execution']['events'])
    assert result['execution']['events'][-1]['message'] == '检查通过'
    assert api._mix.executor.renderer.index == 0


@pytest.mark.parametrize('stop', ['cancel', 'version'])
def test_late_diagnostic_cannot_write_after_cancellation_or_session_version_change(flow, stop):
    api, provider, _, account = flow
    provider.release.clear(); identity = submit(flow); assert provider.entered.wait(2)
    if stop == 'cancel':
        assert call(api, 'task.cancel', {'id': identity}, 'cancel-diagnostic')['ok']
    else:
        with session_scope(api._engine) as session:
            session.get(AccountRow, account.id).session_ref = 'new-session-version'
    with session_scope(api._engine) as session:
        before = session.get(Setting, 'mix.daily.status:' + identity).value_json
    api._daily.auth_progress(identity, index=1, total=1, account_id=account.id, stage='finished',
        state='unavailable', reason='timeout', diagnostic=diagnostic(), guard=lambda: True)
    with session_scope(api._engine) as session:
        assert session.get(Setting, 'mix.daily.status:' + identity).value_json == before
    if stop == 'cancel':
        provider.release.set(); until(lambda: identity not in api._daily.auth._jobs)
        with session_scope(api._engine) as session:
            assert session.get(Setting, 'mix.daily.status:' + identity).value_json == before


def test_diagnostics_log_tail_is_bounded_and_legacy_checks_are_not_backfilled(flow):
    api, provider, _, account = flow
    provider.release.clear(); identity = submit(flow); assert provider.entered.wait(2)
    evidence = diagnostic()
    evidence['events'] = [{'stage': 'surface', 'state': 'completed', 'elapsed_ms': n} for n in range(32)]
    for _ in range(4):
        api._daily.auth_progress(identity, index=1, total=1, account_id=account.id, stage='finished',
            state='unavailable', reason='timeout', diagnostic=evidence, guard=lambda: True)
    with session_scope(api._engine) as session:
        saved = json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)
    assert len(saved['login_log']) == 100
    previous = copy.deepcopy(saved['login_log'])
    api._daily.auth_progress(identity, index=1, total=1, account_id=account.id, stage='finished',
        state='unavailable', reason='timeout', guard=lambda: True)
    with session_scope(api._engine) as session:
        saved = json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)
    assert saved['login_log'][:-1] == previous[-99:]
    assert saved['login_log'][-1]['message'] == '检查超时，暂时无法确认登录'
    assert saved['login_log'][-1]['step'] == 'daily.login_check_finished'


@pytest.mark.parametrize('evidence', [None, {**diagnostic(), 'url': 'https://private.invalid/SECRET'}])
def test_invalid_diagnostic_never_grants_or_exports_provider_data(flow, evidence):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': 'valid', 'platform_user_id': '111', 'diagnostic': evidence}
    identity = submit(flow)
    result = phase(api, identity, 'needs_attention')
    assert api._mix.queue.get(identity).state == 'paused' and api._mix.executor.renderer.index == 0
    assert not any(event['step'] in {'daily.login_verified', 'daily.login_check_step'} for event in result['execution']['events'])
    assert 'SECRET' not in json.dumps(result) + TaskCenterAccess(api._engine).execution_log_text(identity)


def test_successful_finite_diagnostic_can_pass_the_existing_gate_without_fabricating_legacy_steps(flow):
    api, provider, _, account = flow
    evidence = diagnostic('identity_second')
    evidence['events'][-1] = {'stage': 'identity_second', 'state': 'completed', 'elapsed_ms': 24900}
    provider.results[account.id] = {'state': 'valid', 'platform_user_id': '111', 'diagnostic': evidence}
    identity = submit(flow)
    result = phase(api, identity, 'making')
    assert api._mix.queue.get(identity).state == 'waiting'
    steps = [event for event in result['execution']['events'] if event['step'] == 'daily.login_check_step']
    assert steps == []
    assert sum(event['step']=='daily.login_check_finished' for event in result['execution']['events'])==1
    assert sum(event['step'] == 'daily.login_verified' for event in result['execution']['events']) == 0
    assert api._mix.executor.renderer.index == 0 and result['publish_task_ids'] == []


def test_first_failure_summary_survives_four_later_full_step_logs_and_is_cleared_on_continue(flow):
    api, provider, params, first = flow
    failed = diagnostic()
    failed['events'] = [{'stage': 'surface', 'state': 'completed', 'elapsed_ms': n} for n in range(31)] + [
        {'stage': 'navigation_first', 'state': 'failed', 'elapsed_ms': 24900, 'error_kind': 'timeout'}]
    provider.results[first.id] = {'state': 'unavailable', 'reason': 'timeout', 'diagnostic': failed}
    for n in range(4):
        account = AccountService(api._engine).create('douyin', str(200 + n), '后续账号' + str(n))
        with session_scope(api._engine) as session:
            session.get(AccountRow, account.id).session_ref = 'synthetic-session-' + str(n)
        params['publishing']['targets'].append({'account_id': account.id, 'platform': 'douyin'})
        success = {**diagnostic('identity_second'), 'elapsed_ms': 1000, 'events': [
            {'stage': 'identity_second', 'state': 'completed', 'elapsed_ms': i} for i in range(32)]}
        provider.results[account.id] = {'state': 'valid', 'platform_user_id': account.platform_user_id, 'diagnostic': success}
    identity = submit(flow)
    phase(api, identity, 'making')
    with session_scope(api._engine) as session:
        saved = json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)
    assert len(saved['login_log']) == 100
    assert not any(row.get('index') == 1 for row in saved['login_log'])
    assert len(saved['login_failures']) == 1
    summary = saved['login_failures'][0]
    assert summary['index'] == 1 and summary['accounts'] == 5
    assert summary['check_stage'] == 'navigation_first' and summary['elapsed_ms'] == 25000
    assert summary['reason'] == 'timeout' and '打开抖音后台超时' in summary['message']
    copied = TaskCenterAccess(api._engine).execution_log_text(identity)
    assert 'daily.login_check_summary' in copied
    assert '第1/5个账号' in copied and '打开抖音后台超时' in copied
    assert '累计 25 秒' in copied
    provider.release.clear()
    provider.results[first.id] = {'state': 'valid', 'platform_user_id': first.platform_user_id}
    assert call(api, 'mix.daily.continue', {'id': identity}, 'recheck-summary')['ok']
    with session_scope(api._engine) as session:
        assert json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)['login_failures'] == []
    assert 'daily.login_check_summary' not in TaskCenterAccess(api._engine).execution_log_text(identity)
    provider.release.set(); phase(api, identity, 'making')
    with session_scope(api._engine) as session:
        assert json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)['login_failures'] == []


def test_summary_does_not_duplicate_a_finished_login_event_that_is_still_in_the_log(flow):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': 'unavailable', 'reason': 'timeout', 'diagnostic': diagnostic()}
    identity = submit(flow); phase(api, identity, 'needs_attention')
    with session_scope(api._engine) as session:
        saved = json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)
    assert len(saved['login_failures']) == 1
    copied = TaskCenterAccess(api._engine).execution_log_text(identity)
    assert copied.count('daily.login_check_finished') == 1
    assert 'daily.login_check_summary' not in copied


def test_long_account_nickname_cannot_hide_the_specific_timeout_reason(flow):
    api, provider, _, account = flow
    with session_scope(api._engine) as session:
        session.get(AccountRow, account.id).nickname = '长昵称' * 150
    provider.results[account.id] = {'state': 'unavailable', 'reason': 'timeout', 'diagnostic': diagnostic()}
    identity = submit(flow); result = phase(api, identity, 'needs_attention')
    assert len(result['reason']) <= 255 and '打开抖音后台超时' in result['reason']


def test_failure_summaries_are_bounded_and_never_persist_raw_diagnostics_or_unknown_reasons(flow):
    api, provider, _, account = flow
    provider.release.clear(); identity = submit(flow); assert provider.entered.wait(2)
    api._daily.auth_update(identity, phase='needs_attention', login_accounts=[{
        'account_id': account.id, 'platform': 'douyin', 'state': 'unavailable',
        'reason': 'PRIVATE_RAW_ERROR', 'diagnostic': {**diagnostic(), 'url': 'https://private.invalid/SECRET'}}] * 205)
    with session_scope(api._engine) as session:
        saved = json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)
    assert len(saved['login_failures']) == 200
    assert all(entry['reason'] == '' and 'check_stage' not in entry for entry in saved['login_failures'])
    assert all(value not in json.dumps(saved['login_failures']) for value in
               ('PRIVATE_RAW_ERROR', 'SECRET', 'platform_user_id', 'session_fingerprint'))
