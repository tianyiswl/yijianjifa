import json
from pathlib import Path
from sqlalchemy import select,func
from test_publish_preparation import setup,verify_optional_declarations
from test_normal_declarations import call
from test_finished_covers import video
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.accounts.service import AccountService,AccountRow
from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.storage.db import session_scope


def test_publish_cover_is_scoped_to_pair_and_copied_without_saving_finished_cover(db_engine,app_paths,tmp_path):
    _,a,_,p,now=setup(db_engine,app_paths,tmp_path)
    v=FinishedVideoService(db_engine).import_external(video(tmp_path),duration_ms=1000)
    accounts=AccountService(db_engine);b=accounts.create('douyin','cover-other','第二账号')
    with session_scope(db_engine) as s:s.get(AccountRow,b.id).session_ref='fixture-b'
    accounts.update_capabilities(b.id,CapabilitySnapshot(ordinary='available'))
    api=DesktopApi(db_engine,app_paths)
    try:
        p.update(video_ids=[v.id],account_ids=[a.id,b.id],distribution='whole_batch')
        verify_optional_declarations(api,p)
        token=api._covers.frame(v.id,'frame',500)
        p['cover_overrides']={a.id+':'+v.id:token}
        ready=call(api,'publish.prepare',p);assert ready['token']
        assert ready['items'][0]['cover']['frame_ms']==500 and ready['items'][1].get('cover') is None
        assert str(tmp_path) not in json.dumps(ready) and 'path' not in ready['items'][0]['cover']
        result=call(api,'publish.confirm',dict(token=ready['token']))
        with session_scope(db_engine) as s:
            data=json.loads(s.scalar(select(PublishTaskRow)).snapshot_json)
            cover=data['items'][0]['cover'];copied=Path(cover['path'])
            assert copied.read_bytes().startswith(b'\x89PNG') and copied!=api._covers.preview(token)[1]
            assert not data['execution_enabled']
        assert api._covers.read(v.id)[0]['kind']=='first_frame'
        detail=call(api,'task.center.detail',dict(id=result['id']))
        assert detail['items'][0]['cover']['frame_ms']==500 and 'path' not in detail['items'][0]['cover']
        api._covers.close();assert copied.is_file()
        assert call(api,'publish.confirm',dict(token=ready['token']))==result
    finally:api._covers.close();api.close(.1)


def test_cover_wrong_video_and_modified_preview_are_rejected(db_engine,app_paths,tmp_path):
    _,a,original,p,now=setup(db_engine,app_paths,tmp_path)
    v=FinishedVideoService(db_engine).import_external(video(tmp_path),duration_ms=1000)
    api=DesktopApi(db_engine,app_paths)
    def prepare():return api.dispatch(json.dumps(dict(v=1,request_id='cover-invalid',operation='publish.prepare',params=p)),trusted_origin=True)
    try:
        verify_optional_declarations(api,p)
        token=api._covers.frame(v.id,'first_frame',0)
        p['cover_overrides']={a.id+':'+original.id:token};assert not prepare()['ok']
        p['video_ids']=[v.id];p['cover_overrides']={a.id+':'+v.id:token}
        prepared=prepare();assert prepared['ok'] and prepared['data']['token']
        api._covers.preview(token)[1].write_bytes(b'changed')
        response=api.dispatch(json.dumps(dict(v=1,request_id='cover-confirm-invalid',operation='publish.confirm',params=dict(token=prepared['data']['token']))),trusted_origin=True)
        assert not response['ok']
        with session_scope(db_engine) as s:assert s.scalar(select(func.count()).select_from(PublishTaskRow))==0
    finally:api._covers.close();api.close(.1)


