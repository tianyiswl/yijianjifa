"""明确构造升级前已保存的version1无flow快照，验证原bulk/split兼容。"""
import json
from unittest.mock import patch

from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.storage.db import session_scope


def legacy_call(api, params, request_id):
    original = api._daily.submit
    def historical(connection, body):
        result = original(connection, body)
        with session_scope(connection) as s:
            task = s.get(MixTask, result['id'])
            snapshot = json.loads(task.snapshot_json)
            snapshot['daily_publish'].pop('execution_flow')
            task.snapshot_json = json.dumps(snapshot)
        return result
    with patch.object(api._daily, 'submit', historical):
        response = api.dispatch(json.dumps({'v': 1, 'request_id': request_id,
            'operation': 'mix.daily.submit', 'params': params}), trusted_origin=True)
    assert response['ok'], response
    return response['data']
