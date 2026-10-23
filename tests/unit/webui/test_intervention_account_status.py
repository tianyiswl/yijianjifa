"""An explicit account lookup reads its original verification, including deferred."""
import copy

import pytest

from yijianjifa.app_services.library_queries import InvalidRequest
from yijianjifa.app_services.multi_publish_commands import MultiPublishCommands
from yijianjifa.app_services.multi_publish_runtime import MultiPublishRuntime


@pytest.fixture
def account_status(db_engine, app_paths):
    class CachedAdapter:
        platform_id = 'douyin'
        verified_capabilities = {'ordinary', 'publish_now', 'result_query'}
        requests = [
            dict(id='other-request', delivery_id='other-delivery', account_id='other', account_name='同名账号'),
            dict(id='target-request', delivery_id='target-delivery', account_id='target', account_name='同名账号'),
        ]
        def interventions(self): return copy.deepcopy(self.requests)
        def submit(self, *_args, **_kwargs): pytest.fail('status must never submit')
        reconcile = resume = submit
    runtime = MultiPublishRuntime(db_engine, app_paths, adapters={'douyin': CachedAdapter()})
    commands = MultiPublishCommands(db_engine, runtime, declarations=None, locations=None)
    runtime.deferred.add('target-request')
    runtime.enqueue = lambda *_args: pytest.fail('status must never enqueue work')
    return runtime, commands


def call(commands, params):
    return commands.dispatch(dict(operation='multi_publish.intervention.status',
        params=params, request_id='fixture-account-status'))['data']


def test_account_lookup_finds_only_exact_account_even_when_deferred(account_status, db_engine):
    runtime, commands = account_status
    with db_engine.connect() as connection:
        before = '\n'.join(connection.connection.driver_connection.iterdump())
    assert call(commands, {})['request']['id'] == 'other-request'
    assert call(commands, {'account_id': 'target'})['request']['id'] == 'target-request'
    assert call(commands, {'account_id': 'absent'})['request'] is None
    assert call(commands, {})['request']['id'] == 'other-request'
    assert runtime.deferred == {'target-request'} and runtime.thread is None
    with db_engine.connect() as connection:
        assert '\n'.join(connection.connection.driver_connection.iterdump()) == before


@pytest.mark.parametrize('params', [
    {'account_id': None}, {'account_id': ''}, {'account_id': '../other'},
    {'account_id': 1}, {'account_id': 'x' * 65},
    {'account_id': 'target', 'delivery_id': 'target-delivery'},
    {'account_id': 'target', 'action': 'done'},
])
def test_account_status_rejects_invalid_or_combined_selectors(account_status, params):
    runtime, commands = account_status
    with pytest.raises(InvalidRequest):
        call(commands, params)
    assert runtime.deferred == {'target-request'}