def test_reschedule_rejects_changed_frozen_item_cover(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
    _,a,_,p,now=setup(db_engine,app_paths,tmp_path)
    v=FinishedVideoService(db_engine).import_external(video(tmp_path),duration_ms=1000)
    api=DesktopApi(db_engine,app_paths)
    try:
        p['video_ids']=[v.id];verify_optional_declarations(api,p)
        token=api._covers.frame(v.id,'frame',500);p['cover_overrides']={a.id+':'+v.id:token}
        ready=call(api,'publish.prepare',p);result=call(api,'publish.confirm',dict(token=ready['token']))
        # Explicit isolated fixture state; no elapsed time or platform result is claimed.
        def missed():
            with session_scope(db_engine) as s:
                s.get(PublishTaskRow,result['id']).state='missed_waiting_user'
                s.get(TaskCenterRow,result['id']).state='missed_waiting_user'
                s.scalar(select(TaskItemRow)).state='missed_waiting_user'
        missed()
        params=dict(id=result['id'],item_key=a.id+':'+v.id,mode='platform_schedule',when=(now+timedelta(hours=4)).isoformat(),software_consent=False)
        assert call(api,'publish.reschedule',params)['revision']==1
        missed()
        with session_scope(db_engine) as s:
            before=s.get(PublishTaskRow,result['id']).snapshot_json
            path=Path(json.loads(before)['items'][0]['cover']['path'])
        path.write_bytes(b'broken frozen cover')
        params['when']=(now+timedelta(hours=5)).isoformat()
        r=api.dispatch(json.dumps(dict(v=1,request_id='changed-cover-revision',operation='publish.reschedule',params=params)),trusted_origin=True)
        assert not r['ok']
        with session_scope(db_engine) as s:assert s.get(PublishTaskRow,result['id']).snapshot_json==before
    finally:api._covers.close();api.close(.1)


def test_applied_cover_survives_preview_eviction_and_stage_replay(db_engine,app_paths,tmp_path):
    _,a,_,p,now=setup(db_engine,app_paths,tmp_path)
    v=FinishedVideoService(db_engine).import_external(video(tmp_path),duration_ms=1000)
    api=DesktopApi(db_engine,app_paths)
    try:
        p['video_ids']=[v.id];verify_optional_declarations(api,p)
        token=api._covers.frame(v.id,'frame',500);preview_path=api._covers.preview(token)[1]
        staged=call(api,'publish.cover.stage',dict(id=v.id,token=token))
        assert staged['token']!=token and 'path' not in staged
        for i in range(17):api._covers.frame(v.id,'frame',i*10)
        assert not preview_path.exists()
        assert call(api,'publish.cover.stage',dict(id=v.id,token=token))==staged
        metadata,staged_path=api._covers.resolve_for_publish(staged['token'],v.id)
        assert metadata['frame_ms']==500 and staged_path.is_file()
        p['cover_overrides']={a.id+':'+v.id:staged['token']}
        ready=call(api,'publish.prepare',p);assert ready['token']
        result=call(api,'publish.confirm',dict(token=ready['token']))
        with session_scope(db_engine) as s:
            frozen=json.loads(s.get(PublishTaskRow,result['id']).snapshot_json)
            copied=Path(frozen['items'][0]['cover']['path'])
        assert api._covers.read(v.id)[0]['kind']=='first_frame'
        api.close(.1)
        assert not staged_path.exists() and copied.is_file()
    finally:api._covers.close();api.close(.1)


def test_cover_staging_quota_rejects_new_choice_without_evicting_applied_cover(db_engine,app_paths,tmp_path,monkeypatch):
    import pytest
    _,a,_,p,now=setup(db_engine,app_paths,tmp_path)
    v=FinishedVideoService(db_engine).import_external(video(tmp_path),duration_ms=1000)
    api=DesktopApi(db_engine,app_paths)
    try:
        first=api._covers.frame(v.id,'first_frame',0)
        staged=call(api,'publish.cover.stage',dict(id=v.id,token=first))
        data,path=api._covers.resolve_for_publish(staged['token'],v.id)
        monkeypatch.setattr(api._covers,'MAX_STAGED_BYTES',path.stat().st_size)
        second=api._covers.frame(v.id,'frame',500)
        raw=json.dumps(dict(v=1,request_id='quota',operation='publish.cover.stage',params=dict(id=v.id,token=second)))
        assert not api.dispatch(raw,trusted_origin=True)['ok']
        assert api._covers.resolve_for_publish(staged['token'],v.id)==(data,path)
        assert call(api,'publish.cover.stage',dict(id=v.id,token=first))==staged
        assert not api.dispatch(raw)['ok']
    finally:api._covers.close();api.close(.1)
