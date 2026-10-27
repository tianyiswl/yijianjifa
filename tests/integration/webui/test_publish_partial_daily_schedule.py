"""A daily schedule is a capacity for ordinary publish, not a required quantity."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from test_publish_preparation import setup
from yijianjifa.app_services.commerce_planning import CommercePlanning
from yijianjifa.app_services.publish_confirmation import PublishConfirmation
from yijianjifa.app_services.publish_planning import PublishPlanning
from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.storage.db import session_scope


def add_account(engine):
    service = AccountService(engine)
    account = service.create('douyin', 'second-fixture-identity', '第二账号')
    with session_scope(engine) as session:
        session.get(AccountRow, account.id).session_ref = 'second-fixture-only-session'
    service.update_capabilities(account.id, CapabilitySnapshot(ordinary='available'))
    return account


def add_video(engine, tmp_path, index):
    source = tmp_path / f'partial-{index}.mp4'
    source.write_bytes(f'partial-fixture-{index}'.encode())
    return FinishedVideoService(engine).register_generated(source, duration_ms=3000, title=f'视频{index}')


def test_one_video_can_prepare_and_confirm_first_slot_of_five_day_capacity(db_engine, app_paths, tmp_path):
    prep, account, video, params, now = setup(db_engine, app_paths, tmp_path)
    params.update(daily_count=2, days=5)
    preview = PublishPlanning(db_engine).preview(params, now=now)
    assert preview['locally_valid'] and preview['issues'] == []
    assert [(item['video_id'], item['time']) for item in preview['items']] == [(video.id, params['start'])]
    with session_scope(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(PublishTaskRow)) == 0

    prepared = prep.prepare(params, now=now)
    assert prepared['token'] and not prepared['submitted']
    original = prep.resolve(prepared['token'], now=now).data
    params.update(daily_count=1, days=1, start=(now + timedelta(days=2)).isoformat())
    assert prep.resolve(prepared['token'], now=now).data == original
    result = PublishConfirmation(db_engine, app_paths, prep).confirm(prepared['token'], now=now)
    assert result['quantity'] == 1 and result['state'] == 'paused' and not result['submitted']
    with session_scope(db_engine) as session:
        row = session.get(PublishTaskRow, result['id'])
        frozen_json = row.snapshot_json
        frozen = json.loads(frozen_json)
        assert row.state == session.get(TaskCenterRow, result['id']).state == 'paused'
        assert frozen['params']['daily_count'] == 2 and frozen['params']['days'] == 5
        assert len(frozen['items']) == 1 and frozen['items'][0]['time'] == original['items'][0]['time']
        assert session.scalar(select(func.count()).select_from(PublishTaskRow)) == 1
        assert session.scalar(select(func.count()).select_from(TaskItemRow)) == 1
        history = session.scalars(select(PublishHistoryRow)).one()
        assert history.task_id == result['id'] and history.account_id == account.id and history.state == 'queued'

    FinishedVideoService(db_engine).update_metadata(video.id, title='之后修改', body='', tags='')
    other = add_video(db_engine, tmp_path, 2)
    params['video_ids'] = [other.id]
    assert prep.prepare(params, now=now)['token']
    with session_scope(db_engine) as session:
        assert session.get(PublishTaskRow, result['id']).snapshot_json == frozen_json
        assert session.scalar(select(func.count()).select_from(PublishTaskRow)) == 1


@pytest.mark.parametrize('distribution', ['split_across_accounts', 'whole_batch'])
def test_partial_daily_capacity_uses_each_accounts_first_actual_slots(db_engine, app_paths, tmp_path, distribution):
    prep, account, first, params, now = setup(db_engine, app_paths, tmp_path)
    other = add_account(db_engine)
    videos = [first, add_video(db_engine, tmp_path, 2), add_video(db_engine, tmp_path, 3)]
    params.update(account_ids=[account.id, other.id], video_ids=[video.id for video in videos],
                  distribution=distribution, daily_count=2, days=5)
    result = prep.prepare(params, now=now)
    assert result['token'] and result['issues'] == []
    start = datetime.fromisoformat(params['start'])
    for target in (account.id, other.id):
        items = [item for item in result['items'] if item['account_id'] == target]
        assigned = videos if distribution == 'whole_batch' else videos[::2] if target == account.id else videos[1::2]
        assert [item['video_id'] for item in items] == [video.id for video in assigned]
        expected = [start + timedelta(days=index // 2, hours=(index % 2) * 3) for index in range(len(assigned))]
        assert [item['time'] for item in items] == [when.isoformat() for when in expected]
    assert len(result['items']) == (6 if distribution == 'whole_batch' else 3)


@pytest.mark.parametrize('manual_move', [False, True])
def test_daily_account_without_assigned_video_still_blocks_preparation(db_engine, app_paths, tmp_path, manual_move):
    prep, account, first, params, now = setup(db_engine, app_paths, tmp_path)
    other = add_account(db_engine)
    params.update(account_ids=[account.id, other.id], daily_count=2, days=5)
    empty_account = other.id
    if manual_move:
        second = add_video(db_engine, tmp_path, 2)
        params.update(video_ids=[first.id, second.id], moves={first.id: other.id})
        empty_account = account.id
    result = prep.prepare(params, now=now)
    assert result['token'] is None and not result['locally_valid']
    assert result['issues'] == [dict(account_id=empty_account, code='account_has_no_videos')]
    assert len(result['items']) == (2 if manual_move else 1)


@pytest.mark.parametrize('capability', ['ordinary', 'commerce'])
def test_daily_over_capacity_keeps_all_items_and_remains_blocked(db_engine, app_paths, tmp_path, capability):
    _, account, first, params, now = setup(db_engine, app_paths, tmp_path)
    AccountService(db_engine).update_capabilities(account.id, CapabilitySnapshot(ordinary='available', commerce='available'))
    second = add_video(db_engine, tmp_path, 2)
    params['video_ids'] = [first.id, second.id]
    result = PublishPlanning(db_engine).preview(params, now=now, capability=capability)
    assert not result['locally_valid']
    assert result['issues'] == [dict(account_id=account.id, code='schedule_overfilled', expected=1, actual=2)]
    assert [item['video_id'] for item in result['items']] == [first.id, second.id]
    assert 'schedule_capacity_exceeded' in result['items'][1]['blockers']


def test_commerce_daily_capacity_still_requires_its_full_quantity(db_engine, app_paths, tmp_path):
    _, account, _, params, now = setup(db_engine, app_paths, tmp_path)
    params.update(daily_count=2, days=5)
    params.pop('account_ids')
    params.pop('distribution')
    result = CommercePlanning(db_engine).preview(dict(account_id=account.id, plan=params,
                                                     store_pool=[], manual_store_overrides={}), now=now)
    assert not result['locally_valid']
    assert result['issues'] == [dict(account_id=account.id, code='schedule_underfilled', expected=10, actual=1)]


@pytest.mark.parametrize('offset, blocker', [(timedelta(hours=1), 'before_schedule_window'),
                                           (timedelta(days=14), 'beyond_platform_window')])
def test_partial_capacity_does_not_relax_actual_publish_time_limits(db_engine, app_paths, tmp_path, offset, blocker):
    prep, _, _, params, now = setup(db_engine, app_paths, tmp_path)
    params.update(daily_count=2, days=5, start=(now + offset).isoformat())
    result = prep.prepare(params, now=now)
    assert result['token'] is None and not result['locally_valid']
    assert result['issues'] == []
    assert blocker in result['items'][0]['blockers']
