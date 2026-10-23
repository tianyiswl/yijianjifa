import json
import subprocess
import pytest
from pathlib import Path
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.os_integration.resources import ffmpeg_path


def call(api,token,rid='cover-save'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation='finished.cover.save',params={'token':token})),trusted_origin=True)


def video(tmp_path):
    p=tmp_path/'测试 视频.mp4'
    subprocess.run([str(ffmpeg_path()),'-v','error','-f','lavfi','-i','color=c=blue:s=64x64:r=10:d=1','-pix_fmt','yuv420p',str(p)],check=True)
    return p


def test_frame_preview_save_replay_and_restart(db_engine,app_paths,tmp_path):
    source=video(tmp_path);core=FinishedVideoService(db_engine)
    item=core.import_external(source,duration_ms=1000)
    api=DesktopApi(db_engine,app_paths);covers=api._covers
    for ms in (-1,1000,True):
        with pytest.raises(ValueError):covers.frame(item.id,'frame',ms)
    token=covers.frame(item.id,'frame',500)
    data,path=covers.preview(token)
    assert data['frame_ms']==500 and path.read_bytes().startswith(b'\x89PNG')
    assert covers.read(item.id)[0]['kind']=='first_frame'
    result=call(api,token);assert result['ok'],result
    stored,cover_path=covers.read(item.id)
    assert stored['frame_ms']==500 and cover_path.is_file()
    assert str(cover_path) not in json.dumps(result)
    covers.close();api.close(.1)
    api=DesktopApi(db_engine,app_paths)
    assert call(api,token)==result
    assert api._covers.read(item.id)[0]['frame_ms']==500
    assert cover_path.is_file() and source.is_file()
    api._covers.close();api.close(.1)


def test_image_validation_cancel_and_failed_receipt_leave_saved_cover_unchanged(db_engine,app_paths,tmp_path):
    core=FinishedVideoService(db_engine);item=core.import_external(video(tmp_path),duration_ms=1000)
    api=DesktopApi(db_engine,app_paths);covers=api._covers
    assert covers.image(item.id,[]) is None
    bad=tmp_path/'损坏.png';bad.write_bytes(b'not image')
    with pytest.raises(ValueError):covers.image(item.id,[bad])
    first=covers.frame(item.id,'first_frame',0);_,image=covers.preview(first)
    uploaded=covers.image(item.id,[image]);assert covers.preview(uploaded)[0]['kind']=='uploaded'
    with db_engine.begin() as c:c.exec_driver_sql("CREATE TRIGGER fail_cover BEFORE INSERT ON web_command_receipts BEGIN SELECT RAISE(ABORT,'test'); END")
    assert not call(api,uploaded)['ok']
    assert covers.read(item.id)==({'id':item.id,'kind':'first_frame','frame_ms':0,'saved':False},None)
    with db_engine.begin() as c:c.exec_driver_sql('DROP TRIGGER fail_cover')
    assert call(api,uploaded)['ok']
    assert covers.read(item.id)[0]['kind']=='uploaded'
    covers.close();api.close(.1)
