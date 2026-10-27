"""Real result storage and task APIs agree on a completed local delivery."""
import json

import pytest

from test_channels_publish_boundary import channels, freeze, journal


def read(api, operation, params):
    result = api.dispatch(json.dumps({'v': 1, 'request_id': 'read-completion',
        'operation': operation, 'params': params}), trusted_origin=True)
    assert result['ok'], result
    return result['data']


@pytest.mark.parametrize('mode,work_id,expected,label', [
    ('publish_now', None, 'platform_accepted', '平台已接收'),
    ('publish_now', '9007199254740993', 'platform_accepted', '平台已接收'),
    ('platform_schedule', None, 'platform_scheduled', '平台已接收定时'),
    ('platform_schedule', '9007199254740993', 'platform_scheduled', '平台已接收定时'),
])
def test_confirmed_channels_submission_finishes_task_without_claiming_publication(
        channels, db_engine, mode, work_id, expected, label):
    api, _, _, _, events, ack = channels
    if work_id is None:
        ack['data'].pop('objectId')
    batch, did = freeze(channels, db_engine, mode)
    assert api._multi_publish.coordinator.run_once(batch, [did]) == expected
    original_final = journal(db_engine, did)[1]

    listed = read(api, 'task.center.list', {'query': '', 'tab': 'publish', 'status': 'done',
                                          'offset': 0, 'limit': 100})
    task = next((item for item in listed['items'] if item['id'] == batch), None)
    assert task is not None, 'A confirmed upload must leave the needs-attention list'
    assert task['state'] == 'completed' and task['completed'] == 1
    assert task['status_label'] == label
    assert task['accepted_count'] == 1 and task['published_count'] == 0 and task['succeeded'] == 0
    attention = read(api, 'task.center.list', {'query': '', 'tab': 'action', 'status': '',
                                             'offset': 0, 'limit': 100})
    assert batch not in {item['id'] for item in attention['items']}

    detail = read(api, 'task.center.detail', {'id': batch})
    target = detail['deliveries'][0]
    assert detail['task']['state'] == 'completed' and detail['task']['status_label'] == label
    assert detail['summary']['blocked'] == 0 and detail['summary']['ready'] == 0
    assert target['state'] == expected and target['stage'] in {'平台已接收', '平台已接收定时安排'}
    assert target['submission_status'] == 'submitted' and not target['can_submit']
    assert target['reason_message'] == ''
    assert not {'start', 'resume', 'reprepare', 'reschedule'} & set(target['allowed_actions'])
    assert target['receipt_summary']['reference'] == ({'work_id': work_id} if work_id else {})
    assert 'submission_ack' not in json.dumps(detail), 'Internal acceptance proof must remain private'

    assert api._multi_publish.coordinator.run_once(batch, [did]) is None
    assert events.count('upload') == events.count('final') == 1
    assert journal(db_engine, did)[1] == original_final
