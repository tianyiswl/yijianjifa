import json
import yijianjifa.app_services as module
from yijianjifa.features.mixing.desktop_service import DesktopMixService
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService


def api_for(engine, paths):
    cls=getattr(module,'DesktopApi',None)
    assert cls is not None,'desktop task/library service is missing'
    return cls(engine,paths)


def req(op,params,rid='request1'):
    return json.dumps(dict(v=1,request_id=rid,operation=op,params=params))


def test_submit_retry_freezes_copy_and_never_exposes_source_path(db_engine,app_paths,tmp_path):
    api=api_for(db_engine,app_paths)
    file=tmp_path/'source.mp4';file.write_bytes(b'fixture')
    MediaStorageService(db_engine,app_paths,kind='video').import_media(file,keep_copy=False,category_ids=[])
    CopyRepository(db_engine).create(id='C1',title='before',voice='',body='',tags='',full='')
    raw=req('mix.submit',{'quantity':1})
    first=api.dispatch(raw,trusted_origin=True)
    assert first['ok'],first
    second=api.dispatch(raw,trusted_origin=True)
    assert first==second
    tasks=DesktopMixService(db_engine,app_paths).tasks()
    assert len(tasks)==1 and tasks[0].snapshot['copies'][0]['title']=='before'
    assert str(file) not in json.dumps(first)
    result=api.dispatch(req('task.poll',{'epoch':None,'after':None},'poll1'),trusted_origin=True)
    assert result['data']['tasks'][0]['state']=='waiting'
    assert api.close(.1)


def test_library_list_reads_core_text_and_has_no_file_paths(db_engine,app_paths):
    api=api_for(db_engine,app_paths)
    CopyRepository(db_engine).create(id='C1',title='<script>test</script>',voice='',body='findme',tags='',full='')
    result=api.dispatch(req('library.list',{'kind':'copy','query':'findme','category_ids':[],'offset':0,'limit':20}),trusted_origin=True)
    assert result['ok'] and result['data']['total']==1
    assert result['data']['items'][0]['title']=='<script>test</script>'
    assert result['data']['items'][0]['category_ids']==[]
    assert api.close(.1)


def test_task_control_uses_same_core_queue_and_durable_receipt(db_engine,app_paths,tmp_path):
    api=api_for(db_engine,app_paths)
    file=tmp_path/'v.mp4';file.write_bytes(b'x')
    MediaStorageService(db_engine,app_paths,kind='video').import_media(file,keep_copy=False,category_ids=[])
    CopyRepository(db_engine).create(id='C1',title='test',voice='',body='',tags='',full='')
    id=api.dispatch(req('mix.submit',{'quantity':1}),trusted_origin=True)['data']['id']
    paused=api.dispatch(req('task.pause',{'id':id},'pause1'),trusted_origin=True)
    assert paused['ok']
    assert api.dispatch(req('task.pause',{'id':id},'pause1'),trusted_origin=True)==paused
    assert DesktopMixService(db_engine,app_paths).queue.get(id).state=='paused'
    assert api.dispatch(req('task.cancel',{'id':id},'cancel1'),trusted_origin=True)['ok']
    assert DesktopMixService(db_engine,app_paths).queue.get(id).state=='cancelled'
    assert api.close(.1)


def test_music_categories_use_existing_core_kind(db_engine,app_paths):
    from yijianjifa.features.library.copy_service import CategoryRepository
    CategoryRepository(db_engine).create('music','existing music')
    api=api_for(db_engine,app_paths)
    result=api.dispatch(req('category.list',{'kind':'audio','offset':0,'limit':20}),trusted_origin=True)
    assert result['data']['items'][0]['name']=='existing music'
    assert api.dispatch(req('category.create',{'kind':'music','name':'new music'},'catmusic'),trusted_origin=True)['ok']
    assert len(CategoryRepository(db_engine).list('music'))==2
    assert api.close(.1)


def test_publish_schedule_survives_api_restart_and_isolated_by_view(db_engine,app_paths):
    settings=dict(mode='daily',start='2026-10-01T12:00',interval='4',per_day='2',days='7',clock_mode='custom',date='2026-10-01',times=['12:00','18:00'])
    api=api_for(db_engine,app_paths)
    result=api.dispatch(req('settings.publish.save',{'view':'publish','settings':settings},'schedule-save'),trusted_origin=True)
    assert result['ok'],result
    assert api.close(.1)
    api=api_for(db_engine,app_paths)
    assert api.dispatch(req('settings.publish.read',{'view':'publish'},'schedule-read'),trusted_origin=True)['data']['settings']==settings
    assert api.dispatch(req('settings.publish.read',{'view':'commerce'},'commerce-read'),trusted_origin=True)['data']['settings'] is None
    bad={**settings,'unexpected':'field'}
    assert not api.dispatch(req('settings.publish.save',{'view':'publish','settings':bad},'schedule-invalid'),trusted_origin=True)['ok']
    assert api.close(.1)
