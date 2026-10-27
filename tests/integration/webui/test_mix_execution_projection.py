"""The execution panel projects saved evidence; polling never runs work."""
import json

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.publish.deliveries import PublishDeliveryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def call(api, operation, params=None):
    result = api.dispatch(json.dumps(dict(v=1, request_id='execution-read', operation=operation, params=params or {})), trusted_origin=True)
    assert result['ok'], result
    return result['data']


class Adapter:
    platform_id = 'douyin'
    verified_capabilities = {'ordinary', 'publish_now', 'platform_schedule'}
    def submit(self, *args, **kwargs): raise AssertionError('Reading must not publish')
    def reconcile(self, *args, **kwargs): raise AssertionError('Reading must not query a platform')


@pytest.fixture
def execution(db_engine, app_paths):
    api = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': Adapter()})
    task = api._mix.queue.enqueue({'quantity': 3, 'copies': [{'title': f'作品{i + 1}'} for i in range(3)],
                                  'path': '/private/session/secret', 'session_ref': 'private-session'})
    with session_scope(db_engine) as session:
        row = session.get(MixTask, task.id)
        row.state = 'running'; row.current_index = 1; row.completed_count = row.success_count = 1
        session.add(Setting(key=f'mix-result:{task.id}:0', value_json=json.dumps({'state': 'completed', 'execution_log': [
            {'time': '2026-10-15T01:00:00+00:00', 'step': 'mix.started'},
            {'time': '2026-10-15T01:01:00+00:00', 'step': 'mix.completed', 'path': '/private/session/secret'}]})))
    yield api, task.id
    assert api.close(.5)


def test_only_make_poll_and_detail_share_saved_counts_titles_and_event_times(execution, db_engine):
    api, identity = execution
    with session_scope(db_engine) as session:
        before = [(row.key, row.value_json) for row in session.scalars(select(Setting).order_by(Setting.key))]
    poll = call(api, 'task.poll', {'epoch': None, 'after': None})
    value = next(task for task in poll['tasks'] if task['id'] == identity)['execution']
    assert value['making'] == dict(succeeded=1, failed=0, completed=1, total=3)
    assert value['current']['index'] == 2 and value['current']['title'] == '作品2'
    assert [event['time'] for event in value['events']] == ['2026-10-15T01:01:00+00:00']
    assert all(event['message'] for event in value['events'])
    assert all(not event.get('title') for event in value['events'])
    assert call(api, 'task.detail', {'id': identity})['execution'] == value
    assert call(api, 'task.poll', {'epoch': poll['epoch'], 'after': poll['revision']})['changed'] is False
    with session_scope(db_engine) as session:
        assert [(row.key, row.value_json) for row in session.scalars(select(Setting).order_by(Setting.key))] == before
        assert session.get(MixTask, identity).current_index == 1
    assert 'private' not in json.dumps(value)


def test_login_panel_shows_one_account_result_without_internal_start_finish_pairs(execution,db_engine):
    api,identity=execution
    entries=[]
    for stage in ('before_make','before_publish'):
        for step,message in [('daily.login_check_started','开始检查登录'),
            ('daily.login_check_step','关闭检查浏览器：开始（累计 4.72 秒）'),
            ('daily.login_check_step','关闭检查浏览器：完成（累计 5.186 秒）'),
            ('daily.login_check_finished','账号登录有效（累计 5.2 秒）')]:
            entries.append(dict(time='2026-10-15T02:00:00Z',step=step,stage=stage,index=1,accounts=1,
                account_label='抖音 · 测试账号',message=message))
    with session_scope(db_engine) as db:
        db.add(Setting(key='mix.daily.status:'+identity,value_json=json.dumps({'login_log':entries})))
    projected=call(api,'task.detail',{'id':identity})['execution']
    login=[e for e in projected['events'] if e['step'].startswith('daily.login')]
    assert len(login)==1
    assert login[0]['message']=='账号登录有效'
    assert login[0]['account_label']=='抖音 · 测试账号'
    with session_scope(db_engine) as db:
        assert len(json.loads(db.get(Setting,'mix.daily.status:'+identity).value_json)['login_log'])==8


def make_daily(engine, identity, **progress):
    with session_scope(engine) as session:
        row = session.get(MixTask, identity); snapshot = json.loads(row.snapshot_json)
        snapshot['daily_publish'] = {'targets': [{'account_id': f'account{i}', 'platform': 'douyin',
            'platform_user_id': 'private-uid', 'session_fingerprint': 'private-session'} for i in range(2)],
            'resolved_start': '2026-10-17T12:00:00+08:00'}
        row.snapshot_json = json.dumps(snapshot)
        session.add(Setting(key='mix.daily.status:' + identity, value_json=json.dumps(progress)))


