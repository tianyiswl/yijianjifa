from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.copy_service import CopyRepository, CategoryRepository
from test_library_management import call


def listing(api,**extra):
    return call(api,'library.list',dict(kind='video',query='',category_ids=[],offset=0,limit=1,**extra))


def test_media_status_is_real_and_filtered_before_pagination(db_engine,app_paths,tmp_path):
    service=MediaStorageService(db_engine,app_paths,kind='video')
    ids=[]
    for i in range(3):
        file=tmp_path/f'{i}.mp4';file.write_bytes(str(i).encode())
        ids.append(service.import_media(file,keep_copy=False,category_ids=[]).id)
    (tmp_path/'0.mp4').unlink();(tmp_path/'2.mp4').unlink()
    api=DesktopApi(db_engine,app_paths)
    result=listing(api,status='missing')
    assert result['ok'],result
    assert result['data']['total']==2
    assert result['data']['items'][0]['missing'] is True
    ready=listing(api,status='ready')
    assert ready['data']['total']==1 and ready['data']['items'][0]['id']==ids[1]
    api.close(.1)


def test_busy_filter_uses_frozen_core_references(db_engine,app_paths,tmp_path):
    source=tmp_path/'1.mp4';source.write_bytes(b'1')
    MediaStorageService(db_engine,app_paths,kind='video').import_media(source,keep_copy=False,category_ids=[])
    CopyRepository(db_engine).create(id='C1',title='title',voice='',body='',tags='',full='')
    api=DesktopApi(db_engine,app_paths)
    assert listing(api,status='busy')['data']['total']==0
    task=call(api,'mix.submit',dict(quantity=1),'submit')['data']['id']
    assert listing(api,status='busy')['data']['total']==1
    assert call(api,'task.cancel',dict(id=task),'cancel')['ok']
    assert listing(api,status='busy')['data']['total']==0
    api.close(.1)


def test_uncategorized_is_union_with_other_filters_and_invalid_status_rejected(db_engine,app_paths,tmp_path):
    cats=CategoryRepository(db_engine);cat=cats.create('video','分类')
    service=MediaStorageService(db_engine,app_paths,kind='video')
    for i in range(2):
        path=tmp_path/f'{i}.mp4';path.write_bytes(str(i).encode())
        service.import_media(path,keep_copy=False,category_ids=[cat.id] if i else [])
    api=DesktopApi(db_engine,app_paths)
    params=dict(kind='video',query='',category_ids=['__none__'],offset=0,limit=100,status='all')
    result=call(api,'library.list',params)
    assert result['ok'] and result['data']['total']==1,result
    assert call(api,'library.list',dict(params,category_ids=['__none__',cat.id]))['data']['total']==2
    assert not listing(api,status='arbitrary')['ok']
    api.close(.1)
