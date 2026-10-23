import json
import pytest
import yijianjifa.app_services.library_queries as services


def gate_for(engine):
    gate_type = getattr(services, 'NativeReadGate', None)
    assert gate_type is not None, 'native origin gate is not implemented'
    return gate_type(services.LibraryQueries(engine))


def request():
    return json.dumps(dict(v=1, request_id='native1', operation='copy.get', params={'id':'C1'}))


def test_bound_native_window_reads_existing_copy_and_close_revokes(db_engine, copy_repo):
    copy_repo.create(id='C1', title='native copy', voice='', body='', tags='', full='')
    gate = gate_for(db_engine)
    owner = object()
    assert gate.receive(request(), view=owner, main_frame=True, url='about:blank')['error']['code'] == 'origin_denied'
    gate.bind(owner)
    assert gate.receive(request(), view=owner, main_frame=True, url='about:blank')['data']['title'] == 'native copy'
    gate.close()
    assert gate.receive(request(), view=owner, main_frame=True, url='about:blank')['error']['code'] == 'origin_denied'
    with pytest.raises(RuntimeError):
        gate.bind(owner)


@pytest.mark.parametrize('foreign,main,url', [(True,True,'about:blank'), (False,False,'about:blank'), (False,True,'about:srcdoc'), (False,True,'file:///tmp/page.html'), (False,True,'https://example.test'), (False,1,'about:blank')])
def test_foreign_frame_or_navigation_cannot_read_copy(db_engine, copy_repo, foreign, main, url):
    copy_repo.create(id='C1', title='private copy', voice='', body='', tags='', full='')
    gate = gate_for(db_engine)
    owner = object(); gate.bind(owner)
    result = gate.receive(request(), view=object() if foreign else owner, main_frame=main, url=url)
    assert result['error']['code'] == 'origin_denied'
    assert 'private copy' not in json.dumps(result)