def test_daily_login_shows_only_batch_stage_and_real_saved_verification_event(execution, db_engine):
    api, identity = execution
    make_daily(db_engine, identity, auth_phase='checking_login', auth_stage='before_publish',
        login_log=[{'time': '2026-10-15T00:59:00+00:00', 'step': 'daily.login_verified', 'stage': 'before_make', 'accounts': 2}])
    result = call(api, 'mix.daily.status', {'id': identity})
    current = result['execution']['current']
    assert current['label'] == '正在检查本批2个账号'
    assert 'account_label' not in current and 'title' not in current
    event = next(e for e in result['execution']['events'] if e['step'] == 'daily.login_verified')
    assert event['time'] == '2026-10-15T00:59:00+00:00' and event['stage'] == 'before_make'
    assert 'private' not in json.dumps(result['execution'])


def test_daily_execution_joins_actual_delivery_account_title_and_saved_steps(execution, db_engine, tmp_path):
    api, identity = execution
    account = AccountService(db_engine).create('douyin', 'private-uid', '合成账号')
    with session_scope(db_engine) as session:
        session.get(AccountRow, account.id).session_ref = 'private-session'
    path = tmp_path / 'owned.mp4'; path.write_bytes(b'owned local fixture')
    video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title='真实冻结作品')
    prepared = api._multi_publish.service.prepare(request_id='local-prepared', video_ids=[video.id],
        targets=[{'account_id': account.id, 'platform': 'douyin'}], common_fields={}, execution_mode='publish_now')
    saved = api._multi_publish.service.save(prepared['prepared_id'], request_id='local-prepared')
    make_daily(db_engine, identity, publish_task_ids=[saved['id']])
    with session_scope(db_engine) as session:
        session.add(Setting(key='multi_publish.daily_source:' + saved['id'], value_json=json.dumps({'mix_task_id': identity})))
        delivery = session.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == saved['id']))
        session.get(TaskItemRow, delivery.item_id).state = 'running'
        session.add(Setting(key=f'publish.execution:{delivery.id}:1', value_json=json.dumps([
            {'time': '2026-10-15T02:00:00+00:00', 'step': 'execution.start', 'checks': {'path': str(path), 'session': 'private-session'}},
            {'time': '2026-10-15T02:01:00+00:00', 'step': 'submission.final_check'}])))
    result = call(api, 'mix.daily.status', {'id': identity})['execution']
    assert result['current']['title'] == '真实冻结作品' and '合成账号' in result['current']['account_label']
    assert result['events'][-1]['step'] == 'execution.start'
    assert result['events'][-1]['time'] == '2026-10-15T02:00:00+00:00'
    assert result['events'][-1].get('title','') == ''  # 无制作索引证据时不猜编号，也不展示标题。
    assert str(path) not in json.dumps(result) and 'private' not in json.dumps(result)

    with session_scope(db_engine) as session:
        session.add(Setting(key=f'mix-success:{identity}:1',value_json=json.dumps({'finished_id':video.id})))
    numbered=call(api,'mix.daily.status',{'id':identity})['execution']
    assert numbered['events'][-1]['title']=='第2个视频'
    assert numbered['publishing']['targets'][0]['title']=='真实冻结作品'
    with session_scope(db_engine) as session:
        assert json.loads(session.get(PublishDeliveryRow,delivery.id).snapshot_json)['fields']['title']=='真实冻结作品'

    # Browser checks poll repeatedly, including changing timings and state.
    # The user feed shows each real step once, with its first saved timestamp.
    with session_scope(db_engine) as session:
        log = session.get(Setting, f'publish.execution:{delivery.id}:1')
        original = json.loads(log.value_json)
        repeated = [dict(time=f'2026-10-15T02:02:{i:02d}+00:00', step='douyin.form_read',
                         checks={'state': 'started' if i % 2 == 0 else 'completed', 'elapsed_ms': i})
                    for i in range(12)]
        log.value_json = json.dumps(original + repeated + [
            dict(time='2026-10-15T02:03:00+00:00', step='execution.result', checks={'state': 'platform_scheduled'})])
        saved_log = log.value_json
    first = call(api, 'mix.daily.status', {'id': identity})['execution']
    steps = [event for event in first['events'] if event['stage'] == 'publishing']
    assert len(steps) == 2
    assert all('表单' not in e['message'] for e in steps)
    assert steps[0]['time'] == '2026-10-15T02:00:00+00:00'
    assert steps[-1]['step'] == 'execution.result'
    assert call(api, 'mix.daily.status', {'id': identity})['execution'] == first
    with session_scope(db_engine) as session:
        assert session.get(Setting, f'publish.execution:{delivery.id}:1').value_json == saved_log
        session.get(Setting, f'publish.execution:{delivery.id}:1').value_json = json.dumps(
            json.loads(saved_log) + [dict(time='2026-10-15T02:04:00+00:00', step='execution.result', checks={'state': 'published'})])
    reconciled = call(api, 'mix.daily.status', {'id': identity})['execution']
    results = [e for e in reconciled['events'] if e['step'] == 'execution.result']
    assert len(results) == 1 and results[0]['time'] == '2026-10-15T02:04:00+00:00'


