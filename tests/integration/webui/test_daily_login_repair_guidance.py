"""Optional original-account login repairs never relabel unknown or repost work."""
import copy
import json

import pytest

from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_daily_login_flow import flow, login, phase, submit, until
from test_daily_mixing import call


@pytest.mark.parametrize(('state', 'reason'), [
    ('unavailable', 'network_unavailable'), ('unavailable', 'timeout'),
    ('unavailable', 'identity_unavailable'), ('unavailable', ''),
    ('identity_mismatch', ''),
])
def test_unconfirmed_login_offers_optional_original_account_repair_without_expiring_it(flow, state, reason):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': state, **({'reason': reason} if reason else {})}
    identity = submit(flow)
    result = phase(api, identity, 'needs_attention')
    assert result['can_continue'] and result['login_accounts'] == []
    assert result['login_repair_accounts'] == [{
        'id': account.id, 'platform': 'douyin', 'nickname': '原账号', 'state': state, 'reason': reason}]
    assert AccountService(api._engine).get(account.id).login_state == 'valid'
    assert api._mix.executor.renderer.index == 0 and not result['publish_task_ids']
    assert api._login_runtime.flow is None  # Offering repair does not open a login window.
    if state == 'unavailable':
        assert '登录对应账号' in result['login_message']
    encoded = json.dumps(result['login_repair_accounts'])
    assert all(value not in encoded for value in ('platform_user_id', 'session_ref', 'session_fingerprint'))


@pytest.mark.parametrize(('state', 'reason'), [('login_required', 'login_required'),
                                              ('unavailable', 'verification_required')])
def test_explicit_login_and_verification_keep_their_existing_required_login_route(flow, state, reason):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': state, 'reason': reason}
    identity = submit(flow)
    result = phase(api, identity, 'needs_login')
    assert [row['id'] for row in result['login_accounts']] == [account.id]
    assert result['login_repair_accounts'] == []


def test_optional_relogin_then_continue_keeps_original_batch_plan_and_clears_repair_action(flow):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': 'unavailable', 'reason': 'network_unavailable'}
    identity = submit(flow)
    phase(api, identity, 'needs_attention')
    frozen = copy.deepcopy(api._mix.queue.get(identity).snapshot)
    assert login(api, account)['state'] == 'completed'
    provider.release.clear()
    assert call(api, 'mix.daily.continue', {'id': identity}, 'repair-then-continue')['ok']
    checking = api._daily.status(identity)
    assert checking['phase'] == 'checking_login' and checking['login_repair_accounts'] == []
    provider.release.set()
    resumed = phase(api, identity, 'making')
    assert resumed['login_repair_accounts'] == []
    assert api._mix.queue.get(identity).snapshot == frozen and len(api._mix.tasks()) == 1
    assert api._mix.executor.renderer.index == 0


@pytest.mark.parametrize('operation', ['task.pause', 'task.cancel'])
def test_manual_stop_hides_previous_account_repair_action(flow, operation):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': 'unavailable', 'reason': 'timeout'}
    identity = submit(flow)
    assert phase(api, identity, 'needs_attention')['login_repair_accounts']
    assert call(api, operation, {'id': identity}, 'stop-repair')['ok']
    assert api._daily.status(identity)['login_repair_accounts'] == []


@pytest.mark.parametrize('mutation', [
    'foreign_account', 'changed_identity', 'removed_account', 'handoff', 'child_link',
    'hidden_task', 'unknown_state', 'malformed_state',
])
def test_saved_repair_projection_is_bound_to_frozen_original_account_and_auth_wait(flow, mutation):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': 'unavailable', 'reason': 'timeout'}
    identity = submit(flow)
    phase(api, identity, 'needs_attention')
    until(lambda: identity not in api._daily.auth._jobs)
    foreign = AccountService(api._engine).create('douyin', '222', '其他账号')
    with session_scope(api._engine) as session:
        row = session.get(Setting, 'mix.daily.status:' + identity)
        saved = json.loads(row.value_json)
        saved['login_repair_accounts'] = [{
            'id': foreign.id if mutation == 'foreign_account' else account.id,
            'platform': 'douyin', 'nickname': '未经确认的显示名', 'state': 'unavailable',
            'reason': 'timeout', 'platform_user_id': 'SECRET', 'session_ref': 'PRIVATE'}]
        if mutation == 'changed_identity': session.get(AccountRow, account.id).platform_user_id = 'changed-user'
        if mutation == 'removed_account': session.get(AccountRow, account.id).login_state = 'removed'
        if mutation == 'handoff': saved['handoff_authorized'] = True
        if mutation == 'child_link': saved['publish_task_ids'] = ['already-created-child']
        if mutation == 'hidden_task': session.add(Setting(key='task-hidden:' + identity, value_json='{}'))
        if mutation == 'unknown_state': saved['login_repair_accounts'][0]['state'] = 'PRIVATE_STATE'
        if mutation == 'malformed_state': saved['login_repair_accounts'][0]['state'] = {'private': 'SECRET'}
        row.value_json = json.dumps(saved)
    assert api._daily.status(identity)['login_repair_accounts'] == []


def test_stored_repair_fields_are_reprojected_without_raw_diagnostics_or_stale_names(flow):
    api, provider, _, account = flow
    provider.results[account.id] = {'state': 'unavailable', 'reason': 'timeout'}
    identity = submit(flow)
    phase(api, identity, 'needs_attention')
    until(lambda: identity not in api._daily.auth._jobs)
    with session_scope(api._engine) as session:
        row = session.get(Setting, 'mix.daily.status:' + identity)
        saved = json.loads(row.value_json)
        saved['login_repair_accounts'][0].update(
            reason='PRIVATE_RAW_ERROR', nickname='SECRET', session_ref='PRIVATE', platform_user_id='SECRET')
        row.value_json = json.dumps(saved)
    assert api._daily.status(identity)['login_repair_accounts'] == [{
        'id': account.id, 'platform': 'douyin', 'nickname': '原账号', 'state': 'unavailable', 'reason': ''}]
