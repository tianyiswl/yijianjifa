from legacy_schedule_fixture import legacy_window
import threading
from datetime import datetime, timezone


def test_watch_runs_without_page_polling_and_shutdown_joins_active_check():
    from yijianjifa.app_services.publish_watch import PublishWatch
    entered=threading.Event();release=threading.Event();calls=[]
    def recover(engine,*,now):
        calls.append(now);entered.set();assert release.wait(3)
    watch=PublishWatch(object(),recover=recover,interval=.01)
    assert watch.start();assert entered.wait(2)
    assert watch.start()
    assert watch.close(.01) is False
    release.set();assert watch.close(2)
    assert len(calls)==1 and calls[0].tzinfo is not None
    assert watch.start() is False


def test_watch_retries_recoverable_error_and_clears_failure():
    from yijianjifa.app_services.publish_watch import PublishWatch
    second=threading.Event();calls=[]
    def recover(engine,*,now):
        calls.append(now)
        if len(calls)==1:raise RuntimeError('injected')
        second.set()
    watch=PublishWatch(object(),recover=recover,interval=.01)
    watch.start()
    try:assert second.wait(2)
    finally:assert watch.close(2)
    assert not watch.failed and len(calls)>=2


def test_watch_updates_real_task_without_webview(db_engine,app_paths,tmp_path):
    from datetime import timedelta
    from test_publish_preparation import setup
    from yijianjifa.app_services.publish_confirmation import PublishConfirmation
    from yijianjifa.app_services.publish_control import recover_local_software
    from yijianjifa.app_services.publish_watch import PublishWatch
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    earlier=now-timedelta(hours=1);p['start']=(earlier+timedelta(minutes=10)).isoformat()
    p['time_choices']={a.id+':'+v.id:{'mode':'software_schedule','software_consent':True}}
    with legacy_window():  # Existing short-window records remain recoverable.
        task=PublishConfirmation(db_engine,app_paths,prep).confirm(prep.prepare(p,now=earlier)['token'],now=earlier)
    checked=threading.Event()
    def recover(engine,*,now):
        recover_local_software(engine,now=now);checked.set()
    watch=PublishWatch(db_engine,recover=recover,interval=.01);watch.start()
    try:assert checked.wait(2)
    finally:assert watch.close(2)
    assert TaskCenterAccess(db_engine).detail(task['id'])['task']['state']=='missed_waiting_user'


def test_check_status_exposes_failure_and_recovery_without_raw_errors(db_engine,app_paths):
    import json,time
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.app_services.publish_watch import PublishWatch
    recover_ok=threading.Event()
    def recover(engine,*,now):
        if not recover_ok.is_set():raise RuntimeError('/private/session-cookie-secret')
    api=DesktopApi(db_engine,app_paths)
    api._publish_watch=PublishWatch(db_engine,recover=recover,interval=.01)
    raw=json.dumps(dict(v=1,request_id='check-health',operation='local.checks.status',params={}))
    def status():
        response=api.dispatch(raw,trusted_origin=True)
        assert response['ok'],response
        assert 'secret' not in json.dumps(response) and '/private' not in json.dumps(response)
        return response['data']
    try:
        assert status()['state']=='stopped'
        assert api.start_local_checks()
        deadline=time.monotonic()+2
        while status()['state']!='failed' and time.monotonic()<deadline:time.sleep(.01)
        failed=status();assert failed['state']=='failed' and failed['last_failure_at']
        recover_ok.set()
        deadline=time.monotonic()+2
        while status()['state']!='healthy' and time.monotonic()<deadline:time.sleep(.01)
        recovered=status();assert recovered['state']=='healthy' and recovered['last_success_at']
        assert recovered['last_failure_at']==failed['last_failure_at']
        assert not api.dispatch(raw)['ok']
        invalid=json.loads(raw);invalid['params']={'execute':True}
        assert not api.dispatch(json.dumps(invalid),trusted_origin=True)['ok']
    finally:assert api.close(2)
    assert status()['state']=='stopped'
