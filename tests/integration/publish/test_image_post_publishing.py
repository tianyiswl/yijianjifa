"""Image task contracts use synthetic content and never contact a platform."""
from datetime import datetime, timedelta, timezone
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select, inspect, text, event

from yijianjifa.app_services.image_post_publishing import ImagePostPublishing
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation, outcome_evidence
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, PublishResourceRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.app_services.multi_publish_runtime import MultiPublishRuntime
from yijianjifa.storage.db import session_scope, create_engine_for, migrate_to_revision, migrate_to_head


class ImageAdapter:
    platform_id = 'douyin'
    content_type = 'image_post'
    verified_capabilities = {'ordinary', 'publish_now', 'platform_schedule', 'result_query'}

    def __init__(self):
        self.calls = []

    def validate_target(self, target, payload, mode):
        return AdapterValidation(target['platform_user_id'], True)

    def submit(self, payload, *, context, before_final):
        self.calls.append(payload)
        before_final()
        return AdapterOutcome('platform_scheduled' if payload['execution_mode'] == 'platform_schedule' else 'published',
            scheduled_at=payload['scheduled_at'])

    def reconcile(self, payload, *, context):
        return AdapterOutcome('published')


@pytest.fixture
def image_setup(db_engine, app_paths, tmp_path):
    account = AccountService(db_engine).create('douyin', 'image-user', '图文账号')
    image = tmp_path / 'one.jpg'
    image.write_bytes(b'synthetic-image')
    ref = SimpleNamespace(id='image-one', position=0, path=image, content_hash=sha256_file(image),
        width=100, height=200, format='JPEG', original_name='one.jpg')
    record = SimpleNamespace(id='post-one', title='产品介绍', body='正文', tags='#产品',
        images=[ref], content_hash='post-hash')
    adapter = ImageAdapter()
    runtime = MultiPublishRuntime(db_engine, app_paths, adapters={'douyin:image_post': adapter})
    runtime.wake = lambda: None
    publisher = ImagePostPublishing(db_engine, app_paths, runtime)
    publisher.posts = SimpleNamespace(get=lambda pid: record if pid == record.id else None)
    params = dict(post_ids=[record.id], account_id=account.id, mode='immediate', start=None,
        interval_hours=3, daily_count=1, days=1)
    return publisher, params, record, account, adapter, runtime


def test_image_publish_freezes_all_images_and_has_no_fake_video_fields(image_setup, db_engine):
    publisher, p, record, account, adapter, runtime = image_setup
    result = publisher.submit(p, 'image-request')
    with session_scope(db_engine) as s:
        row = s.scalar(select(PublishDeliveryRow))
        payload = json.loads(row.snapshot_json)
        assert row.content_type == payload['content_type'] == 'image_post'
        assert row.content_id == record.id
        assert row.video_id is None and row.video_hash is None
        assert 'video_id' not in payload and 'video_hash' not in payload and 'video_path' not in payload
        assert len(payload['images']) == 1
        frozen = payload['images'][0]
        assert frozen['path'] != str(record.images[0].path)
        assert frozen['content_hash'] == record.images[0].content_hash and frozen['position'] == 0
        assert s.scalar(select(PublishHistoryRow)).video_hash is None
    record.images[0].path.write_bytes(b'edited-original')
    assert runtime.coordinator.run_once(result['task_id']) == 'published'
    assert len(adapter.calls) == 1
    assert runtime.detail(result['task_id'])['state'] == 'completed'
    assert runtime.detail(result['task_id'])['summary']['image_posts'] == 1
    assert runtime.detail(result['task_id'])['summary']['videos'] == 0


@pytest.mark.parametrize('title', ['字' * 21, '😀' * 11])
def test_image_title_over_platform_limit_is_rejected_before_task_or_upload(image_setup, db_engine, title):
    publisher, params, record, _, adapter, _ = image_setup
    record.title = title
    preview = publisher.preview(params)
    assert preview['ready'] is False
    assert preview['issues'][0]['code'] == 'image_post_title_too_long'
    assert preview['issues'][0]['message'] == '图文标题最多20字，请缩短后再发布。'
    with pytest.raises(ValueError, match='image_post_title_too_long'):
        publisher.submit(params, 'long-image-title')
    assert record.title == title
    assert adapter.calls == []
    with session_scope(db_engine) as session:
        assert session.scalar(select(PublishDeliveryRow)) is None


