import json

import pytest

from yijianjifa.app_services.library_queries import LibraryQueries
from yijianjifa.features.library.copy_service import CategoryRepository


def request(operation='copy.get', params=None, **changes):
    return json.dumps(dict(v=1, request_id='r-1', operation=operation,
                          params=params if params is not None else {'id': 'C001'}, **changes))


def test_copy_query_uses_existing_core_and_is_read_only(db_engine, copy_repo):
    copy_repo.create(id='C001', title='<script>not executable</script>', voice='voice', body='body', tags='tag', full='full')
    api = LibraryQueries(db_engine)
    result = api.dispatch(request(), trusted_origin=True)
    assert result['ok'] is True
    assert result['data']['title'] == '<script>not executable</script>'
    assert set(result['data']) == {'id', 'title', 'voice', 'body', 'tags', 'full'}
    assert copy_repo.get('C001').body == 'body'


def test_foreign_origin_rejected_before_reading(db_engine, monkeypatch):
    api = LibraryQueries(db_engine)
    monkeypatch.setattr(api._copies, 'get', lambda _: pytest.fail('must not call core'))
    assert api.dispatch(request(), trusted_origin=False)['error']['code'] == 'origin_denied'


@pytest.mark.parametrize('raw', [
    '[]', '{', 'x'*16385,
    '{"v":1,"v":2,"request_id":"r","operation":"copy.get","params":{"id":"C001"}}',
    json.dumps({'v': True, 'request_id': 'r', 'operation': 'copy.get', 'params': {'id':'C001'}}),
    json.dumps({'v': 1, 'request_id': 'r', 'operation': 'copy.get', 'params': {'id': 1}}),
    json.dumps({'v': 1, 'request_id': 'r', 'operation': 'copy.get', 'params': {'id': 'a'*65}}),
    json.dumps({'v': 1, 'request_id': 'r', 'operation': 'copy.get', 'params': {'path':'../../secrets'}}),
    json.dumps({'v': 1, 'request_id': 'r', 'operation': 'copy.get', 'params': {'id':'C001'}, 'trusted': True}),
])
def test_invalid_envelopes_rejected(db_engine, raw):
    assert LibraryQueries(db_engine).dispatch(raw, trusted_origin=True)['error']['code'] == 'invalid_request'


def test_unknown_operations_and_missing_records(db_engine):
    api = LibraryQueries(db_engine)
    assert api.dispatch(request('shell.exec'), trusted_origin=True)['error']['code'] == 'unknown_operation'
    assert api.dispatch(request(), trusted_origin=True)['error']['code'] == 'not_found'


def test_categories_bounded_and_repeated_queries_safe(db_engine):
    CategoryRepository(db_engine).create('video', '测试')
    api = LibraryQueries(db_engine)
    raw = request('category.list', {'kind':'video', 'offset':0, 'limit':10})
    assert api.dispatch(raw, trusted_origin=True) == api.dispatch(raw, trusted_origin=True)
    result = api.dispatch(raw, trusted_origin=True)
    assert result['data']['items'][0]['name'] == '测试'
    assert result['data']['total'] == 1
    bad = request('category.list', {'kind':'video', 'offset':0, 'limit':101})
    assert api.dispatch(bad, trusted_origin=True)['error']['code'] == 'invalid_request'


def test_internal_exception_does_not_leak_details(db_engine, monkeypatch):
    api = LibraryQueries(db_engine)
    def explode(_):
        raise RuntimeError('Cookie=secret; /private/session/token')
    monkeypatch.setattr(api._copies, 'get', explode)
    result = api.dispatch(request(), trusted_origin=True)
    assert result['error']['code'] == 'internal_error'
    assert 'secret' not in json.dumps(result)
    assert result['error']['diagnostic_id']
