"""A deferred verification is a UI choice, never a publishing instruction."""
import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from yijianjifa.app_services.library_queries import InvalidRequest
from yijianjifa.app_services.multi_publish_commands import MultiPublishCommands
from yijianjifa.app_services.multi_publish_runtime import MultiPublishRuntime
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
from yijianjifa.storage.db import session_scope


@pytest.fixture
def parked(db_engine, app_paths):
    class CachedAdapter:
        platform_id = 'douyin'
        verified_capabilities = {'ordinary', 'publish_now', 'result_query'}

        def __init__(self):
            self.requests = []
            self.responses = []

        def interventions(self):
            return copy.deepcopy(self.requests)

        def respond(self, request_id, action, code):
            self.responses.append((request_id, action, code))
            return {'accepted': True}

        def submit(self, *args, **kwargs):
            raise AssertionError('status must never submit')

        resume = reconcile = submit

    adapter = CachedAdapter()
    runtime = MultiPublishRuntime(db_engine, app_paths, adapters={'douyin': adapter})
    commands = MultiPublishCommands(db_engine, runtime, declarations=None, locations=None)
    jobs = []
    runtime.enqueue = lambda *job: jobs.append(job)

    def call(name, params=None):
        result = commands.dispatch({'operation': 'multi_publish.' + name,
                                    'params': {} if params is None else params,
                                    'request_id': 'fixture-' + name})
        assert result['ok'], result
        return result['data']

    def request(delivery_id, request_id=None):
        item = {'id': request_id or 'request-' + delivery_id,
                'delivery_id': delivery_id, 'platform': 'douyin',
                'account_name': '合成账号', 'final_started': True,
                'kind': 'sms', 'reason': 'sms_required'}
        adapter.requests.append(item)
        return item

    return SimpleNamespace(runtime=runtime, commands=commands, adapter=adapter,
                           call=call, request=request, jobs=jobs)


@pytest.mark.parametrize('count', [1, 3])
def test_repeated_polling_does_not_resurface_all_deferred_requests(parked, count):
    requests = [parked.request('delivery-' + str(i)) for i in range(count)]
    for item in requests:
        assert parked.call('intervention.defer', {'id': item['id'],
                                                  'delivery_id': item['delivery_id']})['deferred']
    for _ in range(5):
        assert parked.call('intervention.status') == {'request': None, 'pending': count}
    assert parked.runtime.deferred == {item['id'] for item in requests}
    assert parked.adapter.responses == [] and parked.jobs == []


def test_a_new_request_is_visible_when_an_old_request_is_deferred(parked):
    old = parked.request('old-delivery')
    parked.call('intervention.defer', {'id': old['id'], 'delivery_id': old['delivery_id']})
    fresh = parked.request('new-delivery')
    status = parked.call('intervention.status')
    assert status['request']['id'] == fresh['id'] and status['pending'] == 2
    assert parked.runtime.deferred == {old['id']}


def test_explicit_target_read_includes_deferred_without_reopening_other_targets(parked):
    first = parked.request('delivery-one')
    other = parked.request('delivery-two')
    parked.call('intervention.defer', {'id': first['id'], 'delivery_id': first['delivery_id']})
    for _ in range(3):
        status = parked.call('intervention.status', {'delivery_id': first['delivery_id']})
        assert status['request']['id'] == first['id'] and status['pending'] == 2
        assert parked.call('intervention.status')['request']['id'] == other['id']
    assert parked.call('intervention.status', {'delivery_id': 'missing'}) == {'request': None, 'pending': 2}
    assert parked.runtime.deferred == {first['id']}
    assert parked.adapter.responses == [] and parked.jobs == []


@pytest.mark.parametrize('params', [
    {'delivery_id': None}, {'delivery_id': ''}, {'delivery_id': 1},
    {'delivery_id': '../other'}, {'delivery_id': 'x' * 65},
    {'delivery_id': 'target', 'action': 'done'}, {'id': 'request-target'},
])
def test_explicit_target_status_keeps_strict_schema(parked, params):
    with pytest.raises(InvalidRequest):
        parked.call('intervention.status', params)
    assert parked.adapter.responses == [] and parked.jobs == []


def test_explicit_response_clears_only_its_deferred_request_and_queues_query(parked):
    first = parked.request('delivery-one')
    second = parked.request('delivery-two')
    parked.runtime.deferred.update([first['id'], second['id']])
    assert parked.call('intervention.respond', {'id': first['id'],
                       'delivery_id': first['delivery_id'], 'action': 'done'})['accepted']
    assert parked.runtime.deferred == {second['id']}
    assert parked.jobs == [('reconcile', first['delivery_id'])]
    assert parked.adapter.responses == [(first['id'], 'done', '')]


def test_defer_and_explicit_reads_do_not_change_final_journal_or_any_database_row(parked, db_engine, tmp_path):
    account = AccountService(db_engine).create('douyin', '111', '合成账号')
    path = tmp_path / 'owned.mp4'
    path.write_bytes(b'synthetic-video')
    video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title='合成测试')
    plan = parked.call('prepare', {'video_ids': [video.id],
        'targets': [{'account_id': account.id, 'platform': 'douyin'}],
        'common_fields': {}, 'execution_mode': 'publish_now'})
    batch = parked.call('confirm', {'prepared_id': plan['prepared_id'],
                                   'intent_id': 'fixture-prepare', 'consent_partial': False})
    delivery = parked.call('detail', {'id': batch['id']})['deliveries'][0]['id']
    with session_scope(db_engine) as session:
        attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery))
        attempt.final_json = json.dumps({'fixture': 'original-final-marker'})
        attempt.result_json = json.dumps({'state': 'result_unknown', 'error_code': 'verification_required'})
    item = parked.request(delivery)

    def snapshot():
        with db_engine.connect() as connection:
            return '\n'.join(connection.connection.driver_connection.iterdump())

    before = snapshot()
    parked.call('intervention.defer', {'id': item['id'], 'delivery_id': delivery})
    for _ in range(3):
        assert parked.call('intervention.status') == {'request': None, 'pending': 1}
        assert parked.call('intervention.status', {'delivery_id': delivery})['request']['id'] == item['id']
    assert snapshot() == before
    assert parked.adapter.responses == [] and parked.jobs == []
    assert parked.runtime.thread is None
