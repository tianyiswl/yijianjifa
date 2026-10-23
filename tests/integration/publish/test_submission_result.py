import json

import pytest
from sqlalchemy import select

from yijianjifa.storage.db import session_scope, create_engine_for
from yijianjifa.storage.models import Setting
from yijianjifa.features.publish.executor import SubmissionEvidence
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.publish.task_submission_boundary import TaskSubmissionBoundary
from yijianjifa.features.publish.submission_result import SubmissionResultStore
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.features.tasks.service import TaskIssueRow
from yijianjifa.features.notifications.service import NotificationRow
from test_task_submission_boundary import seed


def setup_claim(engine, *, final=True):
    seed(engine)
    boundary = TaskSubmissionBoundary(engine)
    claim = boundary.claim('T', 'I')
    if final:
        boundary.mark_before_final('T', 'I', claim['digest'], owner_token=claim['owner_token'])
    return dict(owner_token=claim['owner_token'], expected_digest=claim['digest'])


def states(engine):
    with session_scope(engine) as s:
        return [s.get(model, key).state for model, key in (
            (PublishTaskRow, 'T'), (TaskCenterRow, 'T'), (TaskItemRow, 'I'))] + [s.scalar(select(PublishHistoryRow)).state]


@pytest.mark.parametrize('evidence,state', [
    (SubmissionEvidence(True, public_result='published'), 'published'),
    (SubmissionEvidence(True, public_result='rejected'), 'platform_rejected'),
    (SubmissionEvidence(True, error_code='timeout'), 'result_unknown'),
    (SubmissionEvidence(False, error_code='late_error'), 'result_unknown'),
    (SubmissionEvidence(True, platform_schedule_confirmation='wrong'), 'result_unknown'),
])
def test_final_result_is_atomic_and_does_not_remove_marker(db_engine, evidence, state):
    claim = setup_claim(db_engine)
    store = SubmissionResultStore(db_engine)
    assert store.record('T', 'I', **claim, evidence=evidence) == state
    assert states(db_engine) == [state] * 4
    assert store.record('T', 'I', **claim, evidence=evidence) == state
    with session_scope(db_engine) as s:
        assert len(list(s.scalars(select(Setting).where(Setting.key.like('publish.final.%'))))) == 1
        assert len(list(s.scalars(select(NotificationRow)))) == 1
        issues = list(s.scalars(select(TaskIssueRow)))
        assert len(issues) == (0 if state in {'published', 'platform_scheduled'} else 1)
        for issue in issues:
            assert all((issue.stage, issue.reason, issue.impact, issue.next_action, issue.occurred_at))
    with pytest.raises(ValueError):
        TaskSubmissionBoundary(db_engine).claim('T', 'I')


@pytest.mark.parametrize('evidence,state', [
    (SubmissionEvidence(False, error_code='identity_mismatch'), 'pre_submit_failed'),
    (SubmissionEvidence(False, auth_required=True), 'paused_auth'),
])
def test_pre_submit_failure_never_claims_success(db_engine, evidence, state):
    claim = setup_claim(db_engine, final=False)
    assert SubmissionResultStore(db_engine).record('T', 'I', **claim, evidence=evidence) == state
    assert states(db_engine) == [state] * 4


def test_success_without_final_marker_is_rejected(db_engine):
    claim = setup_claim(db_engine, final=False)
    with pytest.raises(ValueError, match='no durable marker'):
        SubmissionResultStore(db_engine).record('T', 'I', **claim, evidence=SubmissionEvidence(True, public_result='published'))
    assert states(db_engine) == ['running', 'running', 'running', 'queued']


def test_conflicting_receipt_cannot_overwrite_unknown(db_engine):
    claim = setup_claim(db_engine)
    store = SubmissionResultStore(db_engine)
    store.record('T', 'I', **claim, evidence=SubmissionEvidence(True))
    with pytest.raises(ValueError, match='reconciliation'):
        store.record('T', 'I', **claim, evidence=SubmissionEvidence(True, public_result='published'))
    assert states(db_engine) == ['result_unknown'] * 4


def test_matching_platform_schedule_receipt(db_engine):
    seed(db_engine)
    with session_scope(db_engine) as s:
        task = s.get(PublishTaskRow, 'T')
        snapshot = json.loads(task.snapshot_json)
        snapshot['items'][0].update(execution_mode='platform_schedule', time='2026-10-01T12:00:00+08:00')
        task.snapshot_json = s.get(TaskCenterRow, 'T').snapshot_json = json.dumps(snapshot)
        s.get(TaskItemRow, 'I').detail_json = json.dumps(snapshot['items'][0])
    boundary = TaskSubmissionBoundary(db_engine)
    claim = boundary.claim('T', 'I')
    boundary.mark_before_final('T', 'I', claim['digest'], owner_token=claim['owner_token'])
    assert SubmissionResultStore(db_engine).record('T', 'I', owner_token=claim['owner_token'],
        expected_digest=claim['digest'], evidence=SubmissionEvidence(True,
            platform_schedule_confirmation='2026-10-01T12:00:00+08:00')) == 'platform_scheduled'
    assert states(db_engine) == ['platform_scheduled'] * 4


def test_wrong_worker_and_changed_snapshot_rejected(db_engine):
    claim = setup_claim(db_engine)
    with pytest.raises(ValueError, match='another worker'):
        SubmissionResultStore(db_engine).record('T', 'I', **{**claim, 'owner_token': '0' * 64}, evidence=SubmissionEvidence(True))
    with session_scope(db_engine) as s:
        s.get(TaskItemRow, 'I').detail_json = '{}'
    with pytest.raises(ValueError, match='changed'):
        SubmissionResultStore(db_engine).record('T', 'I', **claim, evidence=SubmissionEvidence(True))


def test_result_write_failure_keeps_all_states_unknown(db_engine):
    claim = setup_claim(db_engine)
    with db_engine.begin() as c:
        c.exec_driver_sql("CREATE TRIGGER reject_result BEFORE INSERT ON settings WHEN NEW.key LIKE 'publish.result.%' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(Exception):
        SubmissionResultStore(db_engine).record('T', 'I', **claim, evidence=SubmissionEvidence(True, public_result='published'))
    assert states(db_engine) == ['result_unknown'] * 4


def test_result_survives_reopen_and_duplicate_delivery(db_engine):
    claim = setup_claim(db_engine)
    evidence = SubmissionEvidence(True, public_result='published')
    SubmissionResultStore(db_engine).record('T', 'I', **claim, evidence=evidence)
    reopened = create_engine_for(db_engine.url.database)
    try:
        assert SubmissionResultStore(reopened).record('T', 'I', **claim, evidence=evidence) == 'published'
        assert states(reopened) == ['published'] * 4
    finally:
        reopened.dispose()