def test_saved_event_feed_is_bounded_and_does_not_fabricate_missing_timestamps(execution, db_engine):
    api, identity = execution
    with session_scope(db_engine) as session:
        row = session.get(Setting, f'mix-result:{identity}:0')
        row.value_json = json.dumps({'state': 'failed', 'execution_log':
            [{'time': f'2026-10-15T01:00:{i % 60:02d}+00:00', 'step': 'mix.failed'} for i in range(150)] +
            [{'step': 'mix.started', 'path': '/private/session/secret'}]})
    result = call(api, 'task.poll', {'epoch': None, 'after': None})['tasks'][0]['execution']
    assert len(result['events']) == 1 and all('time' in event for event in result['events'])
    assert result['events'][0]['time']=='2026-10-15T01:00:29+00:00'
    with session_scope(db_engine) as session:
        assert len(json.loads(session.get(Setting,f'mix-result:{identity}:0').value_json)['execution_log'])==151


def test_unknown_saved_step_names_cannot_export_private_values(execution, db_engine):
    api, identity = execution
    with session_scope(db_engine) as session:
        row = session.get(Setting, f'mix-result:{identity}:0')
        row.value_json = json.dumps({'state': 'completed', 'execution_log': [
            {'time': '2026-10-15T01:00:00+00:00', 'step': 'private_uid', 'message': 'Cookie=private-session'}]})
    result = call(api, 'task.poll', {'epoch': None, 'after': None})['tasks'][0]['execution']
    assert result['events'] == []
    assert 'private' not in json.dumps(result) and 'Cookie' not in json.dumps(result)


def test_only_make_failure_has_a_real_short_reason_without_private_values(execution, db_engine):
    api, identity = execution
    with session_scope(db_engine) as session:
        row = session.get(Setting, f'mix-result:{identity}:0')
        row.value_json = json.dumps({'state': 'failed', 'diagnostic': {
            'reason': '素材解码失败：/private/fixture/video.mp4 session_ref=private-session uid=private-uid ' + '说明' * 200 + ' Cookie=private-cookie'},
            'execution_log': [{'time': '2026-10-15T01:01:00+00:00', 'step': 'mix.failed'}]})
    result = call(api, 'task.poll', {'epoch': None, 'after': None})['tasks'][0]['execution']
    assert '素材解码失败' in result['reason'] and len(result['reason']) == 255
    assert '素材解码失败' in result['events'][0]['message']
    assert all(value not in json.dumps(result) for value in ('/private', 'private-cookie', 'private-session', 'private-uid'))


def test_old_malformed_optional_display_shapes_do_not_break_task_poll(execution, db_engine):
    api, identity = execution
    with session_scope(db_engine) as session:
        row = session.get(MixTask, identity)
        row.snapshot_json = json.dumps({'quantity': 3, 'copies': [None, []], 'daily_publish': []})
        session.add(Setting(key='mix.daily.status:' + identity, value_json=json.dumps({
            'login_log': None, 'publish_task_ids': [None, {}]})))
        receipt = session.get(Setting, f'mix-result:{identity}:0')
        receipt.value_json = json.dumps({'execution_log': [{'time': '2026-10-15T01:01:00+00:00', 'step': []}]})
    result = call(api, 'task.poll', {'epoch': None, 'after': None})['tasks'][0]['execution']
    assert result['events'] == [] and result['current']['title'] == ''
    assert result['making']['total'] == 3
