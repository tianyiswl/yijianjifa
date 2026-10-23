import json
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.features.tasks.service import TaskCenterService

class Broken:
    def render_item(self,*args):raise ValueError('素材文件不存在 cookie=private-secret')

def call(api,op,p):return api.dispatch(json.dumps(dict(v=1,request_id='read',operation=op,params=p)),trusted_origin=True)

def test_unified_filters_page_after_filter_and_preserves_unknown_state(db_engine,app_paths):
    q=MixQueue(db_engine,Broken());mix=q.enqueue({'quantity':1,'copies':[{'title':'测试文案'}]})
    q.run_once_until_idle()
    TaskCenterService(db_engine).register_task('P1',kind='publish',name='发布测试',state='result_unknown',snapshot_json='{"cookie":"private"}')
    api=DesktopApi(db_engine,app_paths)
    r=call(api,'task.center.list',{'query':'','tab':'all','status':'','offset':0,'limit':1})
    assert r['ok'],r
    assert r['data']['total']==2
    p=call(api,'task.center.list',{'query':'发布','tab':'publish','status':'ambiguous','offset':0,'limit':20})['data']
    assert p['total']==1 and p['items'][0]['state']=='result_unknown'
    assert 'private' not in json.dumps(p)
    detail=call(api,'task.center.detail',{'id':mix.id})['data']
    assert detail['issues'][0]['reason'] and detail['issues'][0]['occurred_at']
    assert 'private-secret' not in json.dumps(detail)
    assert detail['items'][0]['state']=='failed'
    assert call(api,'task.center.detail',{'id':'P1'})['data']['controls']==[]
    api.close(.1)


def test_diagnostics_redact_quoted_secrets_and_entire_cookie_headers():
    from yijianjifa.features.tasks.diagnostics import DiagnosticBundle
    raw='failure {"password":"two words", "access_token":"abc"}\nCookie: sid=aaa; session=bbb\nretry later'
    text=DiagnosticBundle.from_raw({'reason':raw,'raw_error':raw,'evidence':{'password':'hidden','auth_required':True}}).to_json()
    for secret in ('two words','abc','aaa','bbb','hidden'):assert secret not in text
    assert 'retry later' in text


def test_unstarted_items_follow_pause_and_cancel_without_inventing_results(db_engine):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    q = MixQueue(db_engine, Broken())
    task = q.enqueue({'quantity': 2})
    access = TaskCenterAccess(db_engine)
    q.request_pause(task.id)
    assert [i['state'] for i in access.detail(task.id)['items']] == ['paused', 'paused']
    q.resume(task.id)
    assert [i['state'] for i in access.detail(task.id)['items']] == ['waiting', 'waiting']
    q.cancel_remaining(task.id)
    data = access.detail(task.id)
    assert [i['state'] for i in data['items']] == ['cancelled', 'cancelled']
    assert data['task']['completed'] == 0
    assert data['issues'] == data['outputs'] == data['controls'] == []


def test_batch_detail_exposes_frozen_audio_subtitle_options_only(db_engine):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    q = MixQueue(db_engine, Broken())
    options = {'voice': 'Eddy', 'speed': 1.2, 'spoken_enabled': True,
               'full_enabled': False, 'music_gain': .25, 'original_gain': .5,
               'voice_gain': .8, 'sentence_cut': True, 'spoken_style': {'size': 42}}
    task = q.enqueue({'quantity': 1, 'options': dict(options, cookie='private'),
                      'sources': [{'path': '/private/source.mp4'}]})
    data = TaskCenterAccess(db_engine).detail(task.id)
    assert data['settings']['options'] == options
    assert 'private' not in json.dumps(data)


def test_quick_task_detail_includes_unstarted_cancelled_items(db_engine,app_paths):
    q=MixQueue(db_engine,Broken());task=q.enqueue({'quantity':2});q.cancel_remaining(task.id)
    api=DesktopApi(db_engine,app_paths)
    try:
        detail=call(api,'task.detail',{'id':task.id})
        assert detail['ok'],detail
        assert [i['state'] for i in detail['data']['items']]==['cancelled','cancelled']
    finally:api.close(.1)


def test_mix_detail_preserves_saved_material_diagnostics_and_only_real_events(db_engine):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    from yijianjifa.features.mixing.queue import MixTask
    task=MixQueue(db_engine,Broken()).enqueue({'quantity':1})
    diagnostic={'stage':'读取视频信息','material':r'D:\素材\河北排水沟.mp4',
                'reason':'无法解析视频信息 password=secret-value','error_type':'MediaProbeError',
                'materials':[r'D:\素材\河北排水沟.mp4'],'private_fixture':'must-not-leak'}
    event={'time':'2026-10-15T01:30:35Z','step':'mix.failed',**diagnostic}
    with session_scope(db_engine) as session:
        row=session.get(MixTask,task.id);row.state='completed_with_errors';row.failure_count=1;row.completed_count=1
        session.add(Setting(key=f'mix-result:{task.id}:0',value_json=json.dumps({
            'state':'failed','error':'MediaProbeError: cannot inspect','time':event['time'],
            'diagnostic':diagnostic,'execution_log':[event]},ensure_ascii=False)))
    data=TaskCenterAccess(db_engine).detail(task.id)
    item=data['items'][0];issue=data['issues'][0]
    assert item['diagnostic']['material']==diagnostic['material']
    assert item['diagnostic']['materials']==diagnostic['materials']
    assert item['execution_log'][0]['step']=='mix.failed'
    assert issue['stage']=='读取视频信息' and issue['material']==diagnostic['material']
    assert issue['reason']==item['diagnostic']['reason']
    assert issue['recent_steps']==['mix.failed']
    assert 'secret-value' not in json.dumps(data) and 'must-not-leak' not in json.dumps(data)
    assert '素材检查、配音或渲染' not in json.dumps(data)
    copied=TaskCenterAccess(db_engine).execution_log_text(task.id)
    assert '第1条视频' in copied and '失败素材：'+diagnostic['material'] in copied
    assert '失败步骤：读取视频信息' in copied and 'mix.failed' in copied
    assert 'secret-value' not in copied and 'must-not-leak' not in copied
    assert '未补造历史日志' not in copied


def test_legacy_mix_detail_does_not_invent_steps_or_materials(db_engine):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    from yijianjifa.features.mixing.queue import MixTask
    task=MixQueue(db_engine,Broken()).enqueue({'quantity':1})
    with session_scope(db_engine) as session:
        row=session.get(MixTask,task.id);row.state='completed_with_errors';row.failure_count=1;row.completed_count=1
        session.add(Setting(key=f'mix-result:{task.id}:0',value_json=json.dumps({
            'state':'failed','error':'ValueError: 历史素材错误','time':'2026-10-14T01:00:00Z'})))
    data=TaskCenterAccess(db_engine).detail(task.id)
    assert data['items'][0]['error'].startswith('ValueError:')
    assert data['issues'][0]['recent_steps']==[]
    assert not data['items'][0].get('diagnostic')
    assert not data['items'][0].get('execution_log')
    assert not data['issues'][0].get('material')
    assert data['issues'][0]['stage']=='未记录'
    copied=TaskCenterAccess(db_engine).execution_log_text(task.id)
    assert '第1条视频' in copied and '失败原因：ValueError: 历史素材错误' in copied
    assert '未记录执行步骤；未补造历史日志' in copied
