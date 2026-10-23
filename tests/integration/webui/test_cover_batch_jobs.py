import threading
import pytest


def test_batch_job_progress_cancel_replay_and_shutdown():
    from yijianjifa.app_services.cover_batch_jobs import CoverBatchJobs
    from yijianjifa.app_services.finished_covers import CoverBatchCancelled
    entered=threading.Event();release=threading.Event()
    class Covers:
        calls=0
        def stage_for_videos(self,pid,token,video_ids,*,progress,cancelled):
            self.calls+=1;progress(1,len(video_ids));entered.set();release.wait(3)
            if cancelled():raise CoverBatchCancelled()
            return {'covers':{}}
    covers=Covers();jobs=CoverBatchJobs(covers)
    p=dict(id='v',token='a'*48,video_ids=['v','w'])
    try:
        first=jobs.start(p);assert entered.wait(1)
        assert jobs.start(p)['id']==first['id'] and covers.calls==1
        state=jobs.status(first['id']);assert state['done']==1 and state['total']==2
        with pytest.raises(ValueError):jobs.start({**p,'token':'b'*48})
        assert jobs.cancel(first['id'])['state']=='cancelling'
        assert not jobs.close(.01)
        release.set();assert jobs.close(1)
        assert jobs.status(first['id'])['state']=='cancelled'
    finally:release.set();jobs.close(1)


def test_job_does_not_expose_raw_errors():
    from yijianjifa.app_services.cover_batch_jobs import CoverBatchJobs
    class Covers:
        def stage_for_videos(self,*args,**kwargs):raise RuntimeError('/private/path secret-session')
    jobs=CoverBatchJobs(Covers());job=jobs.start(dict(id='v',token='a'*48,video_ids=['v']))
    assert jobs.close(1)
    state=jobs.status(job['id']);assert state['state']=='failed'
    assert 'private' not in str(state) and 'secret' not in str(state)


def test_cover_jobs_api_is_trusted_only_and_waits_before_cleaning_covers(db_engine,app_paths,monkeypatch):
    import json
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.app_services.finished_covers import CoverBatchCancelled
    api=DesktopApi(db_engine,app_paths);entered=threading.Event();release=threading.Event()
    def work(*args,progress,cancelled,**kwargs):
        entered.set();release.wait(3)
        if cancelled():raise CoverBatchCancelled()
        return {'covers':{}}
    monkeypatch.setattr(api._covers,'stage_for_videos',work)
    def call(op,p,trusted=True):return api.dispatch(json.dumps(dict(v=1,request_id='cover-job',operation=op,params=p)),trusted_origin=trusted)
    try:
        p=dict(id='v',token='a'*48,video_ids=['v'])
        assert not call('publish.cover.batch.start',p,False)['ok']
        result=call('publish.cover.batch.start',p);assert result['ok'] and entered.wait(1)
        jid=result['data']['id'];assert call('publish.cover.batch.status',{'id':jid})['data']['state']=='running'
        assert not api.close(.01) and not api._covers.closed
        release.set();assert api.close(1) and api._covers.closed
        assert api._cover_jobs.status(jid)['state']=='cancelled'
    finally:release.set();api.close(1)
