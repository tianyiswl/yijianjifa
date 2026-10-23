import json
from sqlalchemy import select, text
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.usage_progress import UsageProgressService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import UsageProgress


def call(api, op, params, rid='read'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=params)),trusted_origin=True)


def setup_items(engine, repo, tmp):
    core=FinishedVideoService(engine)
    for pid in ['W1','W2']:repo.create(id=pid,title=pid,body=pid+'正文',tags='#'+pid,voice='不配音',full='不烧录')
    p=tmp/'视频.mp4';p.write_bytes(b'unchanged')
    a=core.import_external(p,duration_ms=1000,title='原标题')
    b=core.import_external(p,duration_ms=1000)
    g=core.register_generated(p,duration_ms=1000,title='制作时标题')
    return core,a,b,g


def test_preview_is_read_only_confirmation_preserves_and_replay_does_not_consume_again(db_engine,app_paths,copy_repo,tmp_path):
    core,a,b,g=setup_items(db_engine,copy_repo,tmp_path);api=DesktopApi(db_engine,app_paths)
    p=dict(ids=[a.id,b.id,g.id],category_ids=[],mode='sequential',replace_existing=False)
    r=call(api,'finished.assignment.preview',p)
    assert r['ok'],r
    d=r['data'];assert len(d['items'])==2 and d['skipped']==[g.id]
    assert [i['copy_id'] for i in d['items']]==['W1','W2']
    progress=UsageProgressService(db_engine)
    assert not progress.is_used('finished_metadata','W1')
    assert core.get(a.id).body==''
    saved=call(api,'finished.assignment.apply',{'token':d['token']},'assign-save')
    assert saved['ok'],saved
    assert core.get(a.id).title=='原标题' and core.get(a.id).body=='W1正文'
    assert core.get(b.id).title=='W2' and core.get(g.id).title=='制作时标题'
    assert progress.is_used('finished_metadata','W1') and not progress.is_used('mixing','W1')
    api.close(.1);api=DesktopApi(db_engine,app_paths)
    assert call(api,'finished.assignment.apply',{'token':d['token']},'assign-save')==saved
    assert not call(api,'finished.assignment.apply',{'token':d['token']},'another')['ok']
    assert (tmp_path/'视频.mp4').read_bytes()==b'unchanged'
    api.close(.1)


def test_changed_metadata_rejects_whole_preview_and_receipt_failure_rolls_back(db_engine,app_paths,copy_repo,tmp_path):
    core,a,b,g=setup_items(db_engine,copy_repo,tmp_path);api=DesktopApi(db_engine,app_paths)
    p=dict(ids=[a.id,b.id],category_ids=[],mode='random',replace_existing=True)
    d=call(api,'finished.assignment.preview',p)['data']
    core.update_metadata(b.id,title='随后修改',body='',tags='')
    r=call(api,'finished.assignment.apply',{'token':d['token']},'stale')
    assert not r['ok'] and r['error']['code']=='assignment_stale'
    assert core.get(a.id).title=='原标题'
    d=call(api,'finished.assignment.preview',p)['data']
    with db_engine.begin() as c:c.exec_driver_sql("CREATE TRIGGER fail_assignment_receipt BEFORE INSERT ON web_command_receipts BEGIN SELECT RAISE(ABORT,'test'); END")
    assert not call(api,'finished.assignment.apply',{'token':d['token']},'rollback')['ok']
    assert core.get(a.id).title=='原标题' and core.get(b.id).title=='随后修改'
    with session_scope(db_engine) as s:assert not list(s.scalars(select(UsageProgress)))
    with db_engine.begin() as c:c.exec_driver_sql('DROP TRIGGER fail_assignment_receipt')
    assert call(api,'finished.assignment.apply',{'token':d['token']},'rollback')['ok']
    assert core.get(a.id).title in ['W1','W2']
    api.close(.1)


def test_category_scope_wraps_only_on_save_and_progress_change_invalidates_preview(db_engine,app_paths,copy_repo,tmp_path):
    from yijianjifa.features.library.copy_service import CategoryRepository
    core,a,b,g=setup_items(db_engine,copy_repo,tmp_path);api=DesktopApi(db_engine,app_paths)
    cat=CategoryRepository(db_engine).create('copy','单条范围');copy_repo.set_categories('W2',[cat.id])
    progress=UsageProgressService(db_engine)
    progress.mark_used('finished_metadata','W2')
    params=dict(ids=[a.id,b.id],category_ids=[cat.id],mode='sequential',replace_existing=True)
    data=call(api,'finished.assignment.preview',params)['data']
    assert [i['copy_id'] for i in data['items']]==['W2','W2']
    assert [i['cycle'] for i in data['items']]==[1,2]
    with session_scope(db_engine) as session:assert progress._cycle(session,'finished_metadata')==0
    progress.reset_cycle('finished_metadata')
    assert call(api,'finished.assignment.apply',{'token':data['token']},'changed-progress')['error']['code']=='assignment_stale'
    data=call(api,'finished.assignment.preview',params)['data']
    assert call(api,'finished.assignment.apply',{'token':data['token']},'scoped')['ok']
    assert core.get(a.id).copy_id==core.get(b.id).copy_id=='W2'
    assert not progress.is_used('finished_metadata','W1')
    assert not call(api,'finished.assignment.preview',dict(params,category_ids=['missing']))['ok']
    api.close(.1)
