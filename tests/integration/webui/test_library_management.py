import json

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.library.copy_service import CategoryRepository, CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.preferences import LibraryPreferences


def call(api, op, params, rid='manage1'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=params)),trusted_origin=True)


def test_rename_changes_display_only_and_rejects_wrong_media_kind(db_engine,app_paths,tmp_path):
    service=MediaStorageService(db_engine,app_paths,kind='video')
    source=tmp_path/'原始 视频.mp4';source.write_bytes(b'video fixture')
    asset=service.import_media(source,keep_copy=False,category_ids=[])
    api=DesktopApi(db_engine,app_paths)
    wrong=call(api,'media.rename',dict(kind='music',id=asset.id,name='wrong'))
    assert not wrong['ok']
    assert service.get(asset.id).display_name==source.name
    result=call(api,'media.rename',dict(kind='video',id=asset.id,name='显示名 <script>'),'rename')
    assert result['ok'],result
    assert service.get(asset.id).display_name=='显示名 <script>'
    assert source.read_bytes()==b'video fixture'
    assert str(source) not in json.dumps(result)
    assert call(api,'media.rename',dict(kind='video',id=asset.id,name='显示名 <script>'),'rename')==result
    api.close(.1)


def test_batch_category_moves_with_legacy_append_and_invalid_target_rolls_back(db_engine,app_paths):
    repo=CopyRepository(db_engine);categories=CategoryRepository(db_engine)
    first=categories.create('copy','first');second=categories.create('copy','second');wrong=categories.create('video','wrong')
    for id in ('C1','C2'):repo.create(id=id,title=id,voice='',body='',tags='',full='')
    repo.set_categories('C1',[first.id])
    api=DesktopApi(db_engine,app_paths)
    params=dict(kind='copy',ids=['C1','C2'],category_ids=[second.id],mode='append')
    result=call(api,'library.categories',params)
    assert result['ok'],result
    assert repo.category_ids('C1')=={second.id}
    assert repo.category_ids('C2')=={second.id}
    rejected=call(api,'library.categories',dict(params,category_ids=[first.id,second.id]),'multiple')
    assert not rejected['ok'] and rejected['error']['code']=='single_category_only'
    assert not call(api,'library.categories',dict(params,category_ids=[wrong.id]),'badcat')['ok']
    assert not call(api,'library.categories',dict(params,ids=['C1','missing'],mode='replace',category_ids=[]),'badid')['ok']
    assert repo.category_ids('C1')=={second.id}
    assert call(api,'library.categories',dict(params,mode='replace',category_ids=[]),'clear')['ok']
    assert repo.category_ids('C1')==repo.category_ids('C2')==set()
    api.close(.1)


def test_import_preference_persists_without_import_and_is_kind_specific(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths)
    result=call(api,'media.preference.save',dict(kind='video',keep_copy=True))
    assert result['ok'],result
    api.close(.1)
    assert LibraryPreferences(db_engine).keep_copy('video') is True
    assert LibraryPreferences(db_engine).keep_copy('music') is False
    assert not call(api,'media.preference.save',dict(kind='video',keep_copy='true'),'bad')['ok']