def test_image_title_exactly_20_characters_remains_valid_without_truncation(image_setup):
    publisher, params, record, _, _, _ = image_setup
    record.title = '字' * 20
    preview = publisher.preview(params)
    assert preview['ready'] is True
    assert preview['items'][0]['title'] == record.title


def test_same_button_request_is_idempotent_and_explicit_repeat_uses_new_task(image_setup):
    publisher, p, _, _, adapter, runtime = image_setup
    first = publisher.submit(p, 'same-button')
    assert publisher.submit(p, 'same-button') == first
    assert runtime.coordinator.run_once(first['task_id']) == 'published'
    repeated = publisher.submit(p, 'another-click')
    assert repeated['task_id'] != first['task_id']
    assert runtime.coordinator.run_once(repeated['task_id']) == 'published'
    assert len(adapter.calls) == 2
    changed = {**p, 'interval_hours': 4}
    with pytest.raises(ValueError, match='request'):
        publisher.submit(changed, 'same-button')


def test_platform_schedule_is_frozen_exact_and_never_immediate(image_setup, db_engine):
    publisher, p, _, _, adapter, runtime = image_setup
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
    result = publisher.submit({**p, 'mode': 'exact', 'start': when.isoformat()}, 'future-image')
    assert runtime.coordinator.run_once(result['task_id']) == 'platform_scheduled'
    assert adapter.calls[0]['execution_mode'] == 'platform_schedule'
    assert adapter.calls[0]['scheduled_at'] == when.isoformat()


def test_video_transport_is_never_used_for_image_posts(db_engine, app_paths, image_setup):
    publisher, p, _, _, adapter, runtime = image_setup
    runtime.catalog._transports.pop('douyin:image_post')
    runtime.catalog._transports['douyin'] = adapter
    preview = publisher.preview(p)
    assert preview['ready'] is False
    assert any(i['code'] == 'adapter_not_integrated' for i in preview['issues'])
    with pytest.raises(ValueError):
        publisher.submit(p, 'wrong-transport')


def test_invalid_login_timezone_and_resources_do_not_create_task(image_setup, db_engine):
    publisher, p, record, account, _, runtime = image_setup
    with pytest.raises(ValueError):
        publisher.submit({**p, 'mode': 'exact', 'start': '2026-12-01T12:00:00'}, 'no-zone')
    with session_scope(db_engine) as s:
        s.get(AccountRow, account.id).login_state = 'expired'
    assert publisher.preview(p)['ready'] is False
    with pytest.raises(ValueError):
        publisher.submit(p, 'bad-account')
    with session_scope(db_engine) as s:
        s.get(AccountRow, account.id).login_state = 'valid'
    record.images[0].path.unlink()
    assert publisher.preview(p)['ready'] is False
    with pytest.raises(ValueError):
        publisher.submit(p, 'missing-image')


def test_settings_independent_and_cancel_delete_reuses_task_records(image_setup, db_engine):
    publisher, p, _, account, _, runtime = image_setup
    saved = publisher.save_settings({k: v for k, v in p.items() if k != 'post_ids'})
    assert saved['account_id'] == account.id
    assert 'post_ids' not in publisher.context()['settings']
    result = publisher.submit(p, 'cancel-image')
    child = runtime.detail(result['task_id'])['deliveries'][0]
    runtime.service.cancel_delivery(child['id'])
    assert runtime.service.delivery_state(child['id']) == 'cancelled'
    with session_scope(db_engine) as s:
        assert s.scalar(select(PublishResourceRow)).cleaned is True


def test_image_tasks_support_the_existing_simple_task_details_and_log_copy(image_setup, db_engine):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.features.publish.execution_log import append_event
    publisher, p, _, _, _, runtime = image_setup
    result = publisher.submit(p, 'task-details-image')
    access = TaskCenterAccess(db_engine, multi_publish=runtime)
    row = access.list('', 'publish', '', 0, 20)['items'][0]
    assert row['name'] == '抖音图文发布' and row['content_type'] == 'image_post'
    detail = access.detail(result['task_id'])
    assert detail['items'][0]['image_count'] == 1
    append_event(db_engine, detail['items'][0]['id'], 1, 'douyin.image_upload', {'image_count':1})
    copied = access.execution_log_text(result['task_id'])
    assert '图文：产品介绍' in copied and 'douyin.image_upload' in copied
    assert 'synthetic-image' not in copied


