"""Finite failure evidence survives the DB boundary without platform IO."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.app_services.multi_publish_dto import detail
from yijianjifa.features.accounts.service import AccountService, AccountIdentityMismatch
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation, PublishCoordinator, outcome_evidence
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


SECRET = 'https://example.invalid/?access_token=must-not-persist Cookie: private-session'


class OfflineAdapter:
    platform_id = 'douyin'
    verified_capabilities = {'ordinary', 'publish_now', 'platform_schedule', 'result_query'}

    def validate_target(self, target, payload, execution):
        return AdapterValidation(payload['platform_user_id'], True)

    def submit(self, payload, *, context, before_final):
        before_final()
        return AdapterOutcome('published')

    def reconcile(self, payload, *, context):
        return AdapterOutcome('result_unknown')


@pytest.fixture
def frozen(db_engine, app_paths, tmp_path):
    account = AccountService(db_engine).create('douyin', 'offline-uid', '合成账号')
    path = tmp_path / 'owned.mp4'
    path.write_bytes(b'offline owned media')
    video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title='离线测试')
    adapter = OfflineAdapter()
    service = MultiPublishService(db_engine, app_paths, catalog=PlatformCatalog(transports={'douyin': adapter}))
    prepared = service.prepare(request_id='diagnostic-plan', video_ids=[video.id],
        targets=[{'account_id': account.id, 'platform': 'douyin'}], common_fields={}, execution_mode='publish_now')
    receipt = service.save(prepared['prepared_id'], request_id='diagnostic-plan')
    return service, adapter, receipt['id']


@pytest.mark.parametrize('operation,error,stage,category', [
    ('bind_delivery', ModuleNotFoundError(SECRET), 'adapter_setup', 'dependency_missing'),
    ('validate_target', ImportError(SECRET), 'account_validation', 'dependency_missing'),
    ('validate_target', TimeoutError(SECRET), 'account_validation', 'timeout'),
    ('validate_target', ValueError('account busy'), 'account_validation', 'account_busy'),
    ('validate_target', ValueError('identity-bound session unavailable'), 'account_validation', 'session_unavailable'),
    ('validate_target', ValueError('Playwright requires its original host worker thread'), 'account_validation', 'worker_thread_mismatch'),
    ('validate_target', RuntimeError('Execution context was destroyed: '+SECRET), 'account_validation', 'execution_failed'),
    ('validate_target', AccountIdentityMismatch(SECRET), 'account_validation', 'identity_mismatch'),
    ('validate_target', FileNotFoundError(SECRET), 'account_validation', 'resource_unavailable'),
    ('submit', RuntimeError(SECRET), 'page_preparation', 'execution_failed'),
])
def test_pre_final_failures_keep_finite_stage_and_category_without_retry(frozen, db_engine, monkeypatch, operation, error, stage, category):
    service, adapter, task_id = frozen
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(adapter, operation, fail, raising=False)
    coordinator = PublishCoordinator(service)
    assert coordinator.run_once(task_id) == 'pre_submit_failed'
    public = detail(service, task_id, coordinator=coordinator)['deliveries'][0]
    assert public['error_stage'] == stage and public['error_category'] == category
    assert public['reason_message'] and '尚未提交' in public['reason_message']
    assert not public['final_started'] and not public['can_submit']
    assert coordinator.run_once(task_id) is None
    with session_scope(db_engine) as db:
        attempt = db.scalar(select(DeliveryAttemptRow))
        evidence = json.loads(attempt.result_json)
        assert evidence['error_stage'] == stage and evidence['error_category'] == category
        assert attempt.final_json is None
        assert 'must-not-persist' not in attempt.result_json
    assert 'must-not-persist' not in json.dumps(public)


def test_post_final_exception_stays_unknown_and_reports_submission_result(frozen, db_engine, monkeypatch):
    service, adapter, task_id = frozen
    def fail_after_final(payload, *, context, before_final):
        before_final()
        raise RuntimeError(SECRET)
    monkeypatch.setattr(adapter, 'submit', fail_after_final)
    coordinator = PublishCoordinator(service)
    assert coordinator.run_once(task_id) == 'result_unknown'
    public = detail(service, task_id, coordinator=coordinator)['deliveries'][0]
    assert public['final_started'] and public['error_stage'] == 'submission_result'
    assert '勿重复' in public['reason_message'] and '尚未提交' not in public['reason_message']
    assert coordinator.run_once(task_id) is None
    with session_scope(db_engine) as db:
        attempt = db.scalar(select(DeliveryAttemptRow))
        assert attempt.final_json is not None and 'must-not-persist' not in attempt.result_json


def test_existing_schedule_reason_survives_to_public_task(frozen,monkeypatch):
    service,adapter,task_id=frozen
    monkeypatch.setattr(adapter,'submit',lambda *args,**kwargs:AdapterOutcome(
        'pre_submit_failed',error_code='adapter_failed',error_stage='page_preparation',error_category='existing_scheduled_work'))
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(task_id)=='pre_submit_failed'
    public=detail(service,task_id,coordinator=coordinator)['deliveries'][0]
    assert public['error_category']=='existing_scheduled_work'
    assert '定时时间相同' in public['reason_message']
    assert '其他已接收的平台无需重发' in public['reason_message']
    assert not public['final_started']
    assert coordinator.run_once(task_id) is None


def test_douyin_daily_limit_reason_is_visible_without_losing_final_marker(frozen,db_engine,monkeypatch):
    service,adapter,task_id=frozen
    def reject(payload,*,context,before_final):
        before_final()
        return AdapterOutcome('platform_rejected',error_code='douyin_daily_post_limit')
    monkeypatch.setattr(adapter,'submit',reject)
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(task_id)=='platform_rejected'
    public=detail(service,task_id,coordinator=coordinator)['deliveries'][0]
    assert '今天投稿次数已达到上限，请明天再试' in public['reason_message']
    assert public['final_started'] and not public['can_submit']
    assert coordinator.run_once(task_id) is None


def test_platform_rejection_preserves_readable_reason_without_private_values(frozen,db_engine,monkeypatch):
    service,adapter,task_id=frozen
    def reject(payload,*,context,before_final):
        before_final()
        return AdapterOutcome('platform_rejected',error_code='platform_rejected',
            platform_message='今天投稿次数已达到上限，请明天再试。 Cookie: private-session')
    monkeypatch.setattr(adapter,'submit',reject)
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(task_id)=='platform_rejected'
    public=detail(service,task_id,coordinator=coordinator)['deliveries'][0]
    assert '今天投稿次数已达到上限' in public['reason_message']
    assert 'private-session' not in json.dumps(public)
    assert public['platform']=='douyin' and public['account_label']=='合成账号'
    assert not public['can_submit'] and coordinator.run_once(task_id) is None


@pytest.mark.parametrize('change,expected',[(None,'platform_accepted'),('digest','result_unknown'),('platform','result_unknown'),('no_final','result_unknown')])
def test_final_success_notice_is_bound_to_current_target(frozen,monkeypatch,change,expected):
    from yijianjifa.features.publish.deliveries import digest
    service,adapter,task_id=frozen
    def accept(payload,*,context,before_final):
        if change!='no_final':before_final()
        notice={'kind':'publish_success_toast','platform':'bilibili' if change=='platform' else payload['platform'],
            'request_digest':'0'*64 if change=='digest' else digest(payload)}
        return AdapterOutcome('platform_accepted',submission_notice=notice)
    monkeypatch.setattr(adapter,'submit',accept)
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(task_id)==expected
    public=detail(service,task_id,coordinator=coordinator)['deliveries'][0]
    assert public['state']==expected and not public['can_submit']
    assert coordinator.run_once(task_id) is None


def test_saved_success_notice_survives_recheck_without_opening_browser(frozen,db_engine,app_paths,monkeypatch):
    from yijianjifa.features.publish.deliveries import digest
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    service,adapter,task_id=frozen;submitted=[]
    def accept(payload,*,context,before_final):
        before_final();submitted.append(dict(payload))
        return AdapterOutcome('platform_accepted',submission_notice={
            'kind':'publish_success_toast','platform':'douyin','request_digest':digest(payload)})
    monkeypatch.setattr(adapter,'submit',accept)
    assert PublishCoordinator(service).run_once(task_id)=='platform_accepted'
    reopened=ChromiumMultiPublishAdapter(db_engine,app_paths)
    def forbidden(*args,**kwargs):raise AssertionError('known success does not need another browser')
    monkeypatch.setattr(reopened,'_open',forbidden)
    try:
        assert reopened.reconcile(submitted[0],context=None).state=='platform_accepted'
    finally:reopened.close()


def test_final_revalidation_failure_does_not_create_final_marker(frozen, db_engine, monkeypatch):
    service, adapter, task_id = frozen
    checks = 0
    def validate(target, payload, execution):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValueError(SECRET)
        return AdapterValidation(payload['platform_user_id'], True)
    monkeypatch.setattr(adapter, 'validate_target', validate)
    assert PublishCoordinator(service).run_once(task_id) == 'pre_submit_failed'
    public = detail(service, task_id)['deliveries'][0]
    assert public['error_stage'] == 'final_validation' and not public['final_started']


@pytest.mark.parametrize('diagnostic,stage,category',[
    ({'error_stage':'page_open','error_category':'profile_in_use'},'page_open','profile_in_use'),
    ({'error_stage':'session_restore','error_category':'session_unavailable'},'session_restore','session_unavailable'),
    ({'error_stage':'account_observation','error_category':'page_closed'},'account_observation','page_closed'),
    ({'error_stage':SECRET,'error_category':'page_closed'},'account_validation','execution_failed'),
    ({'error_stage':[],'error_category':{}},'account_validation','execution_failed'),
    ({'error_stage':'page_open','error_category':'profile_in_use','raw':SECRET},'account_validation','execution_failed'),
])
def test_finite_adapter_account_diagnostic_survives_public_boundary(frozen,db_engine,monkeypatch,diagnostic,stage,category):
    service,adapter,task_id=frozen
    def fail(*args):raise RuntimeError(SECRET)
    monkeypatch.setattr(adapter,'validate_target',fail)
    monkeypatch.setattr(adapter,'failure_diagnostic',lambda error:diagnostic,raising=False)
    assert PublishCoordinator(service).run_once(task_id)=='pre_submit_failed'
    public=detail(service,task_id)['deliveries'][0]
    assert (public['error_stage'],public['error_category'])==(stage,category)
    assert '尚未提交' in public['reason_message'] and 'must-not-persist' not in json.dumps(public)


@pytest.mark.parametrize('diagnostic,stage,category',[
    ({'error_stage':'upload_preparation','error_category':'existing_draft'},'upload_preparation','existing_draft'),
    ({'error_stage':'form_preparation','error_category':'javascript_read_failed'},'form_preparation','javascript_read_failed'),
    ({'error_stage':'schedule_preparation','error_category':'form_changed'},'schedule_preparation','form_changed'),
    ({'error_stage':SECRET,'error_category':'form_changed'},'page_preparation','execution_failed'),
    ({'error_stage':'form_preparation','error_category':SECRET},'page_preparation','execution_failed'),
    ({'error_stage':'form_preparation','error_category':'form_changed','raw':SECRET},'page_preparation','execution_failed'),
])
def test_preparation_diagnostic_reports_specific_safe_stage_without_automatic_retry(frozen,monkeypatch,diagnostic,stage,category):
    service,adapter,task_id=frozen
    def fail(*args,**kwargs):raise RuntimeError(SECRET)
    monkeypatch.setattr(adapter,'submit',fail)
    monkeypatch.setattr(adapter,'failure_diagnostic',lambda error:diagnostic,raising=False)
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(task_id)=='pre_submit_failed'
    child=detail(service,task_id,coordinator=coordinator)['deliveries'][0]
    assert (child['error_stage'],child['error_category'])==(stage,category)
    assert '尚未提交' in child['reason_message'] and 'must-not-persist' not in json.dumps(child)
    assert not child['worker_running'] and not child['final_started']
    assert coordinator.run_once(task_id) is None


def test_worker_running_reports_actual_owner_even_when_final_mark_is_already_unknown(frozen):
    service,_,task_id=frozen
    child=detail(service,task_id)['deliveries'][0]
    assert child['worker_running'] is False
    claim=service.deliveries.claim(child['id'])
    assert detail(service,task_id)['deliveries'][0]['worker_running'] is True
    service.deliveries.mark_before_final(child['id'],claim['digest'],owner_token=claim['owner_token'])
    child=detail(service,task_id)['deliveries'][0]
    assert child['state']=='result_unknown' and child['final_started'] is True and child['worker_running'] is True
    service.deliveries.record(child['id'],claim['digest'],owner_token=claim['owner_token'],
        outcome=AdapterOutcome('result_unknown',error_code='result_unknown'))
    assert detail(service,task_id)['deliveries'][0]['worker_running'] is True
    service.deliveries.workers.finished(claim['owner_hash'])
    child=detail(service,task_id)['deliveries'][0]
    assert child['worker_running'] is False and child['final_started'] is True


def test_finished_unknown_result_keeps_owner_proof_but_is_not_processing(frozen):
    service,_,task_id=frozen
    child=detail(service,task_id)['deliveries'][0]
    claim=service.deliveries.claim(child['id'])
    service.deliveries.mark_before_final(child['id'],claim['digest'],owner_token=claim['owner_token'])
    assert detail(service,task_id)['deliveries'][0]['execution_active'] is True
    service.deliveries.record(child['id'],claim['digest'],owner_token=claim['owner_token'],
        outcome=AdapterOutcome('result_unknown',error_code='timeout'))
    projected=detail(service,task_id)['deliveries'][0]
    assert projected['worker_running'] is True
    assert projected['execution_active'] is False
    assert projected['submission_status']=='uncertain' and 'start' not in projected['allowed_actions']


@pytest.mark.parametrize('login_state',['valid','missing'])
def test_read_only_copy_reports_same_account_blocker_before_unknown_catalog(frozen,db_engine,app_paths,tmp_path,login_state):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    service,_,task_id=frozen
    original=detail(service,task_id)['deliveries'][0]
    claim=service.deliveries.claim(original['id'])
    service.deliveries.mark_before_final(original['id'],claim['digest'],owner_token=claim['owner_token'])
    service.deliveries.record(original['id'],claim['digest'],owner_token=claim['owner_token'],
        outcome=AdapterOutcome('result_unknown',error_code='timeout'))
    path=tmp_path/'different.mp4';path.write_bytes(b'another local-only fixture')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='等待的合成作品')
    prepared=service.prepare(request_id='blocked-plan',video_ids=[video.id],
        targets=[{'account_id':original['account_id'],'platform':'douyin'}],common_fields={},execution_mode='publish_now')
    child_task=service.save(prepared['prepared_id'],request_id='blocked-plan')['id']
    from yijianjifa.features.accounts.service import AccountRow
    from yijianjifa.features.tasks.service import TaskItemRow
    with session_scope(db_engine) as session:
        session.get(AccountRow,original['account_id']).login_state=login_state
        attempt=session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==original['id']))
        protected=(attempt.owner_hash,attempt.final_json,attempt.result_json)
    copied=TaskCenterAccess(db_engine).detail(child_task)
    public=copied['deliveries'][0]
    assert public['readiness_reason']=='account_result_unresolved'
    assert public['blocked_by']=={'delivery_id':original['id'],'state':'result_unknown'}
    assert '暂缓' in public['reason_message'] and '未接入' not in public['reason_message']
    assert '未接入' not in TaskCenterAccess(db_engine).execution_log_text(child_task)
    assert public['can_submit'] is False and 'start' not in public['allowed_actions']
    assert copied['task']['status_label']=='等待核对发布结果，1项暂缓'
    with session_scope(db_engine) as session:
        attempt=session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==original['id']))
        assert (attempt.owner_hash,attempt.final_json,attempt.result_json)==protected
        assert session.get(TaskItemRow,public['item_id']).state=='waiting'
    with pytest.raises(ValueError,match='reconciliation'):
        service.deliveries.claim(original['id'])


def test_read_only_waiting_projection_does_not_invent_adapter_failure(frozen,db_engine):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    _,_,task_id=frozen
    public=TaskCenterAccess(db_engine).detail(task_id)['deliveries'][0]
    assert public['state']=='waiting'
    assert public['readiness_reason']=='readiness_not_checked'
    assert '未接入' not in public['reason_message']
    assert 'start' not in public['allowed_actions']


def test_account_diagnostic_cannot_relabel_post_final_exception(frozen,monkeypatch):
    service,adapter,task_id=frozen
    def fail(payload,*,context,before_final):before_final();raise RuntimeError(SECRET)
    monkeypatch.setattr(adapter,'submit',fail)
    monkeypatch.setattr(adapter,'failure_diagnostic',lambda error:pytest.fail('not an account observation failure'),raising=False)
    assert PublishCoordinator(service).run_once(task_id)=='result_unknown'
    public=detail(service,task_id)['deliveries'][0]
    assert public['error_stage']=='submission_result' and '勿重复' in public['reason_message']


@pytest.mark.parametrize('after_final', [False, True])
def test_resume_diagnostic_is_consumed_only_before_final_and_never_replays(frozen, monkeypatch, after_final):
    service, adapter, task_id = frozen
    coordinator = PublishCoordinator(service)
    monkeypatch.setattr(adapter, 'submit', lambda *a, **k: AdapterOutcome('paused_auth', context={'handle': 'offline'}))
    monkeypatch.setattr(adapter, 'resume', lambda *a, **k: None, raising=False)
    assert coordinator.run_once(task_id) == 'paused_auth'
    observed = []
    def resume(payload, *, context, before_final):
        if after_final: before_final()
        raise RuntimeError(SECRET)
    def diagnostic(error):
        observed.append(True)
        return {'error_stage': 'verification_observation', 'error_category': 'page_closed'}
    monkeypatch.setattr(adapter, 'resume', resume, raising=False)
    monkeypatch.setattr(adapter, 'failure_diagnostic', diagnostic, raising=False)
    child = service.get_task(task_id)['deliveries'][0]
    assert coordinator.resume_verification(child['id']) == ('result_unknown' if after_final else 'pre_submit_failed')
    public = detail(service, task_id)['deliveries'][0]
    assert public['error_stage'] == ('submission_result' if after_final else 'verification_observation')
    assert public['final_started'] is after_final and observed == ([] if after_final else [True])
    assert 'must-not-persist' not in json.dumps(public)
    assert coordinator.run_once(task_id) is None


@pytest.mark.parametrize('value', [SECRET, [], {}])
def test_unrecognized_stored_diagnostics_are_not_public(frozen, db_engine, value):
    service, _, task_id = frozen
    with session_scope(db_engine) as db:
        attempt = db.scalar(select(DeliveryAttemptRow))
        attempt.result_json = json.dumps({'state': 'pre_submit_failed', 'error_code': 'adapter_failed',
            'error_stage': value, 'error_category': value, 'raw_exception': SECRET})
    public = detail(service, task_id)['deliveries'][0]
    assert 'error_stage' not in public and 'error_category' not in public
    assert '客户端' in public['reason_message']
    assert 'must-not-persist' not in json.dumps(public)


def test_prepare_projects_readable_required_fields_reason_without_creating_task(db_engine, app_paths, tmp_path):
    adapter = OfflineAdapter();adapter.platform_id = 'bilibili'
    api = DesktopApi(db_engine, app_paths, login_provider=SimpleNamespace(publish_adapters={'bilibili': adapter}))
    account = AccountService(db_engine).create('bilibili', 'offline-bili', '合成B站')
    path = tmp_path / 'owned.mp4';path.write_bytes(b'owned offline media')
    video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title='离线测试')
    try:
        result = api.dispatch(json.dumps({'v': 1, 'request_id': 'prepared-only', 'operation': 'multi_publish.prepare',
            'params': {'video_ids': [video.id], 'targets': [{'account_id': account.id, 'platform': 'bilibili'}],
                'common_fields': {}, 'execution_mode': 'publish_now'}}), trusted_origin=True)
        assert result['ok'], result
        target = result['data']['targets'][0]
        assert target['hold_reason'] == 'category_required' and '分区' in target['reason_message']
        assert result['data']['summary']['ready'] == 0
        assert not {'payload', 'snapshot_json', 'session_fingerprint', 'video_path'} & set(target)
        with session_scope(db_engine) as db:
            assert db.scalar(select(TaskCenterRow)) is None
    finally:
        api.close(5)


def test_success_evidence_does_not_gain_diagnostic_fields():
    evidence = outcome_evidence(AdapterOutcome('published'), 'digest')
    assert set(evidence) == {'state', 'request_digest', 'scheduled_at', 'receipt_reference', 'error_code'}


@pytest.mark.parametrize('change', [
    {'error_stage': SECRET}, {'error_stage': []}, {'error_category': SECRET}, {'error_category': {}},
    {'error_code': 'timeout'},
])
def test_failure_evidence_rejects_unbounded_diagnostics(change):
    values = {'error_code': 'adapter_failed', 'error_stage': 'account_validation', 'error_category': 'dependency_missing', **change}
    with pytest.raises(ValueError, match='normalized failure diagnostic'):
        outcome_evidence(AdapterOutcome('pre_submit_failed', **values), 'digest')


def test_unknown_stored_error_code_cannot_leak_through_public_projection(frozen, db_engine):
    service, _, task_id = frozen
    with session_scope(db_engine) as db:
        attempt = db.scalar(select(DeliveryAttemptRow))
        attempt.result_json = json.dumps({'state': 'pre_submit_failed', 'error_code': SECRET,
            'error_stage': 'account_validation', 'error_category': 'dependency_missing'})
    public = detail(service, task_id)['deliveries'][0]
    assert public['error_code'] is None and 'error_stage' not in public
    assert 'must-not-persist' not in json.dumps(public)


@pytest.mark.parametrize('state', ['published', 'platform_scheduled', 'platform_accepted'])
def test_success_outcomes_cannot_attach_failure_diagnostics(state):
    with pytest.raises(ValueError, match='normalized failure diagnostic'):
        outcome_evidence(AdapterOutcome(state, error_code='adapter_failed', error_stage='account_validation',
            error_category='dependency_missing'), 'digest')


def test_transient_account_lease_waits_then_submits_once(frozen,db_engine,monkeypatch):
    from yijianjifa.features.publish.account_wait import AccountBusy
    service,adapter,task_id=frozen
    original=adapter.validate_target
    monkeypatch.setattr(adapter,'validate_target',lambda *a,**k: (_ for _ in ()).throw(AccountBusy()))
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(task_id) is None
    child=detail(service,task_id,coordinator=coordinator)['deliveries'][0]
    assert child['state']=='waiting' and not child['final_started']
    with session_scope(db_engine) as s:
        attempt=s.scalar(select(DeliveryAttemptRow))
        assert attempt.owner_hash is None and attempt.result_json is None and attempt.final_json is None
    monkeypatch.setattr(adapter,'validate_target',original)
    assert coordinator.run_once(task_id)=='published'
    assert coordinator.run_once(task_id) is None


def test_busy_error_after_final_is_never_automatically_retried(frozen,monkeypatch):
    from yijianjifa.features.publish.account_wait import AccountBusy
    service,adapter,task_id=frozen
    def submit(payload,*,context,before_final):before_final();raise AccountBusy()
    monkeypatch.setattr(adapter,'submit',submit)
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(task_id)=='result_unknown'
    assert coordinator.run_once(task_id) is None


def test_busy_account_does_not_block_another_account_or_claim_its_task(frozen,db_engine,monkeypatch):
    from yijianjifa.webui.chromium_publish import account_lock
    service,adapter,task_id=frozen
    child=service.get_task(task_id)['deliveries'][0]
    other=AccountService(db_engine).create('douyin','second-offline','另一个账号')
    prepared=service.prepare(request_id='other-account',video_ids=[child['video_id']],targets=[{'account_id':other.id,'platform':'douyin'}],common_fields={},execution_mode='publish_now')
    other_task=service.save(prepared['prepared_id'],request_id='other-account')['id']
    lock=account_lock(child['account_id']);lock.acquire()
    monkeypatch.setattr(adapter,'account_available',lambda target:not account_lock(target['account_id']).locked(),raising=False)
    coordinator=PublishCoordinator(service)
    try:
        assert coordinator.run_once(task_id) is None
        with session_scope(db_engine) as s:
            attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==child['id']))
            assert attempt.owner_hash is None and attempt.final_json is None
        assert coordinator.run_once(other_task)=='published'
        public=detail(service,task_id,coordinator=coordinator)['deliveries'][0]
        assert public['state']=='waiting' and public['execution_log'][0]['step']=='account.wait'
    finally:lock.release()
    assert coordinator.run_once(task_id)=='published'
    assert coordinator.run_once(task_id) is None


def _saved_identity_failure(frozen, monkeypatch, details, *, final=False, older_details=None):
    """通过真实协调器和执行日志保存离线身份读取失败。"""
    from yijianjifa.features.publish.execution_log import append_event
    service, adapter, task_id = frozen
    delivery_id = service.get_task(task_id)['deliveries'][0]['id']
    def fail(payload, *, context, before_final):
        if final:
            before_final()
        if older_details:
            append_event(service.engine, delivery_id, 1, 'douyin.identity', {
                'state': 'failed', 'phase': 'prepare', **older_details})
        append_event(service.engine, delivery_id, 1, 'douyin.identity', {
            'state': 'failed', 'phase': 'result' if final else 'prepare',
            # 页面事件不能代替真实的持久提交标记。
            'final_started': not final, **details})
        append_event(service.engine, delivery_id, 1, 'execution.error', {
            'stage': 'account_observation', 'exception_type': 'Error'})
        raise RuntimeError(SECRET)
    monkeypatch.setattr(adapter, 'submit', fail)
    monkeypatch.setattr(adapter, 'failure_diagnostic', lambda error: {
        'error_stage': 'account_observation', 'error_category': 'execution_failed'}, raising=False)
    coordinator = PublishCoordinator(service)
    assert coordinator.run_once(task_id) == ('result_unknown' if final else 'pre_submit_failed')
    assert coordinator.run_once(task_id) is None
    return detail(service, task_id, coordinator=coordinator)['deliveries'][0], task_id


@pytest.mark.parametrize('final', [False, True])
def test_identity_http_failure_summary_survives_real_detail_and_copy_without_guessing_submission(
        frozen, db_engine, monkeypatch, final):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    checks = {'error_kind': 'http_error', 'endpoint': 'profile_user_info',
              'request_index': 2, 'http_status': 503, 'content_kind': 'html',
              'read_operation': 'fetch', 'request_elapsed_ms': 328}
    child, task_id = _saved_identity_failure(frozen, monkeypatch, checks, final=final)
    summary = child.get('diagnostic_summary', '')
    assert '第2/3次' in summary and '账号资料接口' in summary and 'HTTP 503' in summary
    assert '328 毫秒' in summary
    assert summary in child['reason_message']
    assert ('已进入提交阶段' if final else '尚未提交') in child['reason_message']
    assert ('尚未提交' not in child['reason_message']) if final else ('已进入提交阶段' not in child['reason_message'])
    copied = TaskCenterAccess(db_engine).execution_log_text(task_id)
    assert '诊断摘要：' + summary in copied
    assert 'must-not-persist' not in json.dumps(child) + copied


@pytest.mark.parametrize('kind, expected', [
    ('fetch_error', '请求失败'), ('json_error', '响应解析失败'),
    ('body_read_error', '响应内容读取失败'), ('timeout', '请求超时'),
])
def test_identity_failure_summary_names_only_the_recorded_error_kind(frozen, monkeypatch, kind, expected):
    child, _ = _saved_identity_failure(frozen, monkeypatch, {
        'error_kind': kind, 'endpoint': 'pc_user_info_after', 'request_index': 3,
        'read_operation': 'json' if kind in {'json_error', 'body_read_error'} else 'fetch',
        'request_elapsed_ms': 17})
    summary = child.get('diagnostic_summary', '')
    assert expected in summary and '第3/3次' in summary and '身份复核接口' in summary
    assert '登录过期' not in summary and '限流' not in summary


def test_identity_failure_summary_uses_latest_valid_event_in_current_attempt(frozen, monkeypatch):
    older = {'error_kind': 'http_error', 'endpoint': 'pc_user_info_before',
             'request_index': 1, 'http_status': 403, 'request_elapsed_ms': 99, 'read_operation': 'fetch'}
    latest = {'error_kind': 'http_error', 'endpoint': 'profile_user_info',
              'request_index': 2, 'http_status': 429, 'request_elapsed_ms': 12, 'read_operation': 'fetch'}
    child, _ = _saved_identity_failure(frozen, monkeypatch, latest, older_details=older)
    assert 'HTTP 429' in child.get('diagnostic_summary', '')
    assert 'HTTP 403' not in child['diagnostic_summary']


@pytest.mark.parametrize('checks', [
    {'error_kind': 'SecretLettersOnly', 'http_status': 503},
    {'error_kind': 'http_error', 'endpoint': 'profile_user_info', 'request_index': 2, 'http_status': True},
    {'error_kind': 'http_error', 'endpoint': 'profile_user_info', 'request_index': 1, 'http_status': 503},
    {'error_kind': 'http_error', 'endpoint': 'profile_user_info', 'request_index': 2, 'http_status': 999},
    {'error_kind': 'fetch_error', 'endpoint': 'https://example.invalid/private', 'request_index': 2},
    {'error_kind': 'json_error', 'request_elapsed_ms': -1},
])
def test_malformed_or_unknown_identity_details_keep_generic_reason(frozen, monkeypatch, checks):
    complete = {'error_kind': 'http_error', 'endpoint': 'profile_user_info', 'request_index': 2,
                'read_operation': 'fetch', 'request_elapsed_ms': 7}
    child, _ = _saved_identity_failure(frozen, monkeypatch, {**complete, **checks})
    assert not child.get('diagnostic_summary')
    assert '客户端处理平台页面时遇到异常' in child['reason_message']
    assert 'SecretLettersOnly' not in child['reason_message']


def test_legacy_generic_error_is_not_reconstructed_as_an_http_or_login_failure(frozen, monkeypatch):
    child, _ = _saved_identity_failure(frozen, monkeypatch, {})
    assert not child.get('diagnostic_summary')
    assert 'HTTP' not in child['reason_message'] and '登录过期' not in child['reason_message']


@pytest.mark.parametrize('checks', [
    {'error_kind': 'SecretLettersOnly'},
    {'error_kind': 'http_error', 'endpoint': 'SecretLettersOnly', 'request_index': 2,
     'read_operation': 'fetch', 'request_elapsed_ms': 8, 'http_status': 503},
    {'error_kind': 'unknown', 'source_module': 'SecretLettersOnly', 'source_function': 'SecretFunction', 'source_line': 40},
])
def test_new_diagnostic_fields_cannot_leak_alphabetic_secrets_through_detail_or_copy(
        frozen, db_engine, monkeypatch, checks):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    child, task_id = _saved_identity_failure(frozen, monkeypatch, checks)
    copied = TaskCenterAccess(db_engine).execution_log_text(task_id)
    assert 'SecretLettersOnly' not in json.dumps(child) + copied
    assert 'SecretFunction' not in json.dumps(child) + copied
    # 公开投影收口不能擦除数据库中的原始证据。
    from yijianjifa.features.publish.execution_log import log_key
    with session_scope(db_engine) as db:
        saved = db.get(Setting, log_key(child['id'], 1))
        assert 'SecretLettersOnly' in saved.value_json


def test_generic_classified_identity_error_is_readable_without_inventing_request_details(frozen, monkeypatch):
    child, _ = _saved_identity_failure(frozen, monkeypatch, {'error_kind': 'page_context_unavailable'})
    summary = child.get('diagnostic_summary', '')
    assert '页面执行环境已失效' in summary
    assert 'HTTP' not in summary and '/3次' not in summary
    assert any(e['checks'].get('error_kind') == 'page_context_unavailable' for e in child['execution_log'])


def test_known_source_location_is_copyable_but_unrelated_event_checks_stay_unchanged(frozen, db_engine, monkeypatch):
    from yijianjifa.features.publish.execution_log import append_event
    child, task_id = _saved_identity_failure(frozen, monkeypatch, {
        'error_kind': 'unknown', 'source_module': 'chromium_multi_publish',
        'source_function': 'read', 'source_line': 144})
    checks = next(e['checks'] for e in child['execution_log'] if e['step'] == 'douyin.identity')
    assert checks['source_module'] == 'chromium_multi_publish' and checks['source_line'] == 144
    append_event(db_engine, child['id'], 1, 'douyin.form_readback', {'ready': False, 'topics_confirmed': 2})
    public = detail(frozen[0], task_id)['deliveries'][0]
    assert public['execution_log'][-1]['checks'] == {'ready': False, 'topics_confirmed': 2}


@pytest.mark.parametrize('extra', [
    {'private': 'SecretLettersOnly'}, {'phase': 'SecretLettersOnly'},
    {'error_category': 'SecretLettersOnly'}, {'exception_type': 'SecretLettersOnly'},
    {'stage': 'SecretLettersOnly'}, {'elapsed_ms': 'SecretLettersOnly'},
    {'final_started': 'SecretLettersOnly'}, {'log_schema': 'SecretLettersOnly'},
])
def test_new_identity_diagnostic_context_rejects_extra_or_secret_values_without_erasing_db(
        frozen, db_engine, monkeypatch, extra):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.features.publish.execution_log import log_key
    checks = {'error_kind': 'http_error', 'endpoint': 'profile_user_info', 'request_index': 2,
              'http_status': 503, 'read_operation': 'fetch', 'request_elapsed_ms': 328, **extra}
    child, task_id = _saved_identity_failure(frozen, monkeypatch, checks)
    copied = TaskCenterAccess(db_engine).execution_log_text(task_id)
    assert 'HTTP 503' in child.get('diagnostic_summary', '')
    assert 'SecretLettersOnly' not in json.dumps(child) + copied
    event = next(e for e in child['execution_log'] if e['step'] == 'douyin.identity')
    if 'private' in extra:assert 'private' not in event['checks']
    if 'exception_type' in extra:assert event['checks']['exception_type'] == 'UnknownError'
    with session_scope(db_engine) as db:
        assert 'SecretLettersOnly' in db.get(Setting, log_key(child['id'], 1)).value_json


def test_new_execution_error_uses_fixed_context_while_legacy_and_unrelated_events_stay_compatible():
    from yijianjifa.app_services.multi_publish_dto import safe_identity_execution_events
    raw = [
        {'step': 'execution.error', 'time': '2026-10-26T00:00:00Z', 'checks': {
            'stage': 'account_observation', 'error_kind': 'unknown',
            'exception_type': 'SecretLettersOnly', 'private': 'PrivateLettersOnly',
            'phase': 'prepare', 'final_started': False, 'log_schema': 1, 'elapsed_ms': 328}},
        {'step': 'execution.error', 'time': '2026-10-26T00:00:01Z', 'checks': {
            'stage': 'account_observation', 'exception_type': 'Error', 'legacy_counter': 2}},
        {'step': 'douyin.form_readback', 'time': '2026-10-26T00:00:02Z', 'checks': {
            'ready': False, 'topics_confirmed': 2, 'legacy_flag': True}},
    ]
    projected = safe_identity_execution_events(raw)
    assert projected[0]['checks'] == {'stage': 'account_observation', 'error_kind': 'unknown',
        'exception_type': 'UnknownError', 'phase': 'prepare', 'final_started': False,
        'log_schema': 1, 'elapsed_ms': 328}
    assert projected[1:] == raw[1:]
    assert raw[0]['checks']['private'] == 'PrivateLettersOnly'


def test_cover_unconfirmed_reason_survives_storage_without_final_or_retry(frozen,monkeypatch):
    service,adapter,task_id=frozen
    def unconfirmed(payload,*,context,before_final):
        return AdapterOutcome('pre_submit_failed',error_code='adapter_failed',error_stage='form_preparation',error_category='cover_unconfirmed')
    monkeypatch.setattr(adapter,'submit',unconfirmed)
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(task_id)=='pre_submit_failed'
    public=detail(service,task_id,coordinator=coordinator)['deliveries'][0]
    assert public['error_category']=='cover_unconfirmed'
    assert '封面确认后' in public['reason_message'] and '尚未提交' in public['reason_message']
    assert not public['final_started'] and not public['can_submit']
    assert coordinator.run_once(task_id) is None
