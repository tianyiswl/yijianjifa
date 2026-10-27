import json
from sqlalchemy import select
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.copy_service import CategoryRepository
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import CategoryLink


def call(api,op,params,rid='read'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=params)),trusted_origin=True)


def test_finished_categories_are_independent_filter_once_and_delete_only_links(db_engine,app_paths,tmp_path):
    core=FinishedVideoService(db_engine);source=tmp_path/'sample.MP4';source.write_bytes(b'video')
    row=core.import_external(source,duration_ms=1000)
    cats=CategoryRepository(db_engine);video=cats.create('video','素材');a=cats.create('finished','成品A');b=cats.create('finished','成品B')
    api=DesktopApi(db_engine,app_paths)
    params=dict(query='',origin='all',offset=0,limit=20)
    try:
        wrong=call(api,'finished.categories',dict(ids=[row.id],category_ids=[video.id],mode='replace'),'wrong');assert not wrong['ok']
        first=call(api,'finished.categories',dict(ids=[row.id],category_ids=[a.id],mode='replace'),'first');assert first['ok'],first
        assert call(api,'finished.categories',dict(ids=[row.id],category_ids=[b.id],mode='append'),'second')['ok']
        rejected=call(api,'finished.categories',dict(ids=[row.id],category_ids=[a.id,b.id],mode='replace'),'multiple')
        assert not rejected['ok'] and rejected['error']['code']=='single_category_only'
        result=call(api,'finished.list',dict(params,category_ids=[a.id,b.id]))['data'];assert result['total']==1
        assert set(result['items'][0]['category_ids'])=={b.id}
        assert call(api,'finished.list',dict(params,category_ids=['__none__']))['data']['total']==0
        assert call(api,'category.delete',dict(id=a.id),'delete-a')['ok']
        assert core.category_ids(row.id)=={b.id} and source.read_bytes()==b'video'
        assert call(api,'finished.categories',dict(ids=[row.id],category_ids=[],mode='replace'),'clear')['ok']
        assert call(api,'finished.list',dict(params,category_ids=['__none__']))['data']['total']==1
        assert call(api,'category.list',dict(kind='finished',offset=0,limit=100))['data']['total']==1
        assert core.get(row.id).content_hash==row.content_hash
    finally:api.close(.1)


def test_rename_separates_format_and_never_changes_source_or_hash(db_engine,app_paths,tmp_path):
    source=tmp_path/'原片.mp4';source.write_bytes(b'video')
    media=MediaStorageService(db_engine,app_paths,kind='video');asset=media.import_media(source,keep_copy=False,category_ids=[])
    core=FinishedVideoService(db_engine);finished=core.import_external(source,duration_ms=1000)
    api=DesktopApi(db_engine,app_paths)
    try:
        assert call(api,'media.rename',dict(kind='video',id=asset.id,name='新名字.mov'),'rename-video')['ok']
        result=call(api,'library.list',dict(kind='video',query='',category_ids=[],offset=0,limit=20))['data']['items'][0]
        assert (result['name'],result['format'])==('新名字','MP4')
        saved=call(api,'finished.save',dict(id=finished.id,display_name='成品名.mkv',title='标题',body='',tags=''),'rename-finished')
        assert saved['ok'],saved
        assert (saved['data']['name'],saved['data']['format'])==('成品名','MP4')
        assert media.get(asset.id).content_hash==asset.content_hash and core.get(finished.id).content_hash==finished.content_hash
        assert source.name=='原片.mp4' and source.read_bytes()==b'video'
        assert not call(api,'finished.save',dict(id=finished.id,display_name='.mp4',title='',body='',tags=''),'empty-name')['ok']
    finally:api.close(.1)


def test_finished_category_batch_rolls_back_if_any_item_is_missing(db_engine,app_paths,tmp_path):
    source=tmp_path/'x.mp4';source.write_bytes(b'x')
    core=FinishedVideoService(db_engine);row=core.import_external(source,duration_ms=1000)
    cat=CategoryRepository(db_engine).create('finished','分类')
    api=DesktopApi(db_engine,app_paths)
    try:
        r=call(api,'finished.categories',dict(ids=[row.id,'missing'],category_ids=[cat.id],mode='replace'),'batch')
        assert not r['ok'] and core.category_ids(row.id)==set()
        core.set_categories(row.id,[cat.id]);core.delete(row.id)
        with session_scope(db_engine) as session:assert not list(session.scalars(select(CategoryLink).where(CategoryLink.target_id==row.id)))
        assert source.exists()
    finally:api.close(.1)