def test_unknown_image_result_never_automatically_resubmits_and_is_reconciled_by_image_route(image_setup):
    publisher, p, _, _, adapter, runtime = image_setup
    def uncertain(payload, *, context, before_final):
        adapter.calls.append(payload)
        before_final()
        return AdapterOutcome('result_unknown')
    adapter.submit = uncertain
    result = publisher.submit(p, 'ambiguous-image')
    assert runtime.coordinator.run_once(result['task_id']) == 'result_unknown'
    assert publisher.submit(p, 'ambiguous-image') == result
    assert runtime.coordinator.run_once(result['task_id']) is None
    assert len(adapter.calls) == 1
    did = runtime.detail(result['task_id'])['deliveries'][0]['id']
    assert runtime.coordinator.reconcile_delivery(did) == 'published'
    assert len(adapter.calls) == 1


def test_every_image_is_frozen_in_order_and_can_be_deleted_through_existing_task_command(image_setup, db_engine, tmp_path):
    from yijianjifa.app_services.multi_publish_commands import MultiPublishCommands
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    from yijianjifa.features.publish.deliveries import ResourceConsumerRow
    publisher, p, record, _, _, runtime = image_setup
    path = tmp_path / 'two.png'
    path.write_bytes(b'second-image')
    record.images.append(SimpleNamespace(id='second-image',position=1,path=path,content_hash=sha256_file(path),
        width=300,height=400,format='PNG',original_name='two.png'))
    result = publisher.submit(p, 'two-images')
    with session_scope(db_engine) as s:
        payload = json.loads(s.scalar(select(PublishDeliveryRow)).snapshot_json)
        assert [i['image_id'] for i in payload['images']] == ['image-one','second-image']
        assert [i['position'] for i in payload['images']] == [0,1]
        assert len(list(s.scalars(select(PublishResourceRow)))) == 2
        assert len(list(s.scalars(select(ResourceConsumerRow)))) == 2
    commands = MultiPublishCommands(db_engine,runtime,declarations=None,locations=None)
    deleted = commands.delete_v2({'id':result['task_id']}, 'delete-two-images')
    assert deleted['ok'] and deleted['data']['deleted'] is True
    assert TaskCenterAccess(db_engine,multi_publish=runtime).list('', 'publish', '', 0, 20)['total'] == 0
    assert runtime.coordinator.run_once(result['task_id']) is None
    assert record.images[0].path.is_file() and path.is_file()
    with session_scope(db_engine) as s:
        assert all(row.cleaned for row in s.scalars(select(PublishResourceRow)))


@pytest.mark.parametrize('legacy', ['broken', json.dumps({
    'started_at':'2020-01-01T00:00:00+00:00', 'expires_at':'2021-01-01T00:00:00+00:00',
    'last_seen_at':'2026-01-01T00:00:00+00:00'})])
def test_legacy_term_allows_image_claim_and_final_submission(image_setup,db_engine,legacy):
    from yijianjifa.app_services.use_term import UseTerm
    from yijianjifa.storage.models import Setting
    publisher, p, _, _, adapter, runtime = image_setup
    with session_scope(db_engine) as session:
        session.add(Setting(key=UseTerm.KEY,value_json=legacy))
    result = publisher.submit(p, 'free-image')
    assert result['quantity'] == 1
    assert runtime.coordinator.run_once(result['task_id']) == 'published'
    assert len(adapter.calls) == 1
    with session_scope(db_engine) as session:
        assert session.get(Setting,UseTerm.KEY).value_json == legacy


def test_generic_image_runtime_guard_still_blocks_new_tasks(image_setup):
    publisher, p, _, _, _, _ = image_setup
    publisher.can_start = lambda: False
    assert publisher.preview(p)['ready'] is False
    with pytest.raises(ValueError,match='publication_unavailable'):
        publisher.submit(p, 'runtime-disabled')


def test_first_settings_default_to_tomorrow_noon_and_saved_date_is_unchanged(image_setup):
    publisher, p, _, _, _, _ = image_setup
    settings = publisher.context()['settings']
    tomorrow = datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days=1)
    assert settings['mode'] == 'exact'
    start = datetime.fromisoformat(settings['start'])
    assert start.date() == tomorrow and (start.hour,start.minute) == (12,0)
    saved = {k:v for k,v in p.items() if k != 'post_ids'}
    saved.update(mode='exact',start='2026-10-20T16:30:00+08:00')
    publisher.save_settings(saved)
    assert publisher.context()['settings']['start'] == saved['start']


