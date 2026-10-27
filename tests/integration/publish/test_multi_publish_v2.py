"""Offline V2 acceptance. All files, identities, DBs and transports are synthetic."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.features.publish.coordinator import PublishCoordinator, AdapterValidation, AdapterOutcome
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, DeliveryAttemptRow, PublishResourceRow, ResourceConsumerRow
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.history import PublishHistoryRepository, PublishHistoryRow, DuplicatePublishBlocked
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.storage.db import session_scope, create_engine_for, migrate_to_head, migrate_to_revision
from yijianjifa.storage.models import Setting

NOW = datetime.now(timezone.utc)


class FakeAdapter:
    verified_capabilities = frozenset({'ordinary', 'publish_now', 'platform_schedule', 'result_query'})

    def __init__(self, platform, *, outcome='published', observed_uid=None):
        self.platform_id = platform
        self.outcome = outcome
        self.observed_uid = observed_uid
        self.calls = []

    def validate_target(self, target, payload, execution):
        return AdapterValidation(self.observed_uid or target['platform_user_id'], True)

    def submit(self, payload, *, context, before_final):
        self.calls.append(('submit', payload['video_id']))
        if self.outcome == 'pre_submit_failed':
            return AdapterOutcome('pre_submit_failed')
        if self.outcome == 'park':
            return AdapterOutcome('paused_auth', context={'handle': 'synthetic'})
        before_final()
        if self.outcome == 'crash':
            raise SystemExit('synthetic crash after final')
        if self.outcome == 'post_final_park':
            return AdapterOutcome('paused_auth', context={'handle': 'synthetic'})
        return AdapterOutcome(self.outcome, scheduled_at=payload['scheduled_at'] if self.outcome == 'platform_scheduled' else None)

    def resume(self, payload, *, context, before_final):
        assert context == {'handle': 'synthetic'}
        self.calls.append(('resume', payload['video_id']))
        before_final()
        return AdapterOutcome('published')

    def reconcile(self, payload, *, context):
        self.calls.append(('reconcile', payload['video_id']))
        return AdapterOutcome('published')


@pytest.fixture
def setup_v2(db_engine, app_paths, tmp_path):
    path = tmp_path/'self-owned.mp4'
    path.write_bytes(b'synthetic-owned-short-media')
    video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title='自有测试')
    accounts = [AccountService(db_engine).create(p, 'synthetic-'+p, p) for p in ['douyin','kuaishou','channels','bilibili']]
    adapters = {a.platform: FakeAdapter(a.platform) for a in accounts}
    catalog = PlatformCatalog(transports=adapters)
    service = MultiPublishService(db_engine, app_paths, catalog=catalog)
    return service, video, accounts, adapters


def prepare_save(setup, request='r1', **kwargs):
    service, video, accounts, _ = setup
    prepared = service.prepare(request_id=request, video_ids=[video.id], targets=[{'account_id':a.id, 'platform':a.platform, 'fields':{'category_id':1,'copyright':'original'} if a.platform=='bilibili' else {}} for a in accounts], common_fields={'title':'自有测试'}, execution_mode=kwargs.pop('execution_mode','publish_now'), **kwargs)
    return service.save(prepared['prepared_id'], request_id=request)


def test_one_video_four_targets_share_one_owned_copy_and_exact_histories(setup_v2, db_engine):
    receipt = prepare_save(setup_v2)
    assert receipt['quantity'] == 4 and receipt['submitted'] is False
    with session_scope(db_engine) as s:
        deliveries = list(s.scalars(select(PublishDeliveryRow)))
        assert len(deliveries)==4 and len({d.item_id for d in deliveries})==4
        assert len({d.history_id for d in deliveries})==4
        assert len(list(s.scalars(select(PublishResourceRow))))==1
        assert len(list(s.scalars(select(ResourceConsumerRow))))==4
        assert s.get(PublishTaskRow, receipt['id']).platform=='mixed'
    assert setup_v2[1].effective_path.exists()


def test_request_id_idempotence_after_lost_response_and_conflicting_reuse(setup_v2, db_engine):
    first=prepare_save(setup_v2)
    assert setup_v2[0].receipt('r1') == first
    assert prepare_save(setup_v2)==first
    with pytest.raises(ValueError, match='request'):
        service, video, accounts, _ = setup_v2
        p=service.prepare(request_id='r1',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={'title':'changed'},execution_mode='publish_now')
        service.save(p['prepared_id'],request_id='r1')
    with session_scope(db_engine) as s: assert len(list(s.scalars(select(PublishDeliveryRow))))==4


def test_client_prepared_dto_is_not_authority(setup_v2):
    service, video, accounts, _=setup_v2
    p=service.prepare(request_id='safe',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={'title':'original'},execution_mode='publish_now')
    p['targets']=[{'account_id':'evil','platform':'wechat'}]
    p['title']='evil'
    receipt=service.save(p['prepared_id'],request_id='safe')
    assert service.get_task(receipt['id'])['deliveries'][0]['title']=='original'


@pytest.mark.parametrize('bad', [{'account_id':'missing','platform':'douyin'}, {'account_id':None,'platform':'wechat'}])
def test_invalid_input_leaves_no_partial_batch(setup_v2, db_engine, bad):
    service,video,_,_=setup_v2
    with pytest.raises(ValueError):service.prepare(request_id='bad',video_ids=[video.id],targets=[bad],common_fields={'title':'ok'},execution_mode='publish_now')
    with session_scope(db_engine) as s:assert not list(s.scalars(select(PublishDeliveryRow)))


@pytest.mark.parametrize('state',['failed','result_unknown','published','pre_submit_failed'])
def test_new_v2_task_cannot_bypass_stable_target_history(setup_v2,state):
    service,video,accounts,_=setup_v2
    PublishHistoryRepository(service.engine).record(video.content_hash,accounts[0].id,state)
    with pytest.raises(DuplicatePublishBlocked):
        service.prepare(request_id='blocked',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={'title':'self'},execution_mode='publish_now')


def test_unknown_a_does_not_stop_other_accounts_and_only_current_final_changes(setup_v2,db_engine):
    receipt=prepare_save(setup_v2)
    service,_,accounts,adapters=setup_v2
    adapters['douyin'].outcome='result_unknown'
    coord=PublishCoordinator(service)
    coord.run_once(receipt['id'])
    dto=service.get_task(receipt['id'])
    assert [d['state'] for d in dto['deliveries']]==['result_unknown','waiting','waiting','waiting']
    for _ in range(3):coord.run_once(receipt['id'])
    assert [d['state'] for d in service.get_task(receipt['id'])['deliveries']]==['result_unknown','published','published','published']
    assert len(adapters['douyin'].calls)==1


def test_prefinal_failure_does_not_stop_other_accounts(setup_v2):
    receipt=prepare_save(setup_v2); service,_,_,adapters=setup_v2
    adapters['douyin'].outcome='pre_submit_failed'
    coord=PublishCoordinator(service)
    for _ in range(4):coord.run_once(receipt['id'])
    assert [d['state'] for d in service.get_task(receipt['id'])['deliveries']]==['pre_submit_failed','published','published','published']


def test_sibling_state_does_not_change_claim_digest(setup_v2,db_engine):
    receipt=prepare_save(setup_v2);service=setup_v2[0]
    ids=[d['id'] for d in service.get_task(receipt['id'])['deliveries']]
    claim=service.deliveries.claim(ids[0])
    with session_scope(db_engine) as s:
        s.get(TaskCenterRow,receipt['id']).state='mixed'
        s.get(TaskItemRow,s.get(PublishDeliveryRow,ids[1]).item_id).state='failed'
    service.deliveries.mark_before_final(ids[0],claim['digest'],owner_token=claim['owner_token'])
    assert service.get_task(receipt['id'])['deliveries'][0]['state']=='result_unknown'


def test_wrong_observed_account_stops_before_final(setup_v2):
    receipt=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    adapters['douyin'].observed_uid='wrong'
    PublishCoordinator(service).run_once(receipt['id'])
    assert not adapters['douyin'].calls
    assert service.get_task(receipt['id'])['deliveries'][0]['state']=='pre_submit_failed'


def test_parking_yields_other_accounts_and_resume_uses_same_context(setup_v2):
    receipt=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    adapters['douyin'].outcome='park';coord=PublishCoordinator(service)
    for _ in range(4):coord.run_once(receipt['id'])
    dto=service.get_task(receipt['id']); assert dto['deliveries'][0]['state']=='paused_auth'
    assert all(d['state']=='published' for d in dto['deliveries'][1:])
    coord.resume_verification(dto['deliveries'][0]['id'])
    assert [call[0] for call in adapters['douyin'].calls]==['submit','resume']


def test_final_crash_and_restart_never_resubmit(setup_v2):
    receipt=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    adapters['douyin'].outcome='crash';coord=PublishCoordinator(service)
    with pytest.raises(SystemExit):coord.run_once(receipt['id'])
    new=MultiPublishService(service.engine,service.paths,catalog=service.catalog)
    new.recover_after_restart()
    coord2=PublishCoordinator(new)
    for _ in range(4):coord2.run_once(receipt['id'])
    assert len(adapters['douyin'].calls)==1
    assert new.get_task(receipt['id'])['deliveries'][0]['state']=='result_unknown'
    with pytest.raises(ValueError):new.reprepare_delivery(new.get_task(receipt['id'])['deliveries'][0]['id'],{'title':'bypass'})


def test_pause_blocks_future_final_but_preserves_submitted_evidence(setup_v2):
    receipt=prepare_save(setup_v2);service=setup_v2[0];coord=PublishCoordinator(service)
    coord.run_once(receipt['id']);service.pause(receipt['id'])
    coord.run_once(receipt['id'])
    assert [d['state'] for d in service.get_task(receipt['id'])['deliveries']]==['published','waiting','waiting','waiting']


def test_cancel_releases_only_child_consumer_and_cleanup_only_after_all(setup_v2,db_engine):
    receipt=prepare_save(setup_v2);service,video,_,_=setup_v2
    children=service.get_task(receipt['id'])['deliveries']
    with session_scope(db_engine) as s:path=Path(s.scalar(select(PublishResourceRow)).path)
    for d in children[:-1]:service.cancel_delivery(d['id'])
    assert path.exists() and video.effective_path.exists()
    service.cancel_delivery(children[-1]['id'])
    assert not path.exists() and video.effective_path.exists()


def test_cancel_after_final_preserves_unknown_and_resource(setup_v2,db_engine):
    receipt=prepare_save(setup_v2);service=setup_v2[0]
    d=service.get_task(receipt['id'])['deliveries'][0];claim=service.deliveries.claim(d['id'])
    service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])
    service.cancel_delivery(d['id']);service.hide_delivery(d['id'])
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,d['id']);assert s.get(TaskItemRow,row.item_id).state=='result_unknown'
        assert s.get(PublishHistoryRow,row.history_id).state=='result_unknown'
        assert Path(s.scalar(select(PublishResourceRow)).path).exists()


def test_unintegrated_and_missing_required_fields_are_editable_local_holds(setup_v2):
    service,video,accounts,_=setup_v2
    local=MultiPublishService(service.engine,service.paths)
    p=local.prepare(request_id='local',video_ids=[video.id],targets=[{'account_id':accounts[-1].id,'platform':'bilibili'}],common_fields={'title':'自有'},execution_mode='platform_schedule',scheduled_at=(NOW+timedelta(days=1)).isoformat())
    receipt=local.save(p['prepared_id'],request_id='local')
    d=local.get_task(receipt['id'])['deliveries'][0]
    assert d['state']=='waiting_configuration'
    local.reprepare_delivery(d['id'],{'category_id':1,'copyright':'original'})
    assert local.get_task(receipt['id'])['deliveries'][0]['state']=='waiting_adapter'


def test_reprepare_preserves_history_and_old_attempt(setup_v2,db_engine):
    receipt=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    adapters['douyin'].outcome='pre_submit_failed';PublishCoordinator(service).run_once(receipt['id'])
    d=service.get_task(receipt['id'])['deliveries'][0]
    with session_scope(db_engine) as s:history_id=s.get(PublishDeliveryRow,d['id']).history_id
    service.reprepare_delivery(d['id'],{'title':'corrected'})
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,d['id']);assert row.history_id==history_id and row.attempt==2
        assert len(list(s.scalars(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']))))==2
    adapters['douyin'].outcome='published';PublishCoordinator(service).run_once(receipt['id'])
    assert service.get_task(receipt['id'])['deliveries'][0]['state']=='published'


@pytest.mark.parametrize('legacy', ['broken', json.dumps({
    'started_at':'2020-01-01T00:00:00+00:00', 'expires_at':'2021-01-01T00:00:00+00:00',
    'last_seen_at':'2026-01-01T00:00:00+00:00'})])
def test_legacy_term_allows_save_claim_and_final_without_rewriting_receipt(setup_v2,db_engine,tmp_path,legacy):
    receipt=prepare_save(setup_v2);service,_,accounts,adapters=setup_v2
    children=service.get_task(receipt['id'])['deliveries']
    claim=service.deliveries.claim(children[0]['id'])
    with session_scope(db_engine) as s:
        s.merge(Setting(key='product:annual-use:v1',value_json=legacy))
    service.deliveries.mark_before_final(children[0]['id'],claim['digest'],owner_token=claim['owner_token'])
    assert service.deliveries.claim(children[1]['id'])['owner_token']
    path=tmp_path/'fresh-free-video.mp4';path.write_bytes(b'fresh synthetic free video')
    fresh=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='新成品')
    saved=prepare_save((service,fresh,accounts,adapters),request='free')
    assert saved['quantity']==4
    with session_scope(db_engine) as s:
        assert s.get(Setting,'product:annual-use:v1').value_json==legacy


def test_missed_software_slot_does_not_silently_run_now(setup_v2):
    receipt=prepare_save(setup_v2,execution_mode='software_schedule',scheduled_at=(NOW-timedelta(minutes=1)).isoformat())
    service=setup_v2[0]
    PublishCoordinator(service).run_once(receipt['id'])
    assert service.get_task(receipt['id'])['deliveries'][0]['state']=='missed_schedule'
    assert not setup_v2[3]['douyin'].calls


def test_public_dto_hides_identity_paths_claims_context_and_raw_results(setup_v2):
    receipt=prepare_save(setup_v2);service=setup_v2[0];PublishCoordinator(service).run_once(receipt['id'])
    encoded=json.dumps(service.get_task(receipt['id']))
    for value in ['synthetic-douyin','platform_user_id','owner_token','self-owned.mp4','publish_resources','raw_response']:assert value not in encoded


def test_v1_snapshot_json_and_markers_unchanged_by_v2(setup_v2,db_engine):
    original='{"version": 1, "kind": "normal_publish", "items": []}'
    with session_scope(db_engine) as s:
        s.add(PublishTaskRow(id='legacy',platform='douyin',state='paused',snapshot_json=original))
        s.add(TaskCenterRow(id='legacy',kind='normal_publish',name='legacy',state='paused',snapshot_json=original))
        s.add(Setting(key='publish.final.legacy',value_json='{"evidence": "keep"}'))
    receipt=prepare_save(setup_v2);PublishCoordinator(setup_v2[0]).run_once(receipt['id'])
    with session_scope(db_engine) as s:
        assert s.get(PublishTaskRow,'legacy').snapshot_json==original
        assert s.get(TaskCenterRow,'legacy').snapshot_json==original
        assert s.get(Setting,'publish.final.legacy').value_json=='{"evidence": "keep"}'
    assert setup_v2[0].get_task('legacy')['legacy_version']==1


def test_same_account_park_blocks_later_video_but_other_account_continues(setup_v2,tmp_path):
    service,video,accounts,adapters=setup_v2
    path=tmp_path/'second-owned.mp4';path.write_bytes(b'own-second')
    second=FinishedVideoService(service.engine).import_external(path,duration_ms=1000,title='second')
    p=service.prepare(request_id='same',video_ids=[video.id,second.id],targets=[{'account_id':a.id,'platform':a.platform} for a in accounts[:2]],common_fields={'title':'self'},execution_mode='publish_now')
    r=service.save(p['prepared_id'],request_id='same')
    adapters['douyin'].outcome='park';coord=PublishCoordinator(service)
    for _ in range(4):coord.run_once(r['id'])
    assert [d['state'] for d in service.get_task(r['id'])['deliveries']]==['paused_auth','published','waiting','published']
    assert len(adapters['douyin'].calls)==1


def test_concurrent_duplicate_save_commits_one_batch(setup_v2,db_engine):
    from concurrent.futures import ThreadPoolExecutor
    service,video,accounts,_=setup_v2
    p=service.prepare(request_id='concurrent',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={'title':'self'},execution_mode='publish_now')
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts=list(pool.map(lambda _:service.save(p['prepared_id'],request_id='concurrent'),range(2)))
    assert receipts[0]==receipts[1]
    with session_scope(db_engine) as s:assert len(list(s.scalars(select(PublishDeliveryRow))))==1


def test_database_write_failure_rolls_back_all_v2_rows_and_owned_copies(setup_v2,db_engine):
    service,video,accounts,_=setup_v2
    p=service.prepare(request_id='rollback',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={'title':'self'},execution_mode='publish_now')
    with db_engine.begin() as c:
        c.exec_driver_sql("CREATE TRIGGER reject_v2_history BEFORE INSERT ON publish_history BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(Exception):service.save(p['prepared_id'],request_id='rollback')
    with session_scope(db_engine) as s:
        assert not list(s.scalars(select(PublishDeliveryRow)))
        assert not list(s.scalars(select(PublishTaskRow)))
        assert not list(s.scalars(select(TaskItemRow)))
    assert not list((service.paths.data_dir/'publish_resources').glob('v2-*'))
    assert video.effective_path.exists()


def test_final_marker_db_failure_prevents_adapter_final(setup_v2,db_engine):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    with db_engine.begin() as c:
        c.exec_driver_sql("CREATE TRIGGER reject_v2_final BEFORE UPDATE OF final_json ON publish_delivery_attempts WHEN NEW.final_json IS NOT NULL BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    PublishCoordinator(service).run_once(r['id'])
    d=service.get_task(r['id'])['deliveries'][0]
    assert d['state']=='pre_submit_failed'
    with session_scope(db_engine) as s:
        row=s.scalar(select(DeliveryAttemptRow));assert row.final_json is None
        assert s.get(PublishHistoryRow,d['history_id']).state=='pre_submit_failed'


def test_prefinal_crash_needs_host_stopped_proof_and_explicit_evidence(setup_v2,db_engine):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    d=service.get_task(r['id'])['deliveries'][0]
    claim=service.deliveries.claim(d['id'])
    service.recover_after_restart()
    with pytest.raises(ValueError):service.reprepare_delivery(d['id'],{'title':'bypass'})
    with pytest.raises(ValueError):service.recover_delivery(d['id'])
    service.deliveries.workers.finished(claim['owner_hash'])
    adapters['douyin'].reconcile=lambda payload,context:AdapterOutcome('not_submitted')
    result=service.recover_delivery(d['id'])
    assert result['state']=='pre_submit_failed'
    service.reprepare_delivery(d['id'],{'title':'fixed'})
    assert service.get_task(r['id'])['deliveries'][0]['attempt']==2


def test_postfinal_recovery_uses_reconcile_never_submit(setup_v2):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    adapters['douyin'].outcome='crash'
    with pytest.raises(SystemExit):PublishCoordinator(service).run_once(r['id'])
    d=service.get_task(r['id'])['deliveries'][0]
    result=service.recover_delivery(d['id'])
    assert result['state']=='published'
    assert [c[0] for c in adapters['douyin'].calls]==['submit','reconcile']


def test_recovery_does_not_trust_frontend_boolean(setup_v2):
    r=prepare_save(setup_v2);service=setup_v2[0];d=service.get_task(r['id'])['deliveries'][0]
    with pytest.raises(TypeError):service.recover_delivery(d['id'],worker_stopped=True)


def test_successful_target_can_release_own_resources_without_erasing_evidence(setup_v2,db_engine):
    r=prepare_save(setup_v2);service=setup_v2[0];coord=PublishCoordinator(service)
    for _ in range(4):coord.run_once(r['id'])
    children=service.get_task(r['id'])['deliveries']
    with session_scope(db_engine) as s:path=Path(s.scalar(select(PublishResourceRow)).path)
    for d in children[:-1]:service.release_delivery_resources(d['id'])
    assert path.exists()
    service.release_delivery_resources(children[-1]['id'])
    assert not path.exists()
    assert [d['state'] for d in service.get_task(r['id'])['deliveries']]==['published']*4


def test_pausing_during_adapter_preparation_prevents_final(setup_v2):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    def submit(payload,*,context,before_final):
        service.pause(r['id']);before_final()
        pytest.fail('no final expected')
    adapters['douyin'].submit=submit
    PublishCoordinator(service).run_once(r['id'])
    assert service.get_task(r['id'])['deliveries'][0]['state']=='pre_submit_failed'


def test_frozen_resource_replacement_before_final_is_blocked(setup_v2,db_engine):
    r=prepare_save(setup_v2);service=setup_v2[0];d=service.get_task(r['id'])['deliveries'][0]
    claim=service.deliveries.claim(d['id']);Path(claim['payload']['video_path']).write_bytes(b'replaced-owned-file')
    with pytest.raises(ValueError,match='resource'):service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])
    with session_scope(db_engine) as s:assert s.scalar(select(DeliveryAttemptRow)).final_json is None


def test_migration_preserves_all_v1_json_row_counts_and_keys(tmp_path):
    engine=create_engine_for(tmp_path/'upgrade.db');migrate_to_revision(engine,'0015_local_video')
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO publish_tasks(id,platform,state,snapshot_json,created_at) VALUES('v1','douyin','paused','{\"version\":1}','2026-01-01')")
        c.exec_driver_sql("INSERT INTO settings(key,value_json,updated_at) VALUES('publish.final.synthetic','{\"keep\":true}','2026-01-01')")
        before={table:c.exec_driver_sql('SELECT * FROM '+table).fetchall() for table in ['publish_tasks','task_center_records','task_center_items','publish_history','settings']}
    migrate_to_head(engine)
    with engine.connect() as c:
        after={table:c.exec_driver_sql('SELECT * FROM '+table).fetchall() for table in before}
        assert after==before


def test_readiness_covers_paused_batch_account_and_software_clock(setup_v2):
    r=prepare_save(setup_v2,execution_mode='software_schedule',scheduled_at=(NOW+timedelta(hours=1)).isoformat())
    service=setup_v2[0];d=service.get_task(r['id'])['deliveries'][0]
    assert d['can_submit'] is False and d['readiness_reason']=='software_schedule_not_due'


def test_result_receipt_reports_final_started_without_claim_secrets(setup_v2):
    r=prepare_save(setup_v2);service=setup_v2[0]
    PublishCoordinator(service).run_once(r['id'])
    dto=service.get_task(r['id'])
    assert dto['submitted'] is True
    assert dto['deliveries'][0]['final_started'] is True
    assert dto['deliveries'][1]['final_started'] is False


def test_final_and_result_digest_corruption_cannot_unlock_target(setup_v2,db_engine):
    r=prepare_save(setup_v2);service=setup_v2[0];d=service.get_task(r['id'])['deliveries'][0]
    claim=service.deliveries.claim(d['id'])
    service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])
    service.deliveries.workers.finished(claim['owner_hash'])
    with session_scope(db_engine) as s:
        a=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']))
        changed=json.loads(a.final_json);changed['request_digest']='0'*64;a.final_json=json.dumps(changed)
    with pytest.raises(ValueError,match='journal'):service.recover_delivery(d['id'])


def test_task_center_restart_preserves_v2_child_authority(setup_v2):
    from yijianjifa.features.tasks.service import TaskCenterService
    r=prepare_save(setup_v2);service=setup_v2[0];d=service.get_task(r['id'])['deliveries'][0]
    service.deliveries.claim(d['id'])
    TaskCenterService(service.engine).recover_after_restart()
    assert service.get_task(r['id'])['deliveries'][0]['state']=='interrupted'


def test_distribution_splits_within_each_platform_and_whole_batch_is_explicit(setup_v2,tmp_path):
    service,video,accounts,_=setup_v2
    extra=AccountService(service.engine).create('douyin','synthetic-other-douyin','other')
    path=tmp_path/'split-second.mp4';path.write_bytes(b'split-second')
    second=FinishedVideoService(service.engine).import_external(path,duration_ms=1000,title='second')
    targets=[{'account_id':a.id,'platform':a.platform} for a in [accounts[0],extra,accounts[1]]]
    p=service.prepare(request_id='split',video_ids=[video.id,second.id],targets=targets,common_fields={'title':'self'},execution_mode='publish_now',distribution_by_platform={})
    assert p['quantity']==4
    assert [(d['video_id'],d['account_id']) for d in p['targets']]==[(video.id,accounts[0].id),(video.id,accounts[1].id),(second.id,extra.id),(second.id,accounts[1].id)]
    p=service.prepare(request_id='whole',video_ids=[video.id,second.id],targets=targets,common_fields={'title':'self'},execution_mode='publish_now',distribution_by_platform={'douyin':'whole_batch'})
    assert p['quantity']==6


def test_individual_video_times_and_platform_overrides_are_frozen(setup_v2,tmp_path):
    service,video,accounts,_=setup_v2
    path=tmp_path/'scheduled-second.mp4';path.write_bytes(b'own-scheduled-second')
    second=FinishedVideoService(service.engine).import_external(path,duration_ms=1000,title='second')
    first_time=(NOW+timedelta(days=1)).isoformat();second_time=(NOW+timedelta(days=2)).isoformat()
    p=service.prepare(request_id='slots',video_ids=[video.id,second.id],targets=[{'account_id':a.id,'platform':a.platform} for a in accounts[:2]],common_fields={'title':'self'},execution_mode='software_schedule',scheduled_times={video.id:first_time,second.id:second_time},schedule_by_platform={'kuaishou':{'execution_mode':'platform_schedule','scheduled_times':{video.id:second_time,second.id:(NOW+timedelta(days=3)).isoformat()}}})
    r=service.save(p['prepared_id'],request_id='slots')
    dto=service.get_task(r['id'])
    assert [d['scheduled_at'] for d in dto['deliveries']]==[first_time,second_time,second_time,(NOW+timedelta(days=3)).isoformat()]
    assert [d['execution_mode'] for d in dto['deliveries']]==['software_schedule','platform_schedule']*2


def test_duplicate_content_selected_under_two_video_ids_is_not_double_delivered(setup_v2):
    service,video,accounts,_=setup_v2
    duplicate=FinishedVideoService(service.engine).import_external(video.effective_path,duration_ms=1000,title='alias')
    with pytest.raises(ValueError,match='duplicate content'):
        service.prepare(request_id='alias',video_ids=[video.id,duplicate.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={'title':'same'},execution_mode='publish_now')


def test_new_unverified_transport_stays_local_even_when_injected(setup_v2):
    service,video,accounts,adapters=setup_v2
    adapters['kuaishou'].verified_capabilities=frozenset()
    p=service.prepare(request_id='pending',video_ids=[video.id],targets=[{'account_id':accounts[1].id,'platform':'kuaishou'}],common_fields={'title':'same'},execution_mode='publish_now')
    r=service.save(p['prepared_id'],request_id='pending')
    assert service.get_task(r['id'])['deliveries'][0]['state']=='waiting_adapter'
    PublishCoordinator(service).run_once(r['id'])
    assert not adapters['kuaishou'].calls


def test_child_snapshot_and_exact_history_mismatch_prevent_final(setup_v2,db_engine):
    r=prepare_save(setup_v2);service=setup_v2[0];d=service.get_task(r['id'])['deliveries'][0]
    claim=service.deliveries.claim(d['id'])
    other=service.get_task(r['id'])['deliveries'][1]
    with session_scope(db_engine) as s:
        history=s.get(PublishHistoryRow,d['history_id']);history.account_id=other['account_id']
    with pytest.raises(ValueError,match='authority'):service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])


def test_new_service_with_no_host_proof_does_not_recover_old_owner(setup_v2):
    r=prepare_save(setup_v2);service=setup_v2[0];d=service.get_task(r['id'])['deliveries'][0]
    claim=service.deliveries.claim(d['id']);service.deliveries.workers.finished(claim['owner_hash'])
    restarted=MultiPublishService(service.engine,service.paths,catalog=service.catalog)
    restarted.recover_after_restart()
    with pytest.raises(ValueError,match='host'):restarted.recover_delivery(d['id'])


def test_partial_history_plan_is_explicit_and_requires_consent(setup_v2,db_engine):
    service,video,accounts,_=setup_v2
    old=PublishHistoryRepository(service.engine).record(video.content_hash,accounts[0].id,'result_unknown')
    p=service.prepare(request_id='partial',video_ids=[video.id],targets=[{'account_id':a.id,'platform':a.platform} for a in accounts[:2]],common_fields={'title':'self'},execution_mode='publish_now')
    assert (p['requested_quantity'],p['quantity'],p['excluded_count'])==(2,1,1)
    assert p['excluded_targets'][0]['account_id']==accounts[0].id
    assert p['excluded_targets'][0]['history'][0]['history_id']==old.id
    assert p['targets'][0]['account_id']==accounts[1].id
    with pytest.raises(ValueError,match='partial'):service.save(p['prepared_id'],request_id='partial')
    r=service.save(p['prepared_id'],request_id='partial',consent_partial=True)
    assert r['quantity']==1 and r['excluded_count']==1
    with session_scope(db_engine) as s:
        assert len(list(s.scalars(select(PublishDeliveryRow))))==1
        assert len(list(s.scalars(select(PublishHistoryRow))))==2
    PublishCoordinator(service).run_once(r['id'])
    assert service.get_task(r['id'])['deliveries'][0]['state']=='published'


def test_partial_split_does_not_redistribute_blocked_video_to_other_same_platform_account(setup_v2,tmp_path):
    service,video,accounts,_=setup_v2
    extra=AccountService(service.engine).create('douyin','synthetic-extra','extra')
    path=tmp_path/'partial-second.mp4';path.write_bytes(b'partial-second')
    second=FinishedVideoService(service.engine).import_external(path,duration_ms=1000,title='second')
    PublishHistoryRepository(service.engine).record(video.content_hash,accounts[0].id,'published')
    p=service.prepare(request_id='partial-split',video_ids=[video.id,second.id],targets=[{'account_id':a.id,'platform':a.platform} for a in [accounts[0],extra,accounts[1]]],common_fields={'title':'self'},execution_mode='publish_now')
    assert p['requested_quantity']==4 and p['quantity']==3 and p['excluded_count']==1
    assert (video.id,extra.id) not in [(d['video_id'],d['account_id']) for d in p['targets']]


@pytest.mark.parametrize('legacy', ['broken', json.dumps({
    'started_at':'2020-01-01T00:00:00+00:00', 'expires_at':'2021-01-01T00:00:00+00:00',
    'last_seen_at':'2026-01-01T00:00:00+00:00'})])
def test_legacy_term_allows_existing_result_reconciliation(setup_v2,db_engine,legacy):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    adapters['douyin'].outcome='result_unknown';coord=PublishCoordinator(service);coord.run_once(r['id'])
    with session_scope(db_engine) as s:
        s.merge(Setting(key='product:annual-use:v1',value_json=legacy))
    assert coord.reconcile_delivery(service.get_task(r['id'])['deliveries'][0]['id'])=='published'
    assert [c[0] for c in adapters['douyin'].calls]==['submit','reconcile']
    with session_scope(db_engine) as s:
        assert s.get(Setting,'product:annual-use:v1').value_json==legacy


def test_reopen_reconciliation_reads_persisted_receipt_and_keeps_final(setup_v2,db_engine):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    def submit(payload,*,context,before_final):
        before_final();return AdapterOutcome('result_unknown',receipt_reference={'photo_id':'synthetic-photo-1'},error_code='timeout')
    adapters['douyin'].submit=submit;PublishCoordinator(service).run_once(r['id'])
    d=service.get_task(r['id'])['deliveries'][0]
    with session_scope(db_engine) as s:
        a=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']));old_final=a.final_json;old_owner=a.owner_hash
    def reconcile(payload,*,context):
        assert payload['receipt_reference']=={'photo_id':'synthetic-photo-1'}
        return AdapterOutcome('published',receipt_reference={'photo_id':'synthetic-photo-1'})
    adapters['douyin'].reconcile=reconcile
    reopened=MultiPublishService(service.engine,service.paths,catalog=service.catalog,workers=service.deliveries.workers)
    assert PublishCoordinator(reopened).reconcile_delivery(d['id'])=='published'
    dto=reopened.get_task(r['id'])['deliveries'][0]
    assert dto['receipt_summary']['reference']=={'photo_id':'synthetic-photo-1'}
    with session_scope(db_engine) as s:
        a=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']));assert a.final_json==old_final and a.owner_hash==old_owner


def test_bilibili_repost_fields_are_held_until_source_is_corrected(setup_v2):
    service,video,accounts,_=setup_v2
    p=service.prepare(request_id='repost',video_ids=[video.id],targets=[{'account_id':accounts[-1].id,'platform':'bilibili','fields':{'category_id':1,'copyright':'repost'}}],common_fields={'title':'self'},execution_mode='publish_now')
    r=service.save(p['prepared_id'],request_id='repost');d=service.get_task(r['id'])['deliveries'][0]
    assert d['state']=='waiting_configuration'
    service.reprepare_delivery(d['id'],{'source':'https://example.invalid/self-owned-source'})
    assert service.get_task(r['id'])['deliveries'][0]['state']=='waiting'


def test_reprepare_distinguishes_clear_from_restoring_public_field(setup_v2,db_engine):
    service,video,accounts,_=setup_v2
    p=service.prepare(request_id='override',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin','fields':{'body':'platform'}}],common_fields={'title':'self','body':'public'},execution_mode='publish_now')
    r=service.save(p['prepared_id'],request_id='override');d=service.get_task(r['id'])['deliveries'][0]
    service.reprepare_delivery(d['id'],{'body':''})
    with session_scope(db_engine) as s:assert json.loads(s.get(PublishDeliveryRow,d['id']).snapshot_json)['fields']['body']==''
    service.reprepare_delivery(d['id'],{},reset_fields=['body'])
    with session_scope(db_engine) as s:assert json.loads(s.get(PublishDeliveryRow,d['id']).snapshot_json)['fields']['body']=='public'
    assert FinishedVideoService(service.engine).get(video.id).body==''


def test_per_video_public_and_target_override_isolated_from_other_video_and_library(setup_v2,tmp_path,db_engine):
    service,video,accounts,_=setup_v2
    path=tmp_path/'override-second.mp4';path.write_bytes(b'own-video-override-second')
    second=FinishedVideoService(service.engine).import_external(path,duration_ms=1000,title='second')
    p=service.prepare(request_id='pervideo',video_ids=[video.id,second.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'},
        {'account_id':accounts[-1].id,'platform':'bilibili','fields':{'category_id':1,'copyright':'original'},'fields_by_video':{video.id:{'title':'B站当前'}}}],
        common_fields={'title':'公共'},fields_by_video={video.id:{'body':'当前公共正文'}},execution_mode='publish_now')
    r=service.save(p['prepared_id'],request_id='pervideo')
    assert [d['title'] for d in service.get_task(r['id'])['deliveries']]==['公共','B站当前','公共','公共']
    with session_scope(db_engine) as s:
        rows=list(s.scalars(select(PublishDeliveryRow).order_by(PublishDeliveryRow.position)))
        assert [json.loads(row.snapshot_json)['fields']['body'] for row in rows]==['当前公共正文','当前公共正文','','']
    assert FinishedVideoService(service.engine).get(video.id).title=='自有测试'
    assert FinishedVideoService(service.engine).get(second.id).title=='second'


def test_per_video_fields_reject_unselected_video(setup_v2):
    service,video,accounts,_=setup_v2
    with pytest.raises(ValueError,match='unselected'):
        service.prepare(request_id='bad-pervideo',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={'title':'self'},fields_by_video={'not-selected':{'title':'wrong'}},execution_mode='publish_now')


def test_coordinator_has_one_finite_worker_even_with_concurrent_clicks(setup_v2):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    entered=Event();release=Event()
    def submit(payload,*,context,before_final):
        entered.set();assert release.wait(3);before_final();return AdapterOutcome('published')
    adapters['douyin'].submit=submit
    first=PublishCoordinator(service);second=PublishCoordinator(service)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(first.run_once,r['id'])
        assert entered.wait(3)
        try:
            result=second.run_once(r['id'])
            assert result is None and not adapters['kuaishou'].calls
        finally:release.set()
        assert future.result(timeout=3)=='published'
    second.run_once(r['id'])
    assert len(adapters['kuaishou'].calls)==1


@pytest.mark.parametrize('mismatch',[False,True])
def test_initial_identity_verification_can_park_and_resume_same_broker_action(setup_v2,mismatch):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    adapter=adapters['douyin'];verified=[False]
    def validation(target,payload,execution):
        if not verified[0]:return AdapterOutcome('paused_auth',context={'handle':'initial-login'})
        return AdapterValidation('wrong' if mismatch else target['platform_user_id'],True)
    def resume(payload,*,context,before_final):
        assert context=={'handle':'initial-login'}
        verified[0]=True;adapter.calls.append(('resume',payload['video_id']));before_final();return AdapterOutcome('published')
    adapter.validate_target=validation;adapter.resume=resume
    coord=PublishCoordinator(service)
    for _ in range(4):coord.run_once(r['id'])
    dto=service.get_task(r['id']);assert dto['deliveries'][0]['state']=='paused_auth'
    assert all(d['state']=='published' for d in dto['deliveries'][1:])
    assert not adapter.calls
    coord.resume_verification(dto['deliveries'][0]['id'])
    d=service.get_task(r['id'])['deliveries'][0]
    assert d['state']==('pre_submit_failed' if mismatch else 'published')
    assert d['final_started'] is (not mismatch)
    assert [c[0] for c in adapter.calls]==['resume']


def test_host_authorized_subset_and_delivery_binding_do_not_change_digest(setup_v2):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    children=service.get_task(r['id'])['deliveries'];bound=[]
    adapters['kuaishou'].bind_delivery=lambda **kw:bound.append(kw)
    coord=PublishCoordinator(service)
    coord.run_once(r['id'],delivery_ids=[children[1]['id']])
    assert not adapters['douyin'].calls and len(adapters['kuaishou'].calls)==1
    assert bound[0]['delivery_id']==children[1]['id'] and bound[0]['task_id']==r['id'] and len(bound[0]['digest'])==64
    with pytest.raises(ValueError):coord.run_once(r['id'],delivery_ids=['outside-batch'])


def test_actual_finished_deletion_follows_v2_child_consumers_not_parent_batch(setup_v2,tmp_path):
    service,video,accounts,_=setup_v2
    path=tmp_path/'delete-second.mp4';path.write_bytes(b'own-second-delete')
    second=FinishedVideoService(service.engine).import_external(path,duration_ms=1000,title='second')
    p=service.prepare(request_id='delete-consumers',video_ids=[video.id,second.id],targets=[{'account_id':a.id,'platform':a.platform} for a in accounts[:2]],common_fields={'title':'self'},execution_mode='publish_now')
    r=service.save(p['prepared_id'],request_id='delete-consumers')
    with pytest.raises(RuntimeError):FinishedVideoService(service.engine).delete(video.id)
    with pytest.raises(RuntimeError):FinishedVideoService(service.engine).delete(second.id)
    children=service.get_task(r['id'])['deliveries']
    for d in children:
        if d['video_id']==video.id:service.cancel_delivery(d['id'])
    FinishedVideoService(service.engine).delete(video.id)
    assert FinishedVideoService(service.engine).get(video.id) is None and video.effective_path.exists()
    with pytest.raises(RuntimeError):FinishedVideoService(service.engine).delete(second.id)


def test_unknown_child_keeps_its_copy_and_duplicate_guard_after_source_deletion(setup_v2):
    r=prepare_save(setup_v2);service,video,_,adapters=setup_v2
    adapters['douyin'].outcome='result_unknown';PublishCoordinator(service).run_once(r['id'])
    for d in service.get_task(r['id'])['deliveries']:
        service.cancel_delivery(d['id']);service.hide_delivery(d['id'])
    with session_scope(service.engine) as s:
        row=s.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.platform=='douyin'))
        attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id))
        evidence=attempt.result_json
        copied=Path(json.loads(row.snapshot_json)['video_path'])
    from yijianjifa.app_services.finished_delete import FinishedDeletion
    assert FinishedDeletion(service.engine).delete(video.id)['deleted']
    assert copied.exists() and video.effective_path.exists() # external original stays
    with session_scope(service.engine) as s:
        assert s.get(DeliveryAttemptRow,attempt.id).result_json==evidence
        assert s.get(PublishHistoryRow,row.history_id).state=='result_unknown'
    assert PublishHistoryRepository(service.engine).has_quick_block(video.content_hash)
    assert [call[0] for call in adapters['douyin'].calls]==['submit']


def test_missed_target_reschedule_keeps_delivery_history_and_attempt_audit(setup_v2,db_engine):
    r=prepare_save(setup_v2,execution_mode='software_schedule',scheduled_at=(NOW-timedelta(minutes=2)).isoformat())
    service=setup_v2[0];coord=PublishCoordinator(service);coord.run_once(r['id'])
    d=service.get_task(r['id'])['deliveries'][0];assert d['state']=='missed_schedule'
    newtime=(NOW+timedelta(days=1)).isoformat()
    result=service.reschedule_delivery(d['id'],execution_mode='platform_schedule',scheduled_at=newtime)
    assert result['attempt']==2 and result['state']=='waiting'
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,d['id']);assert row.history_id==d['history_id'] and row.execution_mode=='platform_schedule' and row.scheduled_at==newtime
        attempts=list(s.scalars(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']).order_by(DeliveryAttemptRow.number)))
        assert len(attempts)==2 and json.loads(attempts[0].snapshot_json)['execution_mode']=='software_schedule'
        assert json.loads(attempts[1].snapshot_json)['scheduled_at']==newtime


def test_reschedule_cannot_bypass_live_owner_or_final_marker(setup_v2):
    r=prepare_save(setup_v2);service=setup_v2[0];d=service.get_task(r['id'])['deliveries'][0]
    claim=service.deliveries.claim(d['id'])
    with pytest.raises(ValueError):service.reschedule_delivery(d['id'],execution_mode='publish_now')
    service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])
    service.deliveries.workers.finished(claim['owner_hash'])
    with pytest.raises(ValueError):service.reschedule_delivery(d['id'],execution_mode='publish_now')


@pytest.mark.parametrize('state',['platform_accepted','processing','reviewing'])
def test_formal_receipt_states_are_distinct_and_reconcile_without_resubmit(setup_v2,db_engine,state):
    r=prepare_save(setup_v2);service,video,accounts,adapters=setup_v2
    def submit(payload,*,context,before_final):
        adapters['douyin'].calls.append(('submit',payload['video_id']))
        before_final();return AdapterOutcome(state,receipt_reference={'resource_id':'formal-1'})
    adapters['douyin'].submit=submit
    coord=PublishCoordinator(service);coord.run_once(r['id'])
    d=service.get_task(r['id'])['deliveries'][0]
    assert d['state']==state and d['receipt_summary']['reference']=={'resource_id':'formal-1'}
    with pytest.raises(DuplicatePublishBlocked):
        service.prepare(request_id='formal-bypass',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={'title':'changed'},execution_mode='publish_now')
    adapters['douyin'].reconcile=lambda payload,context:AdapterOutcome('reviewing',receipt_reference={'resource_id':'formal-1'})
    assert coord.reconcile_delivery(d['id'])=='reviewing'
    adapters['douyin'].reconcile=lambda payload,context:AdapterOutcome('published',receipt_reference={'resource_id':'formal-1'})
    assert coord.reconcile_delivery(d['id'])=='published'
    assert [c[0] for c in adapters['douyin'].calls]==['submit']
    with session_scope(db_engine) as s:
        a=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']))
        assert a.final_json and a.owner_hash and state in a.recovery_json and 'reviewing' in a.recovery_json


def test_formal_receipt_without_reference_is_unknown(setup_v2):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    def submit(payload,*,context,before_final):
        before_final();return AdapterOutcome('platform_accepted')
    adapters['douyin'].submit=submit;PublishCoordinator(service).run_once(r['id'])
    assert service.get_task(r['id'])['deliveries'][0]['state']=='result_unknown'


def test_formal_acceptance_without_final_marker_is_protocol_unknown_not_retryable(setup_v2):
    r=prepare_save(setup_v2);service,_,_,adapters=setup_v2
    adapters['douyin'].submit=lambda payload,context,before_final:AdapterOutcome('platform_accepted',receipt_reference={'resource_id':'phantom'})
    PublishCoordinator(service).run_once(r['id'])
    d=service.get_task(r['id'])['deliveries'][0]
    assert d['state']=='result_unknown' and d['final_started'] is False
    with pytest.raises(ValueError):service.reschedule_delivery(d['id'],execution_mode='publish_now')


def saved_cover(setup, db_engine, kind='uploaded', *, missing=False):
    """Self-owned valid 2x2 PNG; no media or platform operations."""
    import base64
    import hashlib
    service, video, *_ = setup
    content=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg==')
    sha=hashlib.sha256(content).hexdigest()
    folder=service.paths.data_dir/'covers';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(sha+'.png')
    if not missing:path.write_bytes(content)
    with session_scope(db_engine) as s:
        s.add(Setting(key='finished-cover:'+video.id,value_json=json.dumps({'kind':kind,'frame_ms':500 if kind=='frame' else None,'sha256':sha})))
    return path,sha


def prepare_one(setup, request='one'):
    service,video,accounts,_=setup
    return service.prepare(request_id=request,video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={},execution_mode='publish_now')


@pytest.mark.parametrize('kind',['uploaded','frame'])
def test_source_custom_cover_is_inherited_frozen_and_held(setup_v2,db_engine,kind):
    source,sha=saved_cover(setup_v2,db_engine,kind)
    service=setup_v2[0];p=prepare_one(setup_v2)
    assert p['targets'][0]['state']=='waiting_configuration'
    r=service.save(p['prepared_id'],request_id='one')
    child=service.get_task(r['id'])['deliveries'][0]
    assert child['can_submit'] is False
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,child['id']);payload=json.loads(row.snapshot_json)
        assert payload['fields']['cover']['kind']==kind
        assert payload['fields']['cover']['sha256']==sha
        copy=Path(payload['fields']['cover']['path'])
        assert copy!=source and copy.read_bytes()==source.read_bytes()
        assert copy.is_relative_to(service.paths.data_dir/'publish_resources')
        refs=list(s.scalars(select(ResourceConsumerRow).where(ResourceConsumerRow.delivery_id==row.id)))
        assert len(refs)==2
        assert any(s.get(PublishResourceRow,ref.resource_id).content_hash==sha for ref in refs)
    service.cancel_delivery(child['id'])
    assert source.exists() and not copy.exists()


def test_missing_source_cover_is_preserved_as_local_hold(setup_v2,db_engine):
    saved_cover(setup_v2,db_engine,missing=True)
    service=setup_v2[0];p=prepare_one(setup_v2)
    assert p['targets'][0]['state']=='waiting_configuration'
    r=service.save(p['prepared_id'],request_id='one')
    child=service.get_task(r['id'])['deliveries'][0]
    assert child['can_submit'] is False
    with session_scope(db_engine) as s:
        cover=json.loads(s.get(PublishDeliveryRow,child['id']).snapshot_json)['fields']['cover']
        assert cover['kind']=='uploaded' and cover['missing'] is True


@pytest.mark.parametrize('change',['choice','bytes'])
def test_source_cover_change_after_prepare_rejects_save_atomically(setup_v2,db_engine,change):
    source,_=saved_cover(setup_v2,db_engine)
    service,video,*_=setup_v2;p=prepare_one(setup_v2)
    if change=='bytes':source.write_bytes(b'changed cover')
    else:
        with session_scope(db_engine) as s:
            row=s.get(Setting,'finished-cover:'+video.id)
            data=json.loads(row.value_json);data['kind']='frame';data['frame_ms']=500;row.value_json=json.dumps(data)
    with pytest.raises(ValueError,match='cover'):service.save(p['prepared_id'],request_id='one')
    with session_scope(db_engine) as s:assert not list(s.scalars(select(PublishDeliveryRow)))
    assert not list((service.paths.data_dir/'publish_resources').glob('v2-*'))


def test_default_first_frame_does_not_hold_and_source_metadata_stays_frozen(setup_v2,db_engine):
    from yijianjifa.features.finished.service import FinishedVideo
    service,video,accounts,_=setup_v2;p=prepare_one(setup_v2)
    with session_scope(db_engine) as s:
        row=s.get(FinishedVideo,video.id);row.title='new title';row.body='new body';row.tags='new tags'
    r=service.save(p['prepared_id'],request_id='one')
    child=service.get_task(r['id'])['deliveries'][0]
    assert child['can_submit'] is True and child['title']=='自有测试'
    with session_scope(db_engine) as s:
        fields=json.loads(s.get(PublishDeliveryRow,child['id']).snapshot_json)['fields']
        assert fields['title']=='自有测试' and fields['body']==video.body and fields['tags']==video.tags
    assert PublishCoordinator(service).run_once(r['id'])=='published'


@pytest.mark.parametrize('state',['published','platform_scheduled','platform_rejected','result_unknown','platform_accepted','processing','reviewing'])
def test_all_submitted_outcomes_without_marker_are_unknown_and_block_retry(setup_v2,db_engine,state):
    service,video,accounts,adapters=setup_v2
    p=prepare_one(setup_v2);r=service.save(p['prepared_id'],request_id='one')
    calls=[]
    def submit(payload,*,context,before_final):
        calls.append(payload['video_id'])
        return AdapterOutcome(state,receipt_reference={'work_id':'reported-work'})
    adapters['douyin'].submit=submit;coord=PublishCoordinator(service)
    assert coord.run_once(r['id'])=='result_unknown'
    d=service.get_task(r['id'])['deliveries'][0]
    assert d['final_started'] is False and d['can_submit'] is False
    assert d['receipt_summary']['reference']=={'work_id':'reported-work'}
    with session_scope(db_engine) as s:
        a=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']))
        result=json.loads(a.result_json)
        assert result['state']=='result_unknown' and result['reported_state']==state
        assert result['protocol_violation']=='receipt_without_final_marker' and a.final_json is None
    with pytest.raises(ValueError):service.reprepare_delivery(d['id'],{'title':'retry'})
    with pytest.raises(ValueError):service.reschedule_delivery(d['id'],execution_mode='publish_now')
    with pytest.raises(DuplicatePublishBlocked):prepare_one(setup_v2,'new-request')
    with pytest.raises(DuplicatePublishBlocked):
        PublishHistoryRepository(db_engine).assert_can_create(video.content_hash,accounts[0].id,allow_repeat_success=True)
    assert coord.run_once(r['id']) is None and calls==[video.id]


@pytest.mark.parametrize('state',['waiting','waiting_adapter','waiting_configuration','missed_schedule','interrupted','platform_accepted','processing','reviewing'])
def test_v2_history_states_block_legacy_creation(setup_v2,db_engine,state):
    service,video,accounts,_=setup_v2
    p=prepare_one(setup_v2);r=service.save(p['prepared_id'],request_id='one')
    d=service.get_task(r['id'])['deliveries'][0]
    with session_scope(db_engine) as s:
        row=s.get(PublishDeliveryRow,d['id'])
        s.get(TaskItemRow,row.item_id).state=s.get(PublishHistoryRow,row.history_id).state=state
    with pytest.raises(DuplicatePublishBlocked):
        PublishHistoryRepository(db_engine).assert_can_create(video.content_hash,accounts[0].id,allow_repeat_success=True)


@pytest.mark.parametrize('state',['queued','result_unknown','platform_scheduled'])
@pytest.mark.parametrize('boundary',['claim','final'])
def test_legacy_history_added_after_v2_save_blocks_claim_and_final_by_stable_uid(setup_v2,db_engine,state,boundary):
    service,video,accounts,_=setup_v2;p=prepare_one(setup_v2)
    r=service.save(p['prepared_id'],request_id='one');d=service.get_task(r['id'])['deliveries'][0]
    claim=service.deliveries.claim(d['id']) if boundary=='final' else None
    alias=AccountService(db_engine).create('douyin','historical-row-now-changed','same stable target')
    with session_scope(db_engine) as s:
        s.add(PublishTaskRow(id='legacy-added',platform='douyin',state='paused',snapshot_json=json.dumps({'accounts':[{'id':alias.id,'platform':'douyin','platform_user_id':accounts[0].platform_user_id}]})))
    PublishHistoryRepository(db_engine).record(video.content_hash,alias.id,state,task_id='legacy-added')
    with pytest.raises(ValueError,match='duplicate|history|target'):
        if boundary=='claim':service.deliveries.claim(d['id'])
        else:service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])
    with session_scope(db_engine) as s:
        a=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']))
        assert a.final_json is None
        if boundary=='claim':assert a.owner_hash is None
    # Other accounts and platforms remain eligible.
    p=service.prepare(request_id='other',video_ids=[video.id],targets=[{'account_id':accounts[1].id,'platform':'kuaishou'}],common_fields={},execution_mode='publish_now')
    other=service.save(p['prepared_id'],request_id='other')
    assert PublishCoordinator(service).run_once(other['id'])=='published'


def test_prepared_account_uid_change_rejects_save(setup_v2,db_engine):
    service,_,accounts,_=setup_v2;p=prepare_one(setup_v2)
    with session_scope(db_engine) as s:s.get(AccountRow,accounts[0].id).platform_user_id='changed-uid'
    with pytest.raises(ValueError,match='identity changed'):service.save(p['prepared_id'],request_id='one')
    with session_scope(db_engine) as s:assert not list(s.scalars(select(PublishDeliveryRow)))


def test_legacy_preparation_cannot_create_while_v2_waiting(setup_v2,db_engine):
    from yijianjifa.app_services.publish_preparation import PublishPreparation
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    service,video,accounts,_=setup_v2
    with session_scope(db_engine) as s:
        s.get(AccountRow,accounts[0].id).platform_user_id='123456'
        s.get(AccountRow,accounts[0].id).session_ref='synthetic-only-session'
    AccountService(db_engine).update_capabilities(accounts[0].id,CapabilitySnapshot(ordinary='available'))
    p=prepare_one(setup_v2);service.save(p['prepared_id'],request_id='one')
    now=datetime.now(timezone.utc)
    legacy=PublishPreparation(db_engine,service.paths)
    p=legacy.prepare(dict(account_ids=[accounts[0].id],video_ids=[video.id],distribution='split_across_accounts',
        start=(now+timedelta(hours=4)).replace(second=0,microsecond=0).isoformat(),mode='daily',interval_hours=3,daily_count=1,days=1),now=now)
    assert p['token'] is None and p['items'][0]['blockers']==['duplicate_blocked']


def test_legacy_history_conflict_exposes_v2_unready_without_claim(setup_v2,db_engine):
    service,video,accounts,_=setup_v2;p=prepare_one(setup_v2)
    r=service.save(p['prepared_id'],request_id='one')
    PublishHistoryRepository(db_engine).record(video.content_hash,accounts[0].id,'queued')
    d=service.get_task(r['id'])['deliveries'][0]
    assert d['can_submit'] is False and d['readiness_reason']=='existing_target_unresolved'
    assert PublishCoordinator(service).run_once(r['id']) is None


def test_missing_source_cover_keeps_selected_hash(setup_v2,db_engine):
    _,sha=saved_cover(setup_v2,db_engine,missing=True)
    service=setup_v2[0];p=prepare_one(setup_v2);r=service.save(p['prepared_id'],request_id='one')
    with session_scope(db_engine) as s:
        row=s.scalar(select(PublishDeliveryRow))
        assert json.loads(row.snapshot_json)['fields']['cover'].get('sha256')==sha


def test_owned_source_cover_changed_after_claim_blocks_final(setup_v2,db_engine):
    source,_=saved_cover(setup_v2,db_engine)
    service=setup_v2[0];p=prepare_one(setup_v2);r=service.save(p['prepared_id'],request_id='one')
    d=service.get_task(r['id'])['deliveries'][0]
    # Explicitly choose the supported default while retaining the source choice
    # as a referenced frozen base for later override resets.
    service.reprepare_delivery(d['id'],{'cover':{'kind':'first_frame','frame_ms':0}})
    claim=service.deliveries.claim(d['id'])
    with session_scope(db_engine) as s:
        payload=json.loads(s.get(PublishDeliveryRow,d['id']).snapshot_json)
        copy=Path(payload['base_fields']['cover']['path'])
    copy.write_bytes(b'changed owned cover')
    with pytest.raises(ValueError,match='resource changed'):
        service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])
    with session_scope(db_engine) as s:
        a=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id'],DeliveryAttemptRow.number==2))
        assert a.final_json is None
    assert source.exists()


def test_source_cover_does_not_overwrite_explicit_common_first_frame_choice(setup_v2,db_engine):
    saved_cover(setup_v2,db_engine)
    service,video,accounts,_=setup_v2
    p=service.prepare(request_id='override-cover',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],
        common_fields={'cover':{'kind':'first_frame','frame_ms':0}},execution_mode='publish_now')
    r=service.save(p['prepared_id'],request_id='override-cover')
    with session_scope(db_engine) as s:
        row=s.scalar(select(PublishDeliveryRow));payload=json.loads(row.snapshot_json)
        assert payload['fields']['cover']=={'kind':'first_frame','frame_ms':0}
    assert service.get_task(r['id'])['deliveries'][0]['can_submit'] is True


def test_unverified_first_frame_path_is_preserved_as_hold(setup_v2):
    service,video,accounts,_=setup_v2
    p=service.prepare(request_id='unverified-cover',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],
        common_fields={'cover':{'kind':'first_frame','frame_ms':0,'path':'unverified.png'}},execution_mode='publish_now')
    assert p['targets'][0]['state']=='waiting_configuration'


@pytest.mark.parametrize('bad',['reference','context','context_object','schedule_object','error_code'])
def test_malformed_submitted_receipt_without_final_still_blocks_retry(setup_v2,db_engine,bad):
    service,video,accounts,adapters=setup_v2;p=prepare_one(setup_v2);r=service.save(p['prepared_id'],request_id='one')
    kwargs={'receipt_reference':{'work_id':'reported-work'}}
    if bad=='reference':kwargs['receipt_reference']={'secret_token':'must-not-persist'}
    elif bad=='context':kwargs['context']={'cookie':'must-not-persist'}
    elif bad=='context_object':kwargs['context']=object()
    elif bad=='schedule_object':kwargs['scheduled_at']=object()
    else:kwargs['error_code']='untrusted raw error'
    adapters['douyin'].submit=lambda payload,context,before_final:AdapterOutcome('published',**kwargs)
    assert PublishCoordinator(service).run_once(r['id'])=='result_unknown'
    d=service.get_task(r['id'])['deliveries'][0]
    assert d['final_started'] is False
    with pytest.raises(ValueError):service.reprepare_delivery(d['id'],{'title':'retry'})
    with session_scope(db_engine) as s:
        row=s.scalar(select(DeliveryAttemptRow));result=json.loads(row.result_json)
        assert result['reported_state']=='published'
        assert 'must-not-persist' not in row.result_json and row.context_json is None


@pytest.mark.parametrize('state',['published','platform_scheduled','platform_rejected','result_unknown','platform_accepted','processing','reviewing'])
@pytest.mark.parametrize('error_code',[pytest.param([],id='list'),pytest.param({},id='dict')])
def test_container_error_code_in_submitted_receipt_is_unknown_without_retry(setup_v2,db_engine,state,error_code):
    service,video,accounts,adapters=setup_v2;p=prepare_one(setup_v2);r=service.save(p['prepared_id'],request_id='one')
    calls=[]
    def submit(payload,*,context,before_final):
        calls.append(payload['video_id'])
        return AdapterOutcome(state,error_code=error_code)
    adapters['douyin'].submit=submit;coordinator=PublishCoordinator(service)
    assert coordinator.run_once(r['id'])=='result_unknown'
    d=service.get_task(r['id'])['deliveries'][0]
    assert d['state']=='result_unknown' and d['final_started'] is False and d['can_submit'] is False
    with session_scope(db_engine) as s:
        a=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==d['id']))
        result=json.loads(a.result_json)
        assert a.final_json is None and a.context_json is None
        assert result['state']=='result_unknown' and result['reported_state']==state
        assert result['protocol_violation']=='receipt_without_final_marker'
        assert result['error_code']=='result_unknown'
    with pytest.raises(ValueError):service.reprepare_delivery(d['id'],{'title':'retry'})
    with pytest.raises(ValueError):service.reschedule_delivery(d['id'],execution_mode='publish_now')
    with pytest.raises(DuplicatePublishBlocked):prepare_one(setup_v2,'new-request')
    with pytest.raises(DuplicatePublishBlocked):
        PublishHistoryRepository(db_engine).assert_can_create(video.content_hash,accounts[0].id,allow_repeat_success=True)
    assert coordinator.run_once(r['id']) is None and calls==[video.id]


def confirmed_query_case(setup, state, *, reference=None, returned_time=None):
    service,video,accounts,adapters=setup
    when=(datetime.now(timezone.utc)+timedelta(hours=5)).isoformat() if state=='platform_scheduled' else None
    prepared=service.prepare(request_id='confirmed-query',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],
        common_fields={},execution_mode='platform_schedule' if when else 'publish_now',scheduled_at=when)
    receipt=service.save(prepared['prepared_id'],request_id='confirmed-query')
    adapter=adapters['douyin'];reference={'work_id':'confirmed-1'} if reference is None else reference
    def submit(payload,*,context,before_final):
        adapter.calls.append(('submit',payload['video_id']));before_final()
        return AdapterOutcome(state,receipt_reference=reference,scheduled_at=returned_time or when)
    adapter.submit=submit;coordinator=PublishCoordinator(service)
    initial=coordinator.run_once(receipt['id'])
    delivery=service.get_task(receipt['id'])['deliveries'][0]
    return service,adapter,receipt,delivery,initial


@pytest.mark.parametrize('state',['platform_accepted','processing','reviewing','platform_scheduled'])
def test_failed_query_preserves_exact_confirmed_attempt_receipt_after_reopen(setup_v2,db_engine,state):
    service,adapter,receipt,delivery,initial=confirmed_query_case(setup_v2,state)
    assert initial==state
    with session_scope(db_engine) as s:
        row,item,history,attempt,*_=service.deliveries.load(s,delivery['id'])
        frozen=(row.attempt,row.request_digest,row.snapshot_json,attempt.final_json,attempt.result_json)
        original_result=json.loads(attempt.result_json)
    reopened=MultiPublishService(service.engine,service.paths,catalog=service.catalog,workers=service.deliveries.workers)
    reopened.recover_after_restart();coordinator=PublishCoordinator(reopened)
    for error in ('timeout','verification_required'):
        def reconcile(payload,*,context):
            adapter.calls.append(('reconcile',payload['video_id']))
            assert payload['receipt_reference']==original_result['receipt_reference']
            assert payload['scheduled_at']==delivery['scheduled_at']
            return AdapterOutcome('result_unknown',error_code=error)
        adapter.reconcile=reconcile
        assert coordinator.reconcile_delivery(delivery['id'])==state
        assert coordinator.run_once(receipt['id'],[delivery['id']]) is None
        with session_scope(db_engine) as s:
            row,item,history,attempt,*_=reopened.deliveries.load(s,delivery['id'])
            assert (row.attempt,row.request_digest,row.snapshot_json,attempt.final_json,attempt.result_json)==frozen
            assert item.state==history.state==state
            event=json.loads(attempt.recovery_json)['events'][-1]
            assert event['old_attempt']==row.attempt and event['old_result']==original_result
            assert event['retained_confirmed_result'] is True
            assert event['verified_result']['state']=='result_unknown' and event['verified_result']['error_code']==error
            assert event['verified_result']['request_digest']==row.request_digest
            if state=='platform_scheduled':assert original_result['scheduled_at']==row.scheduled_at
    assert [call[0] for call in adapter.calls]==['submit','reconcile','reconcile']


@pytest.mark.parametrize('old_state',['reviewing','platform_scheduled'])
@pytest.mark.parametrize('new_state',['published','platform_rejected'])
def test_confirmed_receipt_still_accepts_explicit_public_or_rejected_query(setup_v2,db_engine,old_state,new_state):
    service,adapter,receipt,delivery,_=confirmed_query_case(setup_v2,old_state)
    def reconcile(payload,*,context):
        adapter.calls.append(('reconcile',payload['video_id']))
        return AdapterOutcome(new_state,receipt_reference={'work_id':'confirmed-1'},error_code='platform_rejected' if new_state=='platform_rejected' else None)
    adapter.reconcile=reconcile;coordinator=PublishCoordinator(service)
    assert coordinator.reconcile_delivery(delivery['id'])==new_state
    assert coordinator.run_once(receipt['id'],[delivery['id']]) is None
    with session_scope(db_engine) as s:
        row,item,history,attempt,*_=service.deliveries.load(s,delivery['id'])
        assert item.state==history.state==json.loads(attempt.result_json)['state']==new_state
        assert json.loads(attempt.result_json)['receipt_reference']=={'work_id':'confirmed-1'}
        assert not json.loads(attempt.recovery_json)['events'][-1].get('retained_confirmed_result')
    assert [call[0] for call in adapter.calls]==['submit','reconcile']


def test_unknown_query_stays_unknown_even_with_a_reference_and_cannot_resubmit(setup_v2,db_engine):
    service,adapter,receipt,delivery,initial=confirmed_query_case(setup_v2,'result_unknown')
    assert initial=='result_unknown'
    adapter.reconcile=lambda payload,context:AdapterOutcome('result_unknown',error_code='timeout')
    coordinator=PublishCoordinator(service)
    assert coordinator.reconcile_delivery(delivery['id'])=='result_unknown'
    with session_scope(db_engine) as s:
        row,item,history,attempt,*_=service.deliveries.load(s,delivery['id'])
        assert item.state==history.state=='result_unknown'
        assert json.loads(attempt.result_json)['receipt_reference']=={'work_id':'confirmed-1'}
        assert not json.loads(attempt.recovery_json)['events'][-1].get('retained_confirmed_result')
    assert coordinator.run_once(receipt['id'],[delivery['id']]) is None
    assert [call[0] for call in adapter.calls]==['submit']


def test_formal_query_without_any_receipt_id_cannot_upgrade_unknown(setup_v2):
    service,adapter,receipt,delivery,_=confirmed_query_case(setup_v2,'result_unknown',reference={})
    coordinator=PublishCoordinator(service)
    for state in ('platform_accepted','processing','reviewing'):
        adapter.reconcile=lambda payload,context:AdapterOutcome(state)
        assert coordinator.reconcile_delivery(delivery['id'])=='result_unknown'
    assert coordinator.run_once(receipt['id'],[delivery['id']]) is None
    assert service.get_task(receipt['id'])['deliveries'][0]['receipt_summary']['reference']=={}
    assert [call[0] for call in adapter.calls]==['submit']


def test_mismatched_scheduled_receipt_is_not_a_confirmed_fact_to_preserve(setup_v2,db_engine):
    wrong=(datetime.now(timezone.utc)+timedelta(hours=6)).isoformat()
    service,adapter,receipt,delivery,initial=confirmed_query_case(setup_v2,'platform_scheduled',returned_time=wrong)
    assert initial=='result_unknown'
    adapter.reconcile=lambda payload,context:AdapterOutcome('result_unknown',error_code='timeout')
    coordinator=PublishCoordinator(service)
    assert coordinator.reconcile_delivery(delivery['id'])=='result_unknown'
    with session_scope(db_engine) as s:
        row,item,history,attempt,*_=service.deliveries.load(s,delivery['id'])
        assert row.scheduled_at!=wrong and item.state==history.state=='result_unknown'
        assert not json.loads(attempt.recovery_json)['events'][-1].get('retained_confirmed_result')
    assert coordinator.run_once(receipt['id'],[delivery['id']]) is None


def test_query_cannot_overwrite_a_concurrently_changed_same_attempt_receipt(setup_v2,db_engine):
    service,adapter,_,delivery,_=confirmed_query_case(setup_v2,'reviewing')
    def reconcile(payload,*,context):
        with session_scope(db_engine) as s:
            row,item,history,attempt,*_=service.deliveries.load(s,delivery['id'])
            current=json.loads(attempt.result_json);current['receipt_reference']={'work_id':'concurrent-confirmed'}
            attempt.result_json=json.dumps(current)
        return AdapterOutcome('result_unknown',error_code='timeout')
    adapter.reconcile=reconcile
    with pytest.raises(ValueError,match='recovery authority changed'):
        PublishCoordinator(service).reconcile_delivery(delivery['id'])
    with session_scope(db_engine) as s:
        row,item,history,attempt,*_=service.deliveries.load(s,delivery['id'])
        assert item.state==history.state=='reviewing'
        assert json.loads(attempt.result_json)['receipt_reference']=={'work_id':'concurrent-confirmed'}
        assert attempt.recovery_json is None

@pytest.mark.parametrize('previous',['pre_submit_failed','failed','published','platform_scheduled','result_unknown'])
def test_explicit_new_publish_intent_preserves_history_and_has_one_final_action(setup_v2,db_engine,previous):
    service,video,accounts,_=setup_v2
    old=PublishHistoryRepository(db_engine).record(video.content_hash,accounts[0].id,previous)
    prepared=service.prepare(request_id='manual-new',video_ids=[video.id],targets=[{'account_id':accounts[0].id,'platform':'douyin'}],common_fields={},execution_mode='publish_now',allow_new_request=True)
    assert prepared['excluded_count']==0
    receipt=service.save(prepared['prepared_id'],request_id='manual-new')
    assert service.save(prepared['prepared_id'],request_id='manual-new')==receipt
    d=service.get_task(receipt['id'])['deliveries'][0]
    claim=service.deliveries.claim(d['id'])
    assert claim['payload']['allow_new_request'] is True
    service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])
    with pytest.raises(ValueError):service.deliveries.mark_before_final(d['id'],claim['digest'],owner_token=claim['owner_token'])
    with session_scope(db_engine) as session:
        assert session.get(PublishHistoryRow,old.id).state==previous
        assert len(list(session.scalars(select(PublishDeliveryRow))))==1
    service.deliveries.workers.finished(claim['owner_hash'])
