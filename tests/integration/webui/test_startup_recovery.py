import json

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.features.tasks.service import TaskCenterService


class Executor:
    def render_item(self, task_id, index, snapshot):
        if index == 1:
            raise ValueError('synthetic failure')


def read(api, params=None):
    request = dict(v=1, request_id='startup-read', operation='task.recovery',
                   params={} if params is None else params)
    return api.dispatch(json.dumps(request), trusted_origin=True)


def test_startup_recovery_reports_only_held_making_batches_without_resuming(db_engine, app_paths):
    queue = MixQueue(db_engine, Executor())
    partial = queue.enqueue({'quantity': 4, 'path': '/private/source.mp4'})
    queue._run_single_item(partial.id)
    queue._run_single_item(partial.id)
    waiting = queue.enqueue({'quantity': 2})
    cancelled = queue.enqueue({'quantity': 1})
    queue.cancel_remaining(cancelled.id)
    TaskCenterService(db_engine).register_task(
        'publish-held', kind='publish', name='发布任务', state='paused', snapshot_json='{}')

    api = DesktopApi(db_engine, app_paths)
    try:
        result = read(api)
        assert result['ok'], result
        assert result['data'] == {'tasks': [
            {'id': partial.id, 'state': 'interrupted', 'quantity': 4,
             'completed': 2, 'succeeded': 1, 'failed': 1},
            {'id': waiting.id, 'state': 'paused', 'quantity': 2,
             'completed': 0, 'succeeded': 0, 'failed': 0},
        ]}
        assert '/private' not in json.dumps(result)
        later = queue.enqueue({'quantity': 1})
        queue.request_pause(later.id)
        assert read(api)['data'] == result['data']
        assert queue.get(partial.id).completed_count == 2
        assert queue.get(waiting.id).completed_count == 0
        queue.resume(partial.id)
        assert [t['id'] for t in read(api)['data']['tasks']] == [waiting.id]
        assert not read(api, {'resume': True})['ok']
    finally:
        api.close(.1)


def test_clean_startup_has_no_recovery_prompt_candidates(db_engine, app_paths):
    api = DesktopApi(db_engine, app_paths)
    try:
        result = read(api)
        assert result['ok'], result
        assert result['data'] == {'tasks': []}
    finally:
        api.close(.1)
