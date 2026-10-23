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
