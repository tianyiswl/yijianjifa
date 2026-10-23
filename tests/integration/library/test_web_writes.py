import json
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
import yijianjifa.app_services.library_queries as module
from yijianjifa.features.library.copy_service import CopyRepository, CategoryRepository


def service(engine):
    cls = getattr(module, 'LibraryCommands', None)
    assert cls is not None, 'transactional library commands not implemented'
    return cls(engine)


def request(op='copy.save', params=None, rid='write1'):
    if params is None:
        params = dict(id=None, title=' title ', voice='', body='body', tags='', full='', category_ids=[])
    return json.dumps(dict(v=1, request_id=rid, operation=op, params=params))


def test_save_retry_after_restart_returns_same_id(db_engine):
    raw=request(); result=service(db_engine).dispatch(raw,trusted_origin=True)
    assert result['ok']
    again=service(db_engine).dispatch(raw,trusted_origin=True)
    assert again==result
    rows=CopyRepository(db_engine).list_all()
    assert len(rows)==1 and rows[0].title=='title'


def test_conflicting_request_id_never_rewrites(db_engine):
    api=service(db_engine);raw=request()
    assert api.dispatch(raw,trusted_origin=True)['ok']
    altered=json.loads(raw);altered['params']['title']='changed'
    result=api.dispatch(json.dumps(altered),trusted_origin=True)
    assert result['error']['code']=='request_conflict'
    assert CopyRepository(db_engine).list_all()[0].title=='title'


def test_receipt_failure_rolls_back_core_write(db_engine):
    api=service(db_engine)
    with db_engine.begin() as c:
        c.exec_driver_sql("CREATE TRIGGER fail_receipt BEFORE INSERT ON web_command_receipts BEGIN SELECT RAISE(ABORT, 'injected'); END")
    result=api.dispatch(request(),trusted_origin=True)
    assert result['error']['code']=='internal_error'
    assert CopyRepository(db_engine).list_all()==[]


def test_invalid_category_cannot_leave_partial_copy(db_engine):
    api=service(db_engine);raw=json.loads(request());raw['params']['category_ids']=['missing']
    assert api.dispatch(json.dumps(raw),trusted_origin=True)['error']['code']=='invalid_category'
    assert CopyRepository(db_engine).list_all()==[]


def test_parallel_duplicate_creates_only_once(db_engine):
    api=service(db_engine)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:api.dispatch(request(),trusted_origin=True),range(4)))
    assert all(r==results[0] for r in results) and results[0]['ok']
    assert len(CopyRepository(db_engine).list_all())==1


def test_category_and_copy_changes_use_existing_core(db_engine):
    api=service(db_engine)
    category=api.dispatch(request('category.create',{'kind':'copy','name':' 分类 '},'cat1'),trusted_origin=True)['data']
    raw=json.loads(request());raw['params']['category_ids']=[category['id']]
    saved=api.dispatch(json.dumps(raw),trusted_origin=True)['data']
    assert CopyRepository(db_engine).category_ids(saved['id'])=={category['id']}
    raw['request_id']='update1';raw['params'].update(id=saved['id'],title='updated')
    assert api.dispatch(json.dumps(raw),trusted_origin=True)['data']['title']=='updated'
    assert api.dispatch(request('copy.delete',{'id':saved['id']},'delete1'),trusted_origin=True)['ok']
    assert CopyRepository(db_engine).get(saved['id']) is None
    assert CategoryRepository(db_engine).list('copy')[0].name=='分类'


def test_unauthorized_write_and_blank_new_copy_denied(db_engine):
    api=service(db_engine)
    assert api.dispatch(request())['error']['code']=='origin_denied'
    raw=json.loads(request());raw['params'].update(title='',body='')
    assert api.dispatch(json.dumps(raw),trusted_origin=True)['error']['code']=='empty_copy'
    assert CopyRepository(db_engine).list_all()==[]


def test_active_task_keeps_copy_and_reports_delete_blocked(db_engine):
    from yijianjifa.features.tasks.service import TaskCenterService
    api=service(db_engine)
    saved=api.dispatch(request(),trusted_origin=True)['data']
    TaskCenterService(db_engine).register_task('M1',kind='make',name='active',state='waiting',snapshot_json=json.dumps({'copy_id':saved['id']}))
    result=api.dispatch(request('copy.delete',{'id':saved['id']},'delete-active'),trusted_origin=True)
    assert result['error']['code']=='delete_blocked'
    assert CopyRepository(db_engine).get(saved['id']) is not None
    with db_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM web_command_receipts WHERE request_id='delete-active'")).scalar_one()==0
