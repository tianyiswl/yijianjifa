"""Real SQLite/API login progress; all online checks are local test doubles."""
import json

from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_daily_login_flow import flow, submit, phase, until


def test_blocked_serial_check_exposes_the_actual_account_and_time_then_reports_its_timeout(flow):
    api, provider, params, first = flow
    second = AccountService(api._engine).create('douyin', '222', '第二账号')
    with session_scope(api._engine) as session:
        session.get(AccountRow, second.id).session_ref = 'second-synthetic-session'
    params['publishing']['targets'].append({'account_id': second.id, 'platform': 'douyin'})
    provider.results[first.id] = {'state': 'unavailable', 'reason': 'timeout'}
    provider.release.clear(); identity = submit(flow); assert provider.entered.wait(2)
    checking = api._daily.status(identity)
    assert checking['phase'] == 'checking_login'
    assert checking['execution']['current']['label'] == '正在检查第1/2个账号'
    assert checking['execution']['current']['account_label'] == '抖音 · 原账号'
    assert checking['execution']['events'][-1]['step'] == 'daily.login_check_started'
    assert checking['execution']['events'][-1]['time']
    assert api._mix.queue.get(identity).state == 'paused' and api._mix.executor.renderer.index == 0
    provider.release.set(); result = phase(api, identity, 'making')
    assert any('原账号' in row.get('account_label','') and '超时' in row['message'] for row in result['execution']['events'])
    assert result['login_accounts'] == [] and not result['can_continue']
    assert AccountService(api._engine).get(first.id).login_state == 'valid'
    events = [event for event in result['execution']['events'] if event['step'].startswith('daily.login_check_')]
    assert len(events) == 2 and all(event['time'] for event in events)
    assert '第二账号' in events[-1]['account_label']
    assert all(word not in json.dumps(result['execution']) for word in ('platform_user_id', 'session_fingerprint', 'synthetic-session'))
    assert api._mix.executor.renderer.index == 0 and result['publish_task_ids'] == []


def test_progress_does_not_write_after_cancellation_or_when_the_identity_version_changed(flow):
    api, provider, _, account = flow
    provider.release.clear(); identity = submit(flow); assert provider.entered.wait(2)
    with session_scope(api._engine) as session:
        before = session.get(Setting, 'mix.daily.status:' + identity).value_json
        session.get(AccountRow, account.id).session_ref = 'new-identity-version'
    api._daily.auth_progress(identity, index=1, total=1, account_id=account.id, stage='finished',
                             state='valid', reason='', guard=lambda: True)
    with session_scope(api._engine) as session:
        assert session.get(Setting, 'mix.daily.status:' + identity).value_json == before
    from test_daily_mixing import call
    assert call(api, 'task.cancel', {'id': identity}, 'cancel-progress')['ok']
    with session_scope(api._engine) as session:
        stopped = session.get(Setting, 'mix.daily.status:' + identity).value_json
    api._daily.auth_progress(identity, index=1, total=1, account_id=account.id, stage='finished',
                             state='unavailable', reason='timeout', guard=lambda: True)
    provider.release.set(); until(lambda: identity not in api._daily.auth._jobs)
    with session_scope(api._engine) as session:
        assert session.get(Setting, 'mix.daily.status:' + identity).value_json == stopped
    assert api._mix.executor.renderer.index == 0


def test_network_progress_is_public_bounded_and_never_marks_an_unconfirmed_account_expired(flow):
    api, provider, _, account = flow
    with session_scope(api._engine) as session:
        session.get(AccountRow, account.id).nickname = '用户 uid=111 session_ref=secret-value /private/fixture ' + '字' * 255
    provider.results[account.id] = {'state': 'unavailable', 'reason': 'network_unavailable'}
    provider.release.clear(); identity = submit(flow); assert provider.entered.wait(2)
    for _ in range(110):
        api._daily.auth_progress(identity, index=1, total=1, account_id=account.id, stage='finished',
                                 state='unavailable', reason='PRIVATE_LONG_ERROR', guard=lambda: True)
    with session_scope(api._engine) as session:
        progress = json.loads(session.get(Setting, 'mix.daily.status:' + identity).value_json)
    assert len(progress['login_log']) == 100
    assert len(progress['login_current']['account_label']) <= 255
    assert all(value not in json.dumps(progress) for value in ('secret-value', '/private/fixture', 'PRIVATE_LONG_ERROR'))
    provider.release.set(); result = phase(api, identity, 'needs_attention')
    assert '网络无法确认登录' in result['execution']['events'][-1]['message']
    assert len(result['reason']) <= 255 and len(result['execution']['events']) <= 100
    assert AccountService(api._engine).get(account.id).login_state == 'valid'
    assert api._mix.executor.renderer.index == 0
