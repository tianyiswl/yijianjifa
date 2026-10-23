import json

import pytest

from yijianjifa.app_services.publish_planning import PublishPlanning
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.history import DuplicatePublishBlocked, PublishHistoryRepository
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.storage.db import session_scope


def setup(engine, tmp_path):
    accounts = AccountService(engine)
    a = accounts.create('douyin', 'same-private-uid', '同名账号')
    b = accounts.create('kuaishou', 'same-private-uid', '同名账号')
    accounts.set_default('douyin', a.id)
    path = tmp_path / 'target.mp4'
    path.write_bytes(b'target candidate')
    video = FinishedVideoService(engine).import_external(path, duration_ms=1000)
    return a, b, video


def candidates(engine, ids, **options):
    return PublishPlanning(engine).candidates(query='', offset=0, limit=20, account_ids=ids, **options)


@pytest.mark.parametrize('state', ['published', 'queued', 'running', 'platform_scheduled',
                                  'result_unknown', 'pre_submit_failed', 'paused_auth', 'processing', 'reviewing', 'failed'])
def test_other_platform_first_delivery_remains_eligible_when_selected_sibling_is_blocked(db_engine, tmp_path, state):
    a, b, video = setup(db_engine, tmp_path)
    PublishHistoryRepository(db_engine).record(video.content_hash, a.id, state)
    result = candidates(db_engine, [a.id, b.id])
    assert result['total'] == 1
    row = result['items'][0]
    assert row['eligible_account_ids'] == [b.id]
    assert row['targets'] == [
        {'account_id': a.id, 'platform': 'douyin', 'states': [state], 'eligible': False},
        {'account_id': b.id, 'platform': 'kuaishou', 'states': [], 'eligible': True}]
    public = json.dumps(result)
    assert a.platform_user_id not in public and 'session_ref' not in public


def test_all_selected_targets_blocked_are_excluded_even_when_showing_published(db_engine, tmp_path):
    a, b, video = setup(db_engine, tmp_path)
    history = PublishHistoryRepository(db_engine)
    history.record(video.content_hash, a.id, 'published')
    history.record(video.content_hash, b.id, 'result_unknown')
    assert candidates(db_engine, [a.id, b.id], advanced=True, include_published=True)['items'] == []
    assert candidates(db_engine, [b.id], advanced=True, include_published=True)['items'] == []


def test_v1_candidates_keep_original_global_filter_and_response_shape(db_engine, tmp_path):
    a, b, video = setup(db_engine, tmp_path)
    planner = PublishPlanning(db_engine)
    original = planner.candidates(query='', offset=0, limit=20)
    assert set(original) == {'items', 'total', 'default_account_id'}
    assert 'targets' not in original['items'][0]
    PublishHistoryRepository(db_engine).record(video.content_hash, a.id, 'published')
    assert planner.candidates(query='', offset=0, limit=20)['items'] == []
    shown = planner.candidates(query='', offset=0, limit=20, advanced=True, include_published=True)
    assert shown['items'][0]['id'] == video.id and shown['items'][0]['published'] is True
    assert candidates(db_engine, [b.id])['total'] == 1


@pytest.mark.parametrize('invalid', [[], ['missing'], ['DUPLICATE'], 'account', [True]])
def test_target_candidate_selection_rejects_invalid_or_duplicate_accounts(db_engine, tmp_path, invalid):
    a, _, _ = setup(db_engine, tmp_path)
    ids = [a.id, a.id] if invalid == ['DUPLICATE'] else invalid
    with pytest.raises((ValueError, KeyError)):
        candidates(db_engine, ids)


def test_unknown_or_removed_platform_account_cannot_be_used_as_target(db_engine, tmp_path):
    a, _, _ = setup(db_engine, tmp_path)
    unknown = AccountService(db_engine).create('wechat', 'unknown', '未知平台')
    with pytest.raises(ValueError):
        candidates(db_engine, [unknown.id])
    AccountService(db_engine).remove(a.id)
    with pytest.raises((ValueError, KeyError)):
        candidates(db_engine, [a.id])


@pytest.mark.parametrize('state', ['published', 'result_unknown'])
def test_readded_identity_cannot_lose_history_when_old_account_metadata_was_deleted(db_engine, tmp_path, state):
    a, b, video = setup(db_engine, tmp_path)
    history = PublishHistoryRepository(db_engine)
    history.record(video.content_hash, a.id, state, task_id='frozen')
    snapshot = {'version': 1, 'platform': 'douyin', 'accounts': [
        {'id': a.id, 'platform': 'douyin', 'platform_user_id': a.platform_user_id}]}
    with session_scope(db_engine) as session:
        session.add(PublishTaskRow(id='frozen', platform='douyin', state=state,
                                  snapshot_json=json.dumps(snapshot)))
        session.delete(session.get(AccountRow, a.id))
    readded = AccountService(db_engine).create('douyin', a.platform_user_id, '重新添加')
    assert readded.id != a.id
    result = candidates(db_engine, [readded.id, b.id])
    assert result['items'][0]['eligible_account_ids'] == [b.id]
    assert result['items'][0]['targets'][0]['states'] == [state]
    assert history.states_for_target(video.content_hash, readded.id) == {state}
    with pytest.raises(DuplicatePublishBlocked):
        history.assert_target_can_create(video.content_hash, readded.id)
    if state == 'published':
        history.assert_target_can_create(video.content_hash, readded.id, allow_repeat_success=True)
    else:
        with pytest.raises(DuplicatePublishBlocked):
            history.assert_target_can_create(video.content_hash, readded.id, allow_repeat_success=True)


def test_unresolved_orphan_without_identity_evidence_requires_verification(db_engine, tmp_path):
    a, b, video = setup(db_engine, tmp_path)
    PublishHistoryRepository(db_engine).record(video.content_hash, 'deleted-account', 'result_unknown')
    assert candidates(db_engine, [a.id, b.id])['items'] == []
