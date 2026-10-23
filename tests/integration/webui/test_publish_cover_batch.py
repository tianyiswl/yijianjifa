import json
import subprocess
import pytest
from yijianjifa.app_services.library_queries import CommandRejected
from test_normal_declarations import call
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.os_integration.resources import ffmpeg_path


def videos(engine, folder):
    result=[]
    for color, duration in [('blue',1000),('red',400)]:
        path=folder/(color+'.mp4')
        subprocess.run([str(ffmpeg_path()),'-v','error','-f','lavfi','-i',f'color=c={color}:s=64x64:r=10:d=1','-pix_fmt','yuv420p',str(path)],check=True)
        result.append(FinishedVideoService(engine).import_external(path,duration_ms=duration))
    return result


def test_batch_frames_bind_each_video_and_replay(db_engine,app_paths,tmp_path):
    a,b=videos(db_engine,tmp_path);api=DesktopApi(db_engine,app_paths)
    try:
        token=api._covers.frame(a.id,'frame',200)
        params=dict(id=a.id,token=token,video_ids=[a.id,b.id])
        result=call(api,'publish.cover.batch',params)
        refs=result['covers'];assert set(refs)=={a.id,b.id}
        first=api._covers.resolve_for_publish(refs[a.id]['token'],a.id)[0]
        second=api._covers.resolve_for_publish(refs[b.id]['token'],b.id)[0]
        assert first['sha256']!=second['sha256']
        assert first['frame_ms']==second['frame_ms']==200
        assert 'path' not in json.dumps(result)
        assert call(api,'publish.cover.batch',params)==result
        assert len(api._covers.staged)==2
        assert api._covers.read(b.id)[0]['saved'] is False
    finally:api.close(.1)


def test_batch_validates_all_times_before_staging_and_rolls_back_failure(db_engine,app_paths,tmp_path,monkeypatch):
    a,b=videos(db_engine,tmp_path);api=DesktopApi(db_engine,app_paths);covers=api._covers
    try:
        old=covers.stage_for_publish(a.id,covers.frame(a.id,'first_frame',0))
        before=dict(covers.staged);aliases=dict(covers.stage_sources);size=covers.staged_bytes
        token=covers.frame(a.id,'frame',500)
        with pytest.raises(ValueError):covers.stage_for_videos(a.id,token,[a.id,b.id])
        assert covers.staged==before and covers.staged_bytes==size
        token=covers.frame(a.id,'frame',200)
        monkeypatch.setattr(covers,'MAX_STAGED_COVERS',2)
        with pytest.raises(CommandRejected):covers.stage_for_videos(a.id,token,[a.id,b.id])
        assert covers.staged==before and covers.stage_sources==aliases and covers.staged_bytes==size
        assert len(list(app_paths.cache_dir.glob('publish-cover-*')))==1
        assert covers.resolve_for_publish(old['token'],a.id)[1].is_file()
    finally:api.close(.1)


def test_batch_uploaded_image_rebinds_and_rejects_invalid_targets(db_engine,app_paths,tmp_path):
    a,b=videos(db_engine,tmp_path);api=DesktopApi(db_engine,app_paths);covers=api._covers
    try:
        frame=covers.frame(a.id,'first_frame',0)
        token=covers.image(a.id,[covers.preview(frame)[1]])
        result=covers.stage_for_videos(a.id,token,[a.id,b.id])['covers']
        metadata=[covers.resolve_for_publish(result[v.id]['token'],v.id)[0] for v in (a,b)]
        assert metadata[0]==metadata[1] and metadata[0]['kind']=='uploaded'
        for ids in ([],[a.id,a.id],a.id,[True],[b.id]*201):
            with pytest.raises(ValueError):covers.stage_for_videos(a.id,token,ids)
        raw=json.dumps(dict(v=1,request_id='batch-untrusted',operation='publish.cover.batch',params=dict(id=a.id,token=token,video_ids=[b.id])))
        assert not api.dispatch(raw)['ok']
    finally:api.close(.1)


def test_batch_decode_failure_keeps_existing_applied_cover(db_engine,app_paths,tmp_path,monkeypatch):
    a,b=videos(db_engine,tmp_path);api=DesktopApi(db_engine,app_paths);covers=api._covers
    try:
        old=covers.stage_for_publish(a.id,covers.frame(a.id,'first_frame',0))
        old_data,old_path=covers.resolve_for_publish(old['token'],a.id)
        before=dict(covers.staged);size=covers.staged_bytes;aliases=dict(covers.stage_sources)
        token=covers.frame(a.id,'frame',200)
        def failed(*args,**kwargs):raise subprocess.CalledProcessError(1,'ffmpeg')
        monkeypatch.setattr('yijianjifa.app_services.finished_covers.subprocess.run',failed)
        with pytest.raises(ValueError):covers.stage_for_videos(a.id,token,[a.id,b.id])
        assert covers.staged==before and covers.staged_bytes==size and covers.stage_sources==aliases
        assert covers.resolve_for_publish(old['token'],a.id)==(old_data,old_path)
        assert len(list(app_paths.cache_dir.glob('publish-cover-*')))==1
    finally:api.close(.1)


def test_cancelled_batch_rolls_back_new_copies_and_reports_progress(db_engine,app_paths,tmp_path):
    import threading
    from yijianjifa.app_services.finished_covers import CoverBatchCancelled
    a,b=videos(db_engine,tmp_path);api=DesktopApi(db_engine,app_paths);covers=api._covers
    try:
        old=covers.stage_for_publish(a.id,covers.frame(a.id,'first_frame',0))
        before=dict(covers.staged);size=covers.staged_bytes;cancel=threading.Event();progress=[]
        token=covers.frame(a.id,'frame',200)
        def updated(done,total):
            progress.append((done,total))
            if done==1:cancel.set()
        with pytest.raises(CoverBatchCancelled):covers.stage_for_videos(a.id,token,[a.id,b.id],progress=updated,cancelled=cancel.is_set)
        assert progress==[(0,2),(1,2)]
        assert covers.staged==before and covers.staged_bytes==size
        assert covers.resolve_for_publish(old['token'],a.id)[1].is_file()
    finally:api.close(.1)