def test_packaged_windows_context_does_not_require_an_iana_timezone_database(image_setup):
    from zoneinfo import ZoneInfoNotFoundError
    publisher, _, _, _, _, _ = image_setup
    with patch('yijianjifa.app_services.image_post_publishing.ZoneInfo', create=True,
               side_effect=ZoneInfoNotFoundError('no packaged IANA database')):
        context = publisher.context()
    start = datetime.fromisoformat(context['settings']['start'])
    assert start.utcoffset() == timedelta(hours=8)
    assert (start.hour,start.minute) == (12,0)


def test_real_image_adapter_scheduled_receipt_is_accepted_by_the_coordinator(image_setup, monkeypatch):
    from yijianjifa.webui.douyin_image_publish import DouyinImagePostAdapter, legacy
    publisher, p, _, account, adapter, runtime = image_setup
    actual_adapter = object.__new__(DouyinImagePostAdapter)
    actual_adapter.refreshed = {}
    actual_adapter._account = lambda _session: account
    monkeypatch.setattr(legacy, 'session_blob', lambda _context, _uid: b'synthetic-owned-session')
    def scheduled(payload, *, context, before_final):
        before_final()
        session = SimpleNamespace(payload=payload, context=object(), original_ref='synthetic-ref')
        return actual_adapter._outcome(session, '1234567890')
    adapter.submit = scheduled
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0,microsecond=0)
    result = publisher.submit({**p,'mode':'exact','start':when.isoformat()}, 'real-image-receipt-contract')
    assert runtime.coordinator.run_once(result['task_id']) == 'platform_scheduled'
    detail = runtime.detail(result['task_id'])
    assert detail['state'] == 'completed'
    assert detail['deliveries'][0]['receipt_summary']['reference'] == {'kind':'image_post','item_id':'1234567890'}


@pytest.mark.parametrize('reference', [{'kind':'image_post','item_id':''}, {'kind':'video','item_id':'12345'},
    {'kind':'image_post','item_id':'12345','cookie':'private'}, {'kind':'image_post'}, {'item_id':'12345'}])
def test_image_receipt_references_require_a_complete_bounded_image_identity(reference):
    with pytest.raises(ValueError):
        outcome_evidence(AdapterOutcome('published', receipt_reference=reference), 'synthetic-digest')


@pytest.mark.parametrize('state,label', [('platform_accepted','平台已接收'),('processing','平台处理中'),('reviewing','平台审核中')])
def test_received_image_posts_complete_local_delivery_without_claiming_publication_or_action_badge(image_setup, db_engine, state, label):
    from yijianjifa.app_services.task_center_access import TaskCenterAccess
    publisher, p, _, _, adapter, runtime = image_setup
    def received(payload, *, context, before_final):
        before_final()
        return AdapterOutcome(state,receipt_reference={'kind':'image_post','item_id':'12345'})
    adapter.submit = received
    result = publisher.submit(p, 'received-image-'+state)
    assert runtime.coordinator.run_once(result['task_id']) == state
    detail = runtime.detail(result['task_id'])
    assert detail['state'] == 'completed'
    assert detail['completed'] == 1 and detail['accepted_count'] == 1
    assert detail['published_count'] == 0 and detail['status_label'] == label
    assert detail['deliveries'][0]['reason_message'] == ''
    access = TaskCenterAccess(db_engine,multi_publish=runtime)
    assert access.list('', 'action', '', 0, 20)['total'] == 0
    assert access.list('', 'publish', 'done', 0, 20)['items'][0]['status_label'] == label


@pytest.mark.parametrize('state',['platform_accepted','processing','reviewing'])
def test_video_receipts_share_local_completion_without_changing_the_platform_state(db_engine, state):
    from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
    from yijianjifa.features.publish.normal_service import PublishTaskRow
    from yijianjifa.features.publish.deliveries import PublishDeliveries
    with session_scope(db_engine) as s:
        snapshot = json.dumps({'version':2,'kind':'multi_publish'})
        s.add(TaskCenterRow(id='old-video-task',kind='multi_publish',name='旧视频',state='waiting',snapshot_json=snapshot))
        s.add(PublishTaskRow(id='old-video-task',platform='mixed',state='waiting',snapshot_json=snapshot))
        s.flush()
        s.add(TaskItemRow(id='old-video-item',task_id='old-video-task',item_key='old-video-delivery',state=state,detail_json='{}'))
        s.flush()
        PublishDeliveries.aggregate(s, 'old-video-task')
        assert s.get(TaskCenterRow, 'old-video-task').state == 'completed'
        assert s.get(PublishTaskRow, 'old-video-task').state == 'completed'
        assert s.get(TaskItemRow, 'old-video-item').state == state
        assert s.get(TaskCenterRow, 'old-video-task').snapshot_json == snapshot


