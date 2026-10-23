from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.features.notifications.service import NotificationService
from yijianjifa.core.settings import GlobalSettings


class Executor:
    def __init__(self,fail=False):self.fail=fail
    def render_item(self,*args):
        if self.fail:raise RuntimeError('private /file cookie=secret')


def test_batch_notification_is_once_and_committed_with_terminal_state(db_engine):
    q=MixQueue(db_engine,Executor());t=q.enqueue({'quantity':3})
    q._run_single_item(t.id)
    assert NotificationService(db_engine).history_for(t.id)==[]
    q.run_once_until_idle();q.run_once_until_idle()
    notes=NotificationService(db_engine).history_for(t.id)
    assert len(notes)==1 and notes[0].status=='mix_completed'
    assert '成功 3' in notes[0].message and notes[0].deep_link=='task:'+t.id


def test_failed_batch_has_safe_summary_not_renderer_error(db_engine):
    q=MixQueue(db_engine,Executor(True));t=q.enqueue({'quantity':2});q.run_once_until_idle()
    notes=NotificationService(db_engine).history_for(t.id)
    assert len(notes)==1 and '失败 2' in notes[0].message
    assert 'secret' not in notes[0].message and '/file' not in notes[0].message


def test_sound_pump_never_replays_history_and_honors_current_setting(db_engine,tmp_path):
    from yijianjifa.app_services.notification_sound import NotificationSoundPump
    service=NotificationService(db_engine)
    service.emit_action_required('old','旧通知')
    settings=GlobalSettings(db_engine,default_output_directory=tmp_path);played=[]
    pump=NotificationSoundPump(db_engine,settings,lambda:played.append('sound'))
    pump.poll_once();assert not played
    service.emit_action_required('new','新通知');pump.poll_once();pump.poll_once();assert len(played)==1
    settings.notification_sound_enabled=False
    service.emit_action_required('silent','静音通知');pump.poll_once();assert len(played)==1
    settings.notification_sound_enabled=True;pump.poll_once();assert len(played)==1
    assert len(service.history())==3
    assert pump.close(.1)


def test_notification_api_reads_history_and_marks_read_idempotently(db_engine,app_paths):
    import json
    from yijianjifa.app_services.desktop_api import DesktopApi
    q=MixQueue(db_engine,Executor());t=q.enqueue({'quantity':1});q.run_once_until_idle()
    api=DesktopApi(db_engine,app_paths)
    def call(op,p,rid):return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=p)),trusted_origin=True)
    history=call('notification.list',{'offset':0,'limit':20},'list')['data']
    assert history['unread']==1 and history['items'][0]['task_id']==t.id
    n=history['items'][0]['id'];first=call('notification.read',{'id':n},'read')
    assert first['ok'] and first==call('notification.read',{'id':n},'read')
    assert call('notification.list',{'offset':0,'limit':20},'list2')['data']['unread']==0
    assert api.close(.1)
