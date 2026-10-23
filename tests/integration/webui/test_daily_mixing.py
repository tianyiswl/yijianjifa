"""Daily intent keeps real mixing receipts and V2 saving/grants; only media/platform IO is fake."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_runtime import authorized_targets
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.mixing.ffmpeg_renderer import RenderResult
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation
from yijianjifa.features.publish.deliveries import PublishBatchReceiptRow, PublishDeliveryRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def call(api, op, params=None, rid='daily-read'):
    return api.dispatch(json.dumps(dict(v=1, request_id=rid, operation=op, params=params or {})), trusted_origin=True)


class Adapter:
    platform_id = 'douyin'
    verified_capabilities = {'ordinary', 'publish_now', 'platform_schedule', 'result_query'}
    def validate_target(self, target, payload, execution):
        return AdapterValidation(payload['platform_user_id'], True)
    def submit(self, payload, *, context, before_final):
        before_final()
        return AdapterOutcome('platform_scheduled', scheduled_at=payload['scheduled_at']) if payload['scheduled_at'] else AdapterOutcome('published')
    def reconcile(self, payload, *, context):
        return AdapterOutcome('published')


class Renderer:
    def __init__(self):
        self.index = 0
        self.fail = set()
    def render(self, request):
        index = self.index
        self.index += 1
        if index in self.fail:
            raise ValueError('synthetic render failed')
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(('rendered-' + str(index)).encode())
        return RenderResult(request.output_path, sum(s.duration_ms for s in request.segments), 720, 1280)


@pytest.fixture
def daily(db_engine, app_paths, tmp_path, monkeypatch, daily_auth_sync):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'synthetic input')
    MediaStorageService(db_engine, app_paths, kind='video').import_media(source, keep_copy=False, category_ids=[])
    CopyRepository(db_engine).create(id='C-daily', title='每天的作品', voice='', body='正文', tags='标签', full='')
    account = AccountService(db_engine).create('douyin', '111', '合成账号')
    with session_scope(db_engine) as s:
        s.get(AccountRow, account.id).session_ref = 'synthetic-local-session'
    api = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': Adapter()})
    daily_auth_sync(api)
    renderer = Renderer()
    api._mix.executor.renderer = renderer
    api._mix.probe = lambda path: 100000
    monkeypatch.setattr(api._multi_publish, 'wake', lambda: None)
    params = {'mix': {'quantity': 3, 'total_ms': 1000, 'segment_ms': 1000},
              'publishing': {'targets': [{'account_id': account.id, 'platform': 'douyin'}],
                             'mode': 'platform_schedule', 'time': '12:00', 'day_offset': 1, 'interval_hours': 3}}
    yield api, params, renderer, account
    api.close(3)


def submitted(daily, rid='daily-click'):
    response = call(daily[0], 'mix.daily.submit', daily[1], rid)
    assert response['ok'], response
    return response['data']


@pytest.mark.parametrize('operation', ['mix.submit', 'mix.daily.submit'])
def test_transient_submit_service_freezes_live_voice_identity(daily, operation):
    api, params, _, _ = daily
    class Voice:
        def freeze(self, snapshot):
            assert len(snapshot['copies']) == snapshot['quantity']
            return {'source': 'hosted', 'account_id': 'native-cloud-owner'}
    api._mix.tts = Voice()
    payload = params if operation == 'mix.daily.submit' else params['mix']
    result = call(api, operation, payload, 'cloud-freeze-' + operation.replace('.', '-'))
    assert result['ok'], result
    task = api._mix.queue.get(result['data']['id'])
    assert task.snapshot['voice_identity'] == {'source': 'hosted', 'account_id': 'native-cloud-owner'}


@pytest.mark.parametrize('operation', ['mix.submit', 'mix.daily.submit'])
def test_missing_cloud_login_is_actionable_before_creating_task(daily, operation):
    from yijianjifa.features.mixing.tts.hosted import VoicePaused
    api, params, _, _ = daily
    class Voice:
        def freeze(self, snapshot):
            raise VoicePaused('authentication_required', '请先登录配音账号，再开始本批制作。')
    api._mix.tts = Voice()
    payload = params if operation == 'mix.daily.submit' else params['mix']
    result = call(api, operation, payload, 'cloud-no-login-' + operation.replace('.', '-'))
    assert not result['ok'] and result['error']['code'] == 'authentication_required'
    assert '先登录配音账号' in result['error']['message']
    assert not api._mix.tasks()


def test_cloud_pause_can_continue_and_old_message_clears_after_login_check(daily, db_engine, monkeypatch):
    from yijianjifa.features.mixing.tts.hosted import VoicePaused
    api, _, _, _ = daily
    identity = submitted(daily)['id']
    def blocked(*args):
        raise VoicePaused('insufficient_credit', '本批需要 20 字，剩余 10 字。')
    monkeypatch.setattr(api._mix.executor, 'render_item', blocked)
    api._mix.queue.run_once_until_idle()
    paused = call(api, 'mix.daily.status', {'id': identity})['data']
    assert paused['phase'] == 'needs_attention' and paused['can_continue'] is True
    assert '剩余 10 字' in paused['reason']
    assert paused['execution']['making']['failed'] == 0
    original = api._mix.queue.get(identity).planned_publish_times
    continued = call(api, 'mix.daily.continue', {'id': identity}, 'cloud-resume-after-topup')
    assert continued['ok'], continued
    with session_scope(db_engine) as session:
        assert session.get(Setting, 'mix-voice-pause:' + identity) is None
    api._mix.queue.request_pause(identity)
    plain_pause = call(api, 'mix.daily.status', {'id': identity})['data']
    assert '剩余 10 字' not in plain_pause['reason']
    assert plain_pause['execution'].get('reason') != '本批需要 20 字，剩余 10 字。'
    assert api._mix.queue.get(identity).planned_publish_times == original


def batches(engine):
    with session_scope(engine) as s:
        return list(s.scalars(select(PublishBatchReceiptRow)))


def test_daily_read_save_only_reusable_settings_and_public_account_metadata(daily, db_engine):
    api, params, _, account = daily
    initial = call(api, 'mix.daily.read')['data']
    assert initial['settings'] is None and initial['has_saved_settings'] is False
    assert initial['accounts'][0]['id'] == account.id
    assert initial['accounts'][0]['login_state'] == 'valid'
    assert 'session_ref' not in json.dumps(initial) and 'platform_user_id' not in json.dumps(initial)
    assert call(api, 'mix.daily.save', params, 'daily-save')['ok']
    remembered = call(api, 'mix.daily.read')['data']
    assert remembered['settings'] == params and remembered['has_saved_settings'] is True
    assert api._mix.tasks() == [] and batches(db_engine) == []


def test_submit_replay_is_one_batch_new_click_creates_new_batch_and_freezes_settings(daily, db_engine):
    api, params, _, _ = daily
    first = submitted(daily)
    assert submitted(daily) == first
    assert submitted(daily, 'daily-new-click')['id'] != first['id']
    assert len(api._mix.tasks()) == 2
    frozen = api._mix.queue.get(first['id']).snapshot
    assert 'daily_publish' in frozen and 'publish_after' not in frozen
    assert frozen['auto_publish'] is False
    changed = copy.deepcopy(params)
    changed['mix']['quantity'] = 1
    assert call(api, 'mix.daily.submit', changed, 'daily-click')['error']['code'] == 'request_conflict'
    assert call(api, 'mix.daily.read')['data']['settings'] == params
    assert batches(db_engine) == []


def test_new_batch_can_be_created_and_pending_batch_deleted_without_resuming_it(daily,db_engine):
    from yijianjifa.features.mixing.queue import MixTask
    from yijianjifa.app_services.daily_mixing import STATUS
    api,params,_,_=daily
    old=submitted(daily,'original-pending')
    with session_scope(db_engine) as s:
        s.get(MixTask,old['id']).state='paused'
        progress=s.get(Setting,STATUS+old['id']);data=json.loads(progress.value_json)
        progress.value_json=json.dumps({**data,'auth_phase':'needs_attention','auth_host':api._daily.session_id})
    new=submitted(daily,'independent-new-batch')
    assert new['id']!=old['id'] and len(api._mix.tasks())==2
    with session_scope(db_engine) as s:assert s.get(MixTask,old['id']).state=='paused'
    deleted=call(api,'task.delete',{'id':old['id']},'delete-pending-make')
    assert deleted['ok'],deleted
    with session_scope(db_engine) as s:
        assert s.get(MixTask,old['id']).state=='cancelled'
        assert s.get(Setting,'task-hidden:'+old['id'])
        assert s.get(MixTask,new['id']).state!='cancelled'


def test_daily_clock_is_resolved_to_tomorrow_from_each_new_run(daily, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 5, tzinfo=timezone.utc))
    first = submitted(daily)
    assert first['resolved_start'] == '2026-10-16T12:00:00+08:00'
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 17, 1, tzinfo=timezone.utc))
    assert submitted(daily, 'next-day')['resolved_start'] == '2026-10-18T12:00:00+08:00'
    assert call(api, 'mix.daily.read')['data']['settings']['publishing']['time'] == '12:00'


def test_real_success_receipts_create_only_this_batch_v2_targets_and_exact_attempt_grants(daily, db_engine, tmp_path):
    api, params, renderer, _ = daily
    outsider = tmp_path / 'unrelated.mp4'
    outsider.write_bytes(b'unrelated')
    unrelated = FinishedVideoService(db_engine).import_external(outsider, duration_ms=1000, title='别的成品')
    renderer.fail = {1}
    first = submitted(daily)
    api._mix.queue._run_single_item(first['id'])
    api._runtime.handoff(first['id'])
    assert batches(db_engine) == []  # The current batch has not settled.
    api._mix.queue.run_once_until_idle()
    status = call(api, 'mix.daily.status', {'id': first['id']})['data']
    assert status['phase'] == 'publishing' and len(status['publish_task_ids']) == 1
    task_id = status['publish_task_ids'][0]
    detail = api._multi_publish.detail(task_id)
    assert len(detail['deliveries']) == 2 and unrelated.id not in {d['video_id'] for d in detail['deliveries']}
    start = datetime.fromisoformat(first['resolved_start'])
    assert [datetime.fromisoformat(d['scheduled_at']).astimezone(start.tzinfo).hour for d in detail['deliveries']] == [12, 18]
    with session_scope(db_engine) as s:
        grants = authorized_targets(s, task_id)
        assert set(grants) == {d['id'] for d in detail['deliveries']}
        for did, grant in grants.items():
            row = s.get(PublishDeliveryRow, did)
            assert grant == {'attempt': row.attempt, 'request_digest': row.request_digest}
    api._runtime.handoff(first['id'])
    assert len(batches(db_engine)) == 1
    for _ in range(3):
        api._multi_publish.coordinator.run_once(task_id, list(grants))
    status = call(api, 'mix.daily.status', {'id': first['id']})['data']
    assert status['phase'] == 'needs_attention' and status['accepted'] == 2 and status['failed'] == 1


@pytest.mark.parametrize('control', ['task.pause', 'task.cancel'])
def test_paused_or_cancelled_batch_never_creates_a_publish_batch(daily, db_engine, control):
    api = daily[0]
    task = submitted(daily)
    api._mix.queue._run_single_item(task['id'])
    assert call(api, control, {'id': task['id']}, 'control')['ok']
    api._runtime.handoff(task['id'])
    assert batches(db_engine) == []
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert status['phase'] == ('cancelled' if control == 'task.cancel' else 'needs_attention')


def test_all_failed_batch_keeps_specific_reason_and_creates_no_publish_batch(daily, db_engine):
    api, _, renderer, _ = daily
    renderer.fail = {0, 1, 2}
    task = submitted(daily)
    api._mix.queue.run_once_until_idle()
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert status['phase'] == 'needs_attention' and status['failed'] == 3
    assert status['reason'] and batches(db_engine) == []


def test_reopen_replay_never_grants_but_explicit_online_checked_resume_continues_original_batch(daily, db_engine, app_paths, monkeypatch, daily_auth_sync):
    api, params, _, _ = daily
    task = submitted(daily)
    assert api.close(2)
    reopened = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': Adapter()})
    daily_auth_sync(reopened)
    reopened._mix.executor.renderer = Renderer()
    reopened._mix.probe = lambda path: 100000
    monkeypatch.setattr(reopened._multi_publish, 'wake', lambda: None)
    try:
        assert call(reopened, 'mix.daily.submit', params, 'daily-click')['data']['id'] == task['id']
        assert batches(db_engine) == []
        assert reopened._mix.queue.get(task['id']).state == 'paused'
        assert call(reopened, 'task.resume', {'id': task['id']}, 'explicit-mix-resume')['ok']
        reopened._mix.queue.run_once_until_idle()
        assert len(batches(db_engine)) == 1
        status = call(reopened, 'mix.daily.status', {'id': task['id']})['data']
        assert status['phase'] == 'publishing'
        assert len(reopened._mix.tasks()) == 1
    finally:
        reopened.close(2)


def test_changed_account_identity_during_making_is_not_auto_authorized(daily, db_engine):
    api, _, _, account = daily
    task = submitted(daily)
    with session_scope(db_engine) as s:
        s.get(AccountRow, account.id).platform_user_id = '222'
    api._mix.queue.run_once_until_idle()
    assert batches(db_engine) == []
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert status['phase'] == 'needs_attention' and status['reason_code'] == 'daily_target_changed'


def test_reopen_revokes_daily_waiting_grants_without_changing_ordinary_v2_grants(daily, db_engine, app_paths):
    api, params, _, _ = daily
    task = submitted(daily)
    api._mix.queue.run_once_until_idle()
    batch = batches(db_engine)[0].task_id
    with session_scope(db_engine) as s:
        assert authorized_targets(s, batch)
        s.add(Setting(key='multi_publish.authorization:ordinary-unrelated', value_json='{"version":2,"delivery_ids":[],"targets":{}}'))
    assert api.close(2)
    reopened = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': Adapter()})
    try:
        with session_scope(db_engine) as s:
            assert authorized_targets(s, batch) == {}
            assert s.get(Setting, 'multi_publish.authorization:ordinary-unrelated') is not None
        status = call(reopened, 'mix.daily.status', {'id': task['id']})['data']
        assert status['phase'] == 'needs_attention' and status['pending'] == 3
    finally:
        reopened.close(2)


def test_runtime_close_before_settlement_cannot_save_or_authorize_new_publish_work(daily, db_engine):
    api = daily[0]
    task = submitted(daily)
    assert api._multi_publish.close(1)
    api._mix.queue.run_once_until_idle()
    assert batches(db_engine) == []
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert status['phase'] == 'needs_attention' and status['reason_code'] == 'app_closing'


def test_platform_defaults_are_frozen_for_this_intent_without_changing_original_settings(daily, db_engine):
    api, params, _, _ = daily
    from yijianjifa.app_services import multi_publish_settings
    defaults = multi_publish_settings.defaults()
    defaults['fields_by_platform'] = {'douyin': {'title': '旧标题', 'body': '第一次默认正文', 'tags': '旧标签'}}
    multi_publish_settings.write(db_engine, defaults)
    CopyRepository(db_engine).create(id='C-second', title='第二条作品', voice='', body='第二条正文', tags='第二条标签', full='')
    submitted(daily)
    defaults['fields_by_platform']['douyin']['body'] = '中途改过的默认正文'
    multi_publish_settings.write(db_engine, defaults)
    api._mix.queue.run_once_until_idle()
    detail = api._multi_publish.detail(batches(db_engine)[0].task_id)
    assert {(d['effective_fields']['title'], d['effective_fields']['body'], d['effective_fields']['tags']) for d in detail['deliveries']} == {
        ('每天的作品', '正文', '标签'), ('第二条作品', '第二条正文', '第二条标签')}
    assert multi_publish_settings.read(db_engine)['settings']['fields_by_platform']['douyin']['body'] == '中途改过的默认正文'


@pytest.mark.parametrize('change', ['publish_after', 'forged_platform', 'empty_targets', 'invalid_clock'])
def test_invalid_daily_intent_is_rejected_before_any_settings_or_mixing_write(daily, change):
    api, params, _, _ = daily
    if change == 'publish_after': params['mix']['publish_after'] = {}
    elif change == 'forged_platform': params['publishing']['targets'][0]['platform'] = 'channels'
    elif change == 'empty_targets': params['publishing']['targets'] = []
    else: params['publishing']['time'] = '25:00'
    assert call(api, 'mix.daily.submit', params, 'bad-daily')['ok'] is False
    assert api._mix.tasks() == []
    assert call(api, 'mix.daily.read')['data']['has_saved_settings'] is False


def test_same_session_explicit_resume_restores_paused_original_daily_intent(daily, db_engine):
    api = daily[0]
    task = submitted(daily)
    assert call(api, 'task.pause', {'id': task['id']}, 'pause-first')['ok']
    assert call(api, 'task.resume', {'id': task['id']}, 'resume-making')['ok']
    api._mix.queue.run_once_until_idle()
    assert len(batches(db_engine)) == 1
    assert call(api, 'mix.daily.status', {'id': task['id']})['data']['phase'] == 'publishing'


def test_pause_during_last_render_requires_explicit_same_session_resume_before_handoff(daily, db_engine):
    api, params, renderer, _ = daily
    params['mix']['quantity'] = 1
    task = submitted(daily)
    original = renderer.render
    def pause_while_rendering(request):
        assert call(api, 'task.pause', {'id': task['id']}, 'pause-last-render')['ok']
        return original(request)
    renderer.render = pause_while_rendering
    api._mix.queue.run_once_until_idle()
    assert batches(db_engine) == []
    assert api._mix.queue.get(task['id']).state == 'paused'
    assert call(api, 'mix.daily.status', {'id': task['id']})['data']['reason_code'] == 'daily_batch_paused'
    assert call(api, 'task.resume', {'id': task['id']}, 'resume-last-render')['ok']
    api._mix.queue.run_once_until_idle()
    assert len(batches(db_engine)) == 1 and renderer.index == 1


def test_local_platform_field_preflight_rejects_before_rendering_and_returns_actionable_reason(daily, db_engine):
    api, params, _, account = daily
    api._multi_publish.catalog.transport_for('douyin').adapter.plan_readiness = lambda payload: 'platform_fields_invalid'
    expected = 'platform_fields_invalid'
    response = call(api, 'mix.daily.submit', params, 'blocked-daily')
    assert response['ok'] is False and response['error']['code'] == expected
    assert response['error']['message'] != '读取资料失败，请稍后重试。'
    assert api._mix.tasks() == [] and batches(db_engine) == []


def test_daily_schedule_uses_v2_and_reports_accepted_only_after_actual_receipts(daily, db_engine):
    api, params, _, _ = daily
    task = submitted(daily)
    assert task['resolved_start'] is not None
    api._mix.queue.run_once_until_idle()
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert status['phase'] == 'publishing' and status['accepted'] == 0
    pid = status['publish_task_ids'][0]
    with session_scope(db_engine) as s: ids = list(authorized_targets(s, pid))
    for _ in range(3): api._multi_publish.coordinator.run_once(pid, ids)
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert status['phase'] == 'accepted' and status['published'] == 0 and status['accepted'] == 3


def test_legacy_mix_submit_cannot_inject_daily_authorization(daily):
    api, params, _, _ = daily
    response = call(api, 'mix.submit', {**params['mix'], 'daily_publish': {'host_session': 'forged'}}, 'forged-daily')
    assert not response['ok'] and api._mix.tasks() == []


@pytest.mark.parametrize('stage', ['prepare', 'save'])
@pytest.mark.parametrize('control', ['task.pause', 'task.cancel'])
def test_control_between_completion_and_final_grant_cannot_be_overwritten(daily, db_engine, stage, control):
    api = daily[0]
    task = submitted(daily)
    handoff = api._runtime.daily_handoff
    api._runtime.daily_handoff = None
    api._mix.queue.run_once_until_idle()
    api._runtime.daily_handoff = handoff
    original = getattr(api._multi_publish.service, stage)
    def intervene(*args, **kwargs):
        result = original(*args, **kwargs)
        assert call(api, control, {'id': task['id']}, 'late-control')['ok']
        return result
    setattr(api._multi_publish.service, stage, intervene)
    api._runtime.handoff(task['id'])
    with session_scope(db_engine) as s:
        for batch in batches(db_engine):
            assert authorized_targets(s, batch.task_id) == {}
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert status['phase'] == ('cancelled' if control == 'task.cancel' else 'needs_attention')
    assert status['reason_code'] == ('daily_batch_cancelled' if control == 'task.cancel' else 'daily_batch_paused')


def test_finished_metadata_edited_while_making_does_not_replace_frozen_copy(daily, db_engine):
    api = daily[0]
    task = submitted(daily)
    api._mix.queue._run_single_item(task['id'])
    with session_scope(db_engine) as s:
        finished_id = json.loads(s.get(Setting, 'mix-success:' + task['id'] + ':0').value_json)['finished_id']
    FinishedVideoService(db_engine).update_metadata(finished_id, title='成品库后来改的标题', body='后来改的正文', tags='后来改的标签')
    api._mix.queue.run_once_until_idle()
    first = next(d for d in api._multi_publish.detail(batches(db_engine)[0].task_id)['deliveries'] if d['video_id'] == finished_id)
    assert {k: first['effective_fields'][k] for k in ('title', 'body', 'tags')} == {'title': '每天的作品', 'body': '正文', 'tags': '标签'}


def test_real_douyin_local_preflight_supports_scheduled_intent_and_exposes_actual_capabilities(daily, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    real_adapter = ChromiumMultiPublishAdapter(db_engine, app_paths)
    def browser_forbidden(*args, **kwargs):
        raise AssertionError('Preflight must not access a browser')
    monkeypatch.setattr(real_adapter, '_open', browser_forbidden)
    api = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': real_adapter})
    try:
        params = daily[1]
        result = call(api, 'mix.daily.submit', params, 'real-local-preflight')
        assert result['ok'], result
        capabilities = call(api, 'mix.daily.read')['data']['capabilities']['douyin']
        assert 'platform_schedule' in capabilities and 'publish_now' not in capabilities
        params = copy.deepcopy(params)
        params['publishing']['mode'] = 'publish_now'
        result = call(api, 'mix.daily.submit', params, 'real-unsupported-now')
        assert not result['ok'] and result['error']['code'] == 'daily_schedule_only'
        assert len(api._mix.tasks()) == 1
    finally:
        api.close(2)


def test_excluded_unresolved_target_remains_visible_in_daily_failure_total(daily, db_engine):
    api, params, _, account = daily
    task = submitted(daily)
    api._mix.queue._run_single_item(task['id'])
    with session_scope(db_engine) as s:
        first_id = json.loads(s.get(Setting, 'mix-success:' + task['id'] + ':0').value_json)['finished_id']
    service = api._multi_publish.service
    existing = service.prepare(request_id='earlier-unresolved', video_ids=[first_id], targets=params['publishing']['targets'], common_fields={}, execution_mode='publish_now')
    service.save(existing['prepared_id'], request_id='earlier-unresolved')
    api._mix.queue.run_once_until_idle()
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    pid = status['publish_task_ids'][0]
    with session_scope(db_engine) as s: ids = list(authorized_targets(s, pid))
    for _ in range(2): api._multi_publish.coordinator.run_once(pid, ids)
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert status['phase'] == 'needs_attention' and status['total'] == 3
    assert status['accepted'] == 2 and status['published'] == 0 and status['failed'] == 1 and status['excluded_count'] == 1
    assert status['reason_code'] == 'existing_target_unresolved' and status['reason']


@pytest.mark.parametrize('control', ['task.pause', 'task.cancel'])
def test_control_after_v2_link_is_saved_still_prevents_first_authorization(daily, db_engine, control):
    api = daily[0]
    task = submitted(daily)
    handoff = api._runtime.daily_handoff
    api._runtime.daily_handoff = None
    api._mix.queue.run_once_until_idle()
    api._runtime.daily_handoff = handoff
    original_lock = api._multi_commands.lock
    class InterveneBeforeGrant:
        def __enter__(self):
            assert call(api, control, {'id': task['id']}, 'linked-before-grant-control')['ok']
            return original_lock.__enter__()
        def __exit__(self, *args):
            return original_lock.__exit__(*args)
    api._multi_commands.lock = InterveneBeforeGrant()
    api._runtime.handoff(task['id'])
    with session_scope(db_engine) as s:
        assert all(authorized_targets(s, batch.task_id) == {} for batch in batches(db_engine))
    assert call(api, 'mix.daily.status', {'id': task['id']})['data']['phase'] == ('cancelled' if control == 'task.cancel' else 'needs_attention')


def test_saving_a_draft_without_accounts_preserves_generation_settings_without_authority(daily, db_engine):
    api, params, _, _ = daily
    params['publishing']['targets'] = []
    params['mix']['quantity'] = 120
    response = call(api, 'mix.daily.save', params, 'empty-account-draft')
    assert response['ok'], response
    assert call(api, 'mix.daily.read')['data']['settings'] == params
    assert not call(api, 'mix.daily.submit', params, 'not-a-publishable-draft')['ok']
    assert api._mix.tasks() == [] and batches(db_engine) == []


def test_daily_schedule_uses_days_times_per_day_and_restarts_the_clock_each_day(daily, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 1, tzinfo=timezone.utc))
    params['publishing'].update(days=3, per_day=2)
    params['mix']['quantity'] = 777  # A stale only-generate quantity is not publication authority.
    task = submitted(daily)
    assert task['quantity'] == 6
    frozen = api._mix.queue.get(task['id']).snapshot
    assert frozen['quantity'] == 6 and len(frozen['copies']) == 6
    assert frozen['daily_publish']['slots'] == [
        '2026-10-16T12:00:00+08:00', '2026-10-16T15:00:00+08:00',
        '2026-10-17T12:00:00+08:00', '2026-10-17T15:00:00+08:00',
        '2026-10-18T12:00:00+08:00', '2026-10-18T15:00:00+08:00',
    ]
    saved = call(api, 'mix.daily.read')['data']['settings']
    assert saved['mix']['quantity'] == 777 and saved['publishing']['days'] == 3


@pytest.mark.parametrize(('days', 'per_day', 'expected'), [(1, 2, 2), (3, 1, 3), (3, 2, 6)])
def test_changing_only_days_or_per_day_recomputes_the_actual_mix_quantity(daily, days, per_day, expected):
    api, params, _, _ = daily
    params['publishing'].update(days=days, per_day=per_day)
    params['mix']['quantity'] = 40
    task = submitted(daily)
    assert task['quantity'] == expected and api._mix.queue.get(task['id']).quantity == expected


@pytest.mark.parametrize(('clock', 'per_day', 'interval'), [('23:00', 2, 1), ('12:00', 5, 3), ('20:00', 2, 5)])
def test_daily_schedule_rejects_midnight_spill_instead_of_moving_items_to_the_next_day(daily, db_engine, clock, per_day, interval):
    api, params, _, _ = daily
    params['publishing'].update(time=clock, days=2, per_day=per_day, interval_hours=interval)
    result = call(api, 'mix.daily.submit', params, 'spills-midnight')
    assert not result['ok'] and result['error']['code'] == 'daily_schedule_invalid'
    assert '每天' in result['error']['message'] and '间隔' in result['error']['message']
    assert api._mix.tasks() == [] and batches(db_engine) == []


@pytest.mark.parametrize('rule', [dict(days=2), dict(per_day=2), dict(days=True, per_day=2), dict(days=0, per_day=2), dict(days=51, per_day=2)])
def test_daily_rule_requires_a_positive_integer_pair_and_at_most_100_publication_items(daily, rule):
    api, params, _, _ = daily
    params['publishing'].update(rule)
    assert not call(api, 'mix.daily.submit', params, 'invalid-daily-rule')['ok']
    assert api._mix.tasks() == []


def test_old_daily_settings_keep_continuous_interval_slots_without_a_day_rule(daily):
    api, params, _, _ = daily
    params['mix']['quantity'] = 6
    task = submitted(daily)
    slots = [datetime.fromisoformat(value) for value in api._mix.queue.get(task['id']).snapshot['daily_publish']['slots']]
    assert [value.hour for value in slots] == [12, 15, 18, 21, 0, 3]
    assert (slots[-1].date() - slots[0].date()).days == 1


def test_daily_rule_keeps_failed_item_original_day_and_slot_when_handed_to_v2(daily, db_engine):
    api, params, renderer, _ = daily
    params['publishing'].update(days=3, per_day=2)
    renderer.fail = {1, 2}
    task = submitted(daily)
    api._mix.queue.run_once_until_idle()
    status = call(api, 'mix.daily.status', {'id': task['id']})['data']
    assert len(status['publish_task_ids']) == 1
    children = api._multi_publish.detail(status['publish_task_ids'][0])['deliveries']
    slots = api._mix.queue.get(task['id']).snapshot['daily_publish']['slots']
    assert [datetime.fromisoformat(child['scheduled_at']).astimezone(timezone.utc).isoformat() for child in children] == [
        datetime.fromisoformat(slots[i]).astimezone(timezone.utc).isoformat() for i in (0, 3, 4, 5)]


def test_daily_rule_draft_can_be_saved_while_its_quantity_or_times_still_need_adjustment(daily, db_engine):
    api, params, _, _ = daily
    params['publishing'].update(targets=[], days=3, per_day=50, time='23:00')
    params['mix']['quantity'] = 120
    result = call(api, 'mix.daily.save', params, 'save-incomplete-rule')
    assert result['ok'], result
    assert call(api, 'mix.daily.read')['data']['settings'] == params
    assert api._mix.tasks() == [] and batches(db_engine) == []


@pytest.mark.parametrize('legacy', ['broken', json.dumps({
    'started_at':'2020-01-01T00:00:00+00:00', 'expires_at':'2021-01-01T00:00:00+00:00',
    'last_seen_at':'2026-01-01T00:00:00+00:00'})])
def test_legacy_term_allows_daily_making_and_synthetic_publish_handoff(daily, db_engine, legacy):
    api, params, renderer, _ = daily
    from yijianjifa.app_services.use_term import UseTerm
    with session_scope(db_engine) as session:
        session.add(Setting(key=UseTerm.KEY, value_json=legacy))
    result = call(api, 'mix.daily.submit', params, 'free-daily-intent')
    assert result['ok'], result
    api._mix.queue.run_once_until_idle()
    task = api._mix.queue.get(result['data']['id'])
    assert task.success_count == 3 and renderer.index == 3
    status = call(api, 'mix.daily.status', {'id': task.id})['data']
    assert len(status['publish_task_ids']) == 1
    with session_scope(db_engine) as session:
        assert session.get(Setting, UseTerm.KEY).value_json == legacy


def test_selected_start_date_freezes_exact_shanghai_date_and_each_days_clock(daily, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 19, 5, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing'].update(start_date='2026-10-20', days=3, per_day=2)
    params['mix']['quantity'] = 40
    task = submitted(daily)
    assert task['quantity'] == 6 and task['resolved_start'] == '2026-10-20T12:00:00+08:00'
    assert api._mix.queue.get(task['id']).snapshot['daily_publish']['slots'] == [
        '2026-10-20T12:00:00+08:00', '2026-10-20T15:00:00+08:00',
        '2026-10-21T12:00:00+08:00', '2026-10-21T15:00:00+08:00',
        '2026-10-22T12:00:00+08:00', '2026-10-22T15:00:00+08:00',
    ]
    saved = call(api, 'mix.daily.read')['data']['settings']
    assert saved['mix']['quantity'] == 40 and saved['publishing']['start_date'] == '2026-10-20'
    assert 'day_offset' not in saved['publishing']


@pytest.mark.parametrize('invalid_date', ['2026-02-29', '2026-13-01', '2026-10-32', '2026-1-01', '20261020', '2026-10-20T00:00:00', None, 20261020])
def test_explicit_start_date_requires_a_real_calendar_date_in_exact_iso_format(daily, invalid_date):
    api, params, _, _ = daily
    params['publishing'].pop('day_offset')
    params['publishing']['start_date'] = invalid_date
    assert not call(api, 'mix.daily.save', params, 'invalid-date-save')['ok']
    assert not call(api, 'mix.daily.submit', params, 'invalid-date-submit')['ok']
    assert api._mix.tasks() == [] and call(api, 'mix.daily.read')['data']['settings'] is None


@pytest.mark.parametrize('choice', ['both', 'neither'])
def test_explicit_date_and_relative_offset_are_strictly_exclusive(daily, choice):
    api, params, _, _ = daily
    if choice == 'both':
        params['publishing']['start_date'] = '2026-10-20'
    else:
        params['publishing'].pop('day_offset')
    assert not call(api, 'mix.daily.save', params, 'ambiguous-start-save')['ok']
    assert not call(api, 'mix.daily.submit', params, 'ambiguous-start-submit')['ok']
    assert api._mix.tasks() == []


@pytest.mark.parametrize(('start_date', 'clock'), [('2026-10-14', '23:00'), ('2026-10-15', '12:00'), ('2026-10-15', '13:00')])
def test_explicit_past_start_is_rejected_without_rolling_forward(daily, db_engine, monkeypatch, start_date, clock):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 5, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing'].update(start_date=start_date, time=clock)
    result = call(api, 'mix.daily.submit', params, 'past-explicit-start')
    assert not result['ok'] and result['error']['code'] == 'daily_start_out_of_range'
    assert '明天' in result['error']['message'] and '重新选择' in result['error']['message']
    assert api._mix.tasks() == [] and batches(db_engine) == []


def test_explicit_today_future_start_is_rejected_and_legacy_past_draft_remains_readable(daily, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 5, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing'].update(start_date='2026-10-14', time='12:00')
    saved = call(api, 'mix.daily.save', params, 'past-explicit-draft')
    assert saved['ok'] and saved['data']['resolved_start'] == '2026-10-14T12:00:00+08:00'
    assert call(api, 'mix.daily.read')['data']['settings'] == params
    assert not call(api, 'mix.daily.submit', params, 'reject-past-draft')['ok']
    assert call(api, 'mix.daily.read')['data']['settings'] == params
    params['publishing'].update(start_date='2026-10-15', time='14:00')
    rejected = call(api, 'mix.daily.submit', params, 'reject-today-future')
    assert not rejected['ok'] and rejected['error']['code'] == 'daily_start_out_of_range'
    params['publishing']['start_date'] = '2026-10-16'
    assert submitted(daily)['resolved_start'] == '2026-10-16T14:00:00+08:00'


def test_selected_date_past_check_uses_shanghai_day_near_utc_midnight(daily, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 14, 18, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing'].update(start_date='2026-10-15', time='01:00')
    response = call(api, 'mix.daily.submit', params, 'shanghai-start-date')
    assert not response['ok'] and response['error']['code'] == 'daily_start_out_of_range'


def test_daily_fourteen_day_cap_preserves_stricter_real_douyin_local_window(daily, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    from yijianjifa.webui import chromium_publish
    from yijianjifa.app_services import daily_mixing
    reference = datetime(2026, 10, 15, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: reference)
    real_validate_form = chromium_publish.validate_form_request
    def validate_at_reference(**kwargs):
        return real_validate_form(**kwargs, now=reference)
    monkeypatch.setattr(chromium_publish, 'validate_form_request', validate_at_reference)
    real_adapter = ChromiumMultiPublishAdapter(db_engine, app_paths)
    def browser_forbidden(*args, **kwargs):
        raise AssertionError('A schedule preflight must not access a browser')
    monkeypatch.setattr(real_adapter, '_open', browser_forbidden)
    api = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': real_adapter})
    try:
        params = copy.deepcopy(daily[1])
        params['publishing'].pop('day_offset')
        params['publishing'].update(start_date='2026-10-28', time='14:00', days=1, per_day=1)
        result = call(api, 'mix.daily.submit', params, 'real-date-window')
        assert not result['ok'] and result['error']['code'] == 'platform_fields_invalid'
        assert api._mix.tasks() == [] and batches(db_engine) == []
    finally:
        api.close(2)


def test_daily_submit_only_authorizes_platform_schedule_while_old_immediate_draft_stays_readable(daily, db_engine):
    api, params, _, _ = daily
    params['publishing']['mode'] = 'publish_now'
    assert call(api, 'mix.daily.save', params, 'old-immediate-draft')['ok']
    assert call(api, 'mix.daily.read')['data']['settings'] == params
    response = call(api, 'mix.daily.submit', params, 'forbidden-daily-now')
    assert not response['ok'] and response['error']['code'] == 'daily_schedule_only'
    assert '平台定时' in response['error']['message']
    assert api._mix.tasks() == [] and batches(db_engine) == []


@pytest.mark.parametrize('start', [{'day_offset': 0}, {'start_date': '2026-10-14'}, {'start_date': '2026-10-15'}, {'start_date': '2026-10-29'}])
def test_daily_new_submission_rejects_starts_outside_shanghai_allowed_window(daily, db_engine, monkeypatch, start):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 5, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing'].update(start)
    assert call(api, 'mix.daily.save', params, 'old-start-draft')['ok']
    response = call(api, 'mix.daily.submit', params, 'forbidden-daily-start')
    assert not response['ok'] and response['error']['code'] == 'daily_start_out_of_range'
    assert '明天' in response['error']['message']
    assert api._mix.tasks() == [] and batches(db_engine) == []
    assert call(api, 'mix.daily.read')['data']['settings'] == params


def test_daily_tomorrow_is_resolved_at_new_click_across_shanghai_midnight_without_changing_receipt_replay(daily, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    now = [datetime(2026, 10, 15, 15, 59, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: now[0])
    first = submitted(daily, 'before-shanghai-midnight')
    assert first['resolved_start'] == '2026-10-16T12:00:00+08:00'
    params['publishing'].pop('day_offset')
    params['publishing']['start_date'] = '2026-10-16'
    explicit = submitted(daily, 'old-explicit-tomorrow')
    now[0] = datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc)
    assert submitted(daily, 'old-explicit-tomorrow') == explicit
    rejected = call(api, 'mix.daily.submit', params, 'new-explicit-after-midnight')
    assert not rejected['ok'] and rejected['error']['code'] == 'daily_start_out_of_range'
    params['publishing'].pop('start_date')
    params['publishing']['day_offset'] = 1
    assert submitted(daily, 'new-relative-after-midnight')['resolved_start'] == '2026-10-17T12:00:00+08:00'


def test_daily_restrictions_do_not_change_multi_publish_now_authorization(daily, db_engine, tmp_path):
    api, params, _, _ = daily
    path = tmp_path / 'ordinary-now.mp4'
    path.write_bytes(b'ordinary multi-platform now fixture')
    video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title='原多平台立即发布')
    prepared = call(api, 'multi_publish.prepare', {'video_ids': [video.id], 'targets': params['publishing']['targets'],
                    'common_fields': {}, 'execution_mode': 'publish_now'}, 'ordinary-now-prepare')
    assert prepared['ok'], prepared
    saved = call(api, 'multi_publish.confirm', {'prepared_id': prepared['data']['prepared_id'], 'intent_id': 'ordinary-now-prepare',
                 'consent_partial': False}, 'ordinary-now-save')
    assert saved['ok'], saved
    pid = saved['data']['id']
    ids = [item['id'] for item in api._multi_publish.detail(pid)['deliveries']]
    check = call(api, 'multi_publish.execution.check', {'id': pid, 'delivery_ids': ids}, 'ordinary-now-check')['data']
    started = call(api, 'multi_publish.execution.start', {'id': pid, 'delivery_ids': ids, 'digest': check['digest'],
                   'consent': True, 'consent_partial': False}, 'ordinary-now-start')
    assert started['ok'], started
    assert api._multi_publish.coordinator.run_once(pid, ids) == 'published'
    assert api._multi_publish.detail(pid)['deliveries'][0]['state'] == 'published'


@pytest.mark.parametrize(('start_date', 'days', 'per_day'), [('2026-10-16', 3, 2), ('2026-10-17', 3, 2), ('2026-10-28', 2, 1)])
def test_daily_explicit_start_accepts_shanghai_plus_one_through_plus_thirteen_inclusively(daily, monkeypatch, start_date, days, per_day):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 5, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing'].update(start_date=start_date, days=days, per_day=per_day)
    params['mix']['quantity'] = 40
    task = submitted(daily)
    assert task['quantity'] == days * per_day and task['resolved_start'] == start_date + 'T12:00:00+08:00'
    frozen = api._mix.queue.get(task['id']).snapshot['daily_publish']
    first = datetime.fromisoformat(start_date + 'T12:00:00+08:00')
    assert frozen['slots'] == [(first + timedelta(days=day, hours=within_day * 3)).isoformat() for day in range(days) for within_day in range(per_day)]
    assert call(api, 'mix.daily.read')['data']['settings'] == params


@pytest.mark.parametrize('start_date', ['2026-10-14', '2026-10-15', '2026-10-29'])
def test_daily_explicit_start_rejects_past_today_and_plus_fourteen_without_rewriting_draft(daily, db_engine, monkeypatch, start_date):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 5, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing']['start_date'] = start_date
    assert call(api, 'mix.daily.save', params, 'range-draft')['ok']
    response = call(api, 'mix.daily.submit', params, 'range-click')
    assert not response['ok'] and response['error']['code'] == 'daily_start_out_of_range'
    assert '明天' in response['error']['message'] and '13天' in response['error']['message']
    assert call(api, 'mix.daily.read')['data']['settings'] == params
    assert api._mix.tasks() == [] and batches(db_engine) == []


def test_daily_start_upper_boundary_is_rechecked_on_a_new_request_after_shanghai_midnight(daily, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    now = [datetime(2026, 10, 15, 15, 59, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: now[0])
    params['publishing'].pop('day_offset')
    params['publishing']['start_date'] = '2026-10-28'
    accepted = submitted(daily, 'old-upper-boundary')
    params['publishing']['start_date'] = '2026-10-29'
    rejected = call(api, 'mix.daily.submit', params, 'before-midnight-plus-fourteen')
    assert not rejected['ok'] and rejected['error']['code'] == 'daily_start_out_of_range'
    now[0] = datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc)
    assert submitted(daily, 'after-midnight-plus-thirteen')['resolved_start'] == '2026-10-29T12:00:00+08:00'
    params['publishing']['start_date'] = '2026-10-28'
    assert submitted(daily, 'old-upper-boundary') == accepted


@pytest.mark.parametrize('daily_rule', [True, False], ids=['per-day', 'legacy-continuous'])
@pytest.mark.parametrize('excess', [timedelta(0), timedelta(seconds=1), timedelta(minutes=1), timedelta(microseconds=1)],
                         ids=['exact-limit', 'one-second-over', 'one-minute-over', 'one-microsecond-over'])
def test_daily_final_slots_obey_exact_fourteen_day_limit_before_creating_any_task(daily, db_engine, monkeypatch, daily_rule, excess):
    api, params, renderer, _ = daily
    from yijianjifa.app_services import daily_mixing
    reference = datetime(2026, 10, 15, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: reference - excess)
    if daily_rule:
        params['publishing'].pop('day_offset')
        params['publishing'].update(start_date='2026-10-28', days=2, per_day=2, interval_hours=1)
    else:
        params['mix']['quantity'] = 3
        params['publishing']['interval_hours'] = 156.5
    response = call(api, 'mix.daily.submit', params, 'fourteen-day-intent')
    if excess:
        assert not response['ok'] and response['error']['code'] == 'daily_end_out_of_range'
        assert response['error']['message'] == '最后一条超过14天，请减少发布天数、每天条数或缩短间隔。'
        assert api._mix.tasks() == [] and batches(db_engine) == [] and renderer.index == 0
        assert call(api, 'mix.daily.read')['data']['settings'] is None
    else:
        assert response['ok'], response
        frozen = api._mix.queue.get(response['data']['id']).snapshot['daily_publish']
        assert frozen['slots'][-1] == '2026-10-29T13:00:00+08:00'
        assert len(frozen['slots']) == (4 if daily_rule else 3)
        monkeypatch.setattr(daily_mixing, 'utcnow', lambda: reference - timedelta(seconds=1))
        assert call(api, 'mix.daily.submit', params, 'fourteen-day-intent') == response
        assert len(api._mix.tasks()) == 1


def test_daily_later_start_cannot_push_the_final_day_past_fourteen_days(daily, db_engine, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 4, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing'].update(start_date='2026-10-17', days=13, per_day=2)
    response = call(api, 'mix.daily.submit', params, 'later-start-final-window')
    assert not response['ok'] and response['error']['code'] == 'daily_end_out_of_range'
    assert api._mix.tasks() == [] and batches(db_engine) == []


def test_saving_a_draft_beyond_fourteen_days_does_not_create_work_or_rewrite_settings(daily, db_engine, monkeypatch):
    api, params, _, _ = daily
    from yijianjifa.app_services import daily_mixing
    monkeypatch.setattr(daily_mixing, 'utcnow', lambda: datetime(2026, 10, 15, 4, tzinfo=timezone.utc))
    params['publishing'].pop('day_offset')
    params['publishing'].update(start_date='2026-10-17', days=13, per_day=2)
    params['mix']['quantity'] = 40
    assert call(api, 'mix.daily.save', params, 'late-end-draft')['ok']
    response = call(api, 'mix.daily.submit', params, 'late-end-draft-submit')
    assert not response['ok'] and response['error']['code'] == 'daily_end_out_of_range'
    assert call(api, 'mix.daily.read')['data']['settings'] == params
    assert api._mix.tasks() == [] and batches(db_engine) == []