def test_content_migration_retains_video_snapshots_histories_and_foreign_keys(tmp_path):
    engine = create_engine_for(tmp_path / 'old-video.db')
    @event.listens_for(engine, 'connect')
    def enforce_foreign_keys(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
    migrate_to_revision(engine, '0016_publish_deliveries')
    old = '{"version":2,"kind":"multi_publish","accounts":[]}'
    with engine.begin() as c:
        c.execute(text("INSERT INTO publish_tasks(id,platform,state,snapshot_json,created_at) VALUES ('task','mixed','waiting',:snapshot,CURRENT_TIMESTAMP)"), {'snapshot':old})
        c.execute(text("INSERT INTO task_center_records(id,kind,name,state,snapshot_json,queue_order,created_at,updated_at) VALUES ('task','multi_publish','旧视频','waiting',:snapshot,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"), {'snapshot':old})
        c.execute(text("INSERT INTO task_center_items(id,task_id,item_key,state,detail_json) VALUES ('item','task','delivery','waiting','{}')"))
        c.execute(text("INSERT INTO publish_history(id,video_hash,account_id,attempt,state,task_id,created_at) VALUES ('history','video-hash','account',1,'waiting','task',CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO publish_deliveries(id,task_id,item_id,history_id,platform,account_id,platform_user_id,video_id,video_hash,snapshot_json,request_digest,execution_mode,attempt,position,hidden,cancel_requested) VALUES ('delivery','task','item','history','douyin','account','uid','video','video-hash',:snapshot,'digest','publish_now',1,0,0,0)"), {'snapshot':old})
    migrate_to_head(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
        assert c.exec_driver_sql('PRAGMA foreign_key_check').first() is None
        assert c.execute(text("SELECT snapshot_json FROM publish_tasks WHERE id='task'")).scalar_one() == old
        assert c.execute(text("SELECT content_type,content_id,content_hash FROM publish_deliveries")).one() == ('video','video','video-hash')
        assert c.execute(text("SELECT video_hash,state,content_type,content_id,content_hash FROM publish_history")).one() == ('video-hash','waiting','video','video','video-hash')
    assert next(col for col in inspect(engine).get_columns('publish_deliveries') if col['name']=='video_id')['nullable'] is True


def test_same_image_group_with_different_copy_can_execute_serially_in_one_batch(image_setup):
    publisher, p, first, _, adapter, runtime = image_setup
    second = copy.copy(first)
    second.id, second.title = 'post-two', '另一个产品标题'
    records = {first.id:first, second.id:second}
    publisher.posts = SimpleNamespace(get=records.get)
    params = {**p, 'post_ids':list(records)}
    preview = publisher.preview(params)
    assert preview['ready'] is True
    result = publisher.submit(params, 'same-images-different-copy')
    assert runtime.coordinator.run_once(result['task_id']) == 'published'
    assert runtime.coordinator.run_once(result['task_id']) == 'published'
    assert len(adapter.calls) == 2
    assert adapter.calls[0]['content_hash'] != adapter.calls[1]['content_hash']
    assert runtime.detail(result['task_id'])['state'] == 'completed'


def test_exact_duplicate_posts_in_one_batch_are_explained_before_creating_tasks(image_setup, db_engine):
    publisher, p, first, _, _, _ = image_setup
    second = copy.copy(first)
    second.id = 'post-two'
    publisher.posts = SimpleNamespace(get={first.id:first, second.id:second}.get)
    params = {**p,'post_ids':[first.id,second.id]}
    preview = publisher.preview(params)
    assert preview['ready'] is False
    assert '图片和文案完全相同' in preview['issues'][0]['message']
    with pytest.raises(ValueError, match='duplicate_image_post_content'):
        publisher.submit(params, 'exact-duplicates')
    with session_scope(db_engine) as s:
        assert list(s.scalars(select(PublishDeliveryRow))) == []
