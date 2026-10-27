import json
import threading
from types import SimpleNamespace
import pytest
from yijianjifa.app_services.task_center_access import TaskCenterAccess
from yijianjifa.webui.host_commands import make_host

def saved_data():
    return {'task':{'id':'task','name':'发布测试','state':'action_required'},'deliveries':[
        {'id':'target','platform':'channels','account_label':'测试账号','scheduled_at':'2026-10-11T04:00:00+00:00',
         'state':'result_unknown','reason_message':'等待结果','execution_log':[
             {'time':'2026-10-10T04:52:33Z','step':'submission.final_check','checks':{'schedule_matches':True,'private':'token-value!', 'nested':{'cookie':'x'}}}]}], 'issues':[]}

def test_copy_all_current_saved_events_with_schedule_and_failure_context(monkeypatch):
    data=saved_data();access=TaskCenterAccess(None)
    monkeypatch.setattr(access,'detail',lambda pid:data)
    first=access.execution_log_text('task')
    assert '任务编号：task' in first and '视频号' in first and '等待结果' in first
    assert '2026-10-11T04:00:00+00:00' in first and 'submission.final_check' in first
    assert 'schedule_matches' in first and 'token-value' not in first and 'cookie' not in first
    data['deliveries'][0]['execution_log'].append({'time':'2026-10-10T04:52:35Z','step':'execution.result','checks':{'state':'result_unknown'}})
    assert 'execution.result' in access.execution_log_text('task')

def test_legacy_or_missing_events_are_not_invented(monkeypatch):
    access=TaskCenterAccess(None)
    monkeypatch.setattr(access,'detail',lambda pid:{'task':{'id':pid,'name':'旧任务','state':'failed'},'issues':[{'reason':'处理失败'}]})
    result=access.execution_log_text('old')
    assert '没有已记录' in result and '处理失败' in result


def test_mix_copy_numbers_items_with_saved_material_stage_reason_and_events(monkeypatch):
    access=TaskCenterAccess(None)
    diag={'material':r'D:\素材\河北排水沟.mp4','stage':'读取视频信息','reason':'工具未返回有效视频信息',
          'error_type':'MediaProbeError'}
    data={'task':{'id':'mix','name':'制作批次','kind':'make','state':'completed_with_errors'},
          'items':[{'index':0,'state':'failed','diagnostic':diag,'execution_log':[
              {'time':'2026-10-15T01:30:35Z','step':'mix.failed',**diag}]},
                   {'index':1,'state':'failed','error':'ValueError: 旧错误'}],
          'issues':[{'reason':diag['reason']},{'reason':'ValueError: 旧错误'}]}
    monkeypatch.setattr(access,'detail',lambda pid:data)
    monkeypatch.setattr('yijianjifa.app_services.mix_execution.read_execution',lambda *args:{
        'making':{'completed':2,'total':2,'succeeded':0,'failed':2},'current':{'label':'制作结束，部分失败'}})
    result=access.execution_log_text('mix')
    assert '第1条视频' in result and '第2条视频' in result
    assert '失败素材：'+diag['material'] in result
    assert '失败步骤：读取视频信息' in result
    assert '失败原因：'+diag['reason'] in result
    assert 'mix.failed' in result and '2026-10-15T01:30:35Z' in result
    assert result.count('ValueError: 旧错误')==1
    assert '未记录执行步骤' in result
    assert '未补造历史日志' in result

@pytest.mark.parametrize('operation',['task.execution.copy','task.publish_report.copy'])
def test_native_copy_uses_id_only_and_checks_trusted_origin(monkeypatch,operation):
    copies=[];reads=[]
    monkeypatch.setattr(TaskCenterAccess,'execution_log_text',lambda self,pid:reads.append(pid) or '真实已记录日志')
    monkeypatch.setattr('yijianjifa.app_services.mix_execution.read_execution',lambda engine,pid:reads.append(pid) or {'report':{'ready':True,'text':'真实已记录日志'}})
    error=lambda code,rid:{'ok':False,'error':{'code':code}}
    api=SimpleNamespace(_error=error,dispatch=lambda *a,**kw:error('not_trusted',None))
    host=make_host(api=api,engine=None,window=None,webview=None,exit_started=threading.Event(),finished_files=None,
        finished_deletion=None,deletion_recovery_pending=False,cover_response=None,drops=None,drop_access=None,
        media=None,excel=None,output=None,voice_catalog=None,volc_config=None,voices=None,play_sound=None,
        copy_text=copies.append,request_exit=None)
    raw=lambda params:json.dumps({'v':1,'request_id':'copy','operation':operation,'params':params})
    assert host.dispatch(raw({'id':'task'}),trusted_origin=True)['data']=={'copied':True}
    assert copies==['真实已记录日志'] and reads==['task']
    assert not host.dispatch(raw({'id':'task','text':'arbitrary'}),trusted_origin=True)['ok']
    assert not host.dispatch(raw({'id':'task'}),trusted_origin=False)['ok']
    assert copies==['真实已记录日志']

def test_native_clipboard_failure_is_reported(monkeypatch):
    monkeypatch.setattr(TaskCenterAccess,'execution_log_text',lambda *a:'日志')
    def broken(content):raise RuntimeError('clipboard failure')
    api=SimpleNamespace(_error=lambda code,rid:{'ok':False,'error':{'code':code}})
    host=make_host(api=api,engine=None,window=None,webview=None,exit_started=threading.Event(),finished_files=None,
        finished_deletion=None,deletion_recovery_pending=False,cover_response=None,drops=None,drop_access=None,
        media=None,excel=None,output=None,voice_catalog=None,volc_config=None,voices=None,play_sound=None,copy_text=broken,request_exit=None)
    result=host.dispatch(json.dumps({'v':1,'request_id':'copy','operation':'task.execution.copy','params':{'id':'task'}}),trusted_origin=True)
    assert not result['ok']


def test_copy_records_application_version_log_schema_and_only_trusted_build_commit(monkeypatch, tmp_path):
    import sys
    from yijianjifa import APP_VERSION
    exe = tmp_path / 'app.exe'
    exe.write_bytes(b'offline executable marker')
    (tmp_path / 'build-manifest.json').write_text(json.dumps({'commit': 'a' * 40}), encoding='utf-8')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(exe))
    access = TaskCenterAccess(None)
    monkeypatch.setattr(access, 'detail', lambda pid: saved_data())
    copied = access.execution_log_text('task')
    assert '应用版本：' + APP_VERSION in copied
    assert '日志格式：1' in copied
    assert '构建提交：' + 'a' * 40 in copied


@pytest.mark.parametrize('value', ['https://example.invalid/?token=SECRET', {'private': 'SECRET'}, 'a' * 41, 'z' * 40])
def test_copy_rejects_malformed_build_identity_without_echoing_private_data(monkeypatch, tmp_path, value):
    import sys
    exe = tmp_path / 'app.exe'
    exe.write_bytes(b'offline executable marker')
    (tmp_path / 'build-manifest.json').write_text(json.dumps({'commit': value}), encoding='utf-8')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(exe))
    access = TaskCenterAccess(None)
    monkeypatch.setattr(access, 'detail', lambda pid: saved_data())
    copied = access.execution_log_text('task')
    assert '构建提交：未记录' in copied
    assert 'SECRET' not in copied and 'example.invalid' not in copied
