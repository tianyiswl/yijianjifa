import json
from pathlib import Path
import pytest
from sqlalchemy import select
from test_commerce_preparation import configured
from test_finished_covers import video
from test_normal_declarations import call
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.commerce.service import CommerceTaskRow
from yijianjifa.storage.db import session_scope


def configured_api(engine,paths,tmp):
    prep,music,a,_,p,now=configured(engine,paths,tmp)
    v=FinishedVideoService(engine).import_external(video(tmp),duration_ms=1000)
    p['plan']['video_ids']=[v.id]
    api=DesktopApi(engine,paths)
    # Real API entry points with explicitly synthetic provider responses.
    api._commerce_catalog=prep._planning.catalog
    api._commerce_music=music
    api._commerce_declarations=prep._planning.declarations
    api._commerce_preparation._planning.catalog=api._commerce_catalog
    api._commerce_preparation._planning.music=music
    api._commerce_preparation._planning.declarations=api._commerce_declarations
    return api,a,v,p


def test_commerce_cover_freezes_private_copy_and_replays_after_preview_close(db_engine,app_paths,tmp_path):
    api,a,v,p=configured_api(db_engine,app_paths,tmp_path)
    try:
        token=api._covers.frame(v.id,'frame',500)
        staged=call(api,'publish.cover.batch',dict(id=v.id,token=token,video_ids=[v.id]))['covers'][v.id]
        p['cover_overrides']={a.id+':'+v.id:staged['token']}
        preview=call(api,'commerce.preview',p);assert preview['items'][0]['cover']['frame_ms']==500
        ready=call(api,'commerce.prepare',p);assert ready['token'] and 'path' not in ready['items'][0]['cover']
        result=call(api,'commerce.confirm',dict(token=ready['token']))
        with session_scope(db_engine) as s:
            data=json.loads(s.get(CommerceTaskRow,result['id']).snapshot_json)
            image=Path(data['items'][0]['cover']['path'])
            assert image.read_bytes().startswith(b'\x89PNG') and not data['execution_enabled']
        assert api._covers.read(v.id)[0]['saved'] is False
        api._covers.close();assert image.is_file()
        assert call(api,'commerce.confirm',dict(token=ready['token']))==result
    finally:api.close(.1)


def test_commerce_cover_rejects_foreign_pair_nested_choice_and_changed_image(db_engine,app_paths,tmp_path):
    api,a,v,p=configured_api(db_engine,app_paths,tmp_path)
    try:
        token=api._covers.frame(v.id,'first_frame',0)
        p['cover_overrides']={'other:'+v.id:token}
        with pytest.raises(ValueError):api._commerce_preparation.prepare(p)
        p['cover_overrides']={a.id+':'+v.id:token}
        p['plan']['cover_overrides']=p['cover_overrides']
        with pytest.raises(ValueError):api._commerce_preparation.prepare(p)
        del p['plan']['cover_overrides']
        ready=call(api,'commerce.prepare',p);assert ready['token']
        api._covers.preview(token)[1].write_bytes(b'changed')
        raw=json.dumps(dict(v=1,request_id='cover-changed',operation='commerce.confirm',params=dict(token=ready['token'])))
        assert not api.dispatch(raw,trusted_origin=True)['ok']
        with session_scope(db_engine) as s:assert s.scalar(select(CommerceTaskRow)) is None
    finally:api.close(.1)
