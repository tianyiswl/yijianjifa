import json
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.media_storage import MediaStorageService


def call(api,op,p,rid='read'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=p)),trusted_origin=True)


def test_notes_persist_clear_and_do_not_change_publish_copy(db_engine,app_paths,tmp_path):
    source=tmp_path/'note.mp4';source.write_bytes(b'test video')
    video=MediaStorageService(db_engine,app_paths,kind='video').import_media(source,keep_copy=False,category_ids=[])
    core=FinishedVideoService(db_engine);finished=core.import_external(source,duration_ms=1200)
    api=DesktopApi(db_engine,app_paths)
    try:
        for kind,row in [('video',video),('finished',finished)]:
            p=dict(kind=kind,id=row.id,note='第一行\n第二行 <本地备注>')
            r=call(api,'video.note.save',p,kind);assert r['ok'],r
            assert call(api,'video.note.save',p,kind)==r
        bad=call(api,'video.note.save',dict(kind='video',id='missing',note='x'),'missing');assert not bad['ok']
        assert not call(api,'video.note.save',dict(kind='video',id=video.id,note='x'*2001),'long')['ok']
    finally:api.close(.1)
    api=DesktopApi(db_engine,app_paths)
    try:
        v=call(api,'library.list',dict(kind='video',query='',category_ids=[],offset=0,limit=20))['data']['items'][0]
        f=call(api,'finished.list',dict(query='',origin='all',offset=0,limit=20))['data']['items'][0]
        assert v['note']==f['note']=='第一行\n第二行 <本地备注>'
        assert f['title']==f['body']==f['tags']==''
        assert call(api,'video.note.save',dict(kind='finished',id=finished.id,note=''),'clear')['ok']
        assert call(api,'finished.list',dict(query='',origin='all',offset=0,limit=20))['data']['items'][0]['note']==''
        assert source.read_bytes()==b'test video'
    finally:api.close(.1)
