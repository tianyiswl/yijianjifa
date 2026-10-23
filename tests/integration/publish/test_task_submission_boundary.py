import json
import pytest
from sqlalchemy import select
from yijianjifa.storage.db import session_scope, create_engine_for
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.commerce.service import CommerceTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow, PublishHistoryRepository
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.features.publish.task_submission_boundary import TaskSubmissionBoundary


def seed(engine, kind='normal_publish', enabled=True):
    item=dict(account_id='A',video_id='V')
    snapshot=json.dumps(dict(kind=kind,execution_enabled=enabled,items=[item],videos=[dict(id='V',content_hash='a'*64)]))
    with session_scope(engine) as s:
        model=PublishTaskRow if kind=='normal_publish' else CommerceTaskRow
        s.add(model(id='T',state='running',snapshot_json=snapshot,**({'platform':'douyin'} if kind=='normal_publish' else {'account_id':'A'})))
        s.add(TaskCenterRow(id='T',kind=kind,name='fixture',state='running',snapshot_json=snapshot))
        s.flush()
        s.add(TaskItemRow(id='I',task_id='T',item_key='A:V',state='running',detail_json=json.dumps(item)))
    PublishHistoryRepository(engine).record('a'*64,'A','queued',task_id='T')
    return snapshot


@pytest.mark.parametrize('kind',['normal_publish','commerce_publish'])
def test_marker_task_item_and_history_commit_together_and_survive_reopen(db_engine,kind):
    original=seed(db_engine,kind)
    boundary=TaskSubmissionBoundary(db_engine)
    claim=boundary.claim('T','I');digest=claim['digest']
    boundary.mark_before_final('T','I',digest,owner_token=claim['owner_token'])
    reopened=create_engine_for(db_engine.url.database)
    try:
        with pytest.raises(ValueError): TaskSubmissionBoundary(reopened).check_ready('T','I')
        with session_scope(reopened) as s:
            assert s.get(TaskItemRow,'I').state=='result_unknown'
            assert s.get(TaskCenterRow,'T').state=='result_unknown'
            assert s.get(TaskCenterRow,'T').snapshot_json==original
            assert s.scalar(select(PublishHistoryRow)).state=='result_unknown'
        assert PublishHistoryRepository(reopened).has_quick_block('a'*64)
    finally: reopened.dispose()


def test_current_disabled_tasks_cannot_be_enabled_by_boundary(db_engine):
    seed(db_engine,enabled=False)
    with pytest.raises(ValueError): TaskSubmissionBoundary(db_engine).check_ready('T','I')
    with session_scope(db_engine) as s: assert s.get(TaskItemRow,'I').state=='running'


def test_stale_digest_rolls_back_everything(db_engine):
    seed(db_engine)
    boundary=TaskSubmissionBoundary(db_engine);claim=boundary.claim('T','I')
    with pytest.raises(ValueError): boundary.mark_before_final('T','I','b'*64,owner_token=claim['owner_token'])
    with session_scope(db_engine) as s:
        assert s.get(TaskItemRow,'I').state=='running'
        assert s.scalar(select(PublishHistoryRow)).state=='queued'


def test_missing_history_blocks_final_action(db_engine):
    seed(db_engine)
    with session_scope(db_engine) as s:
        s.delete(s.scalar(select(PublishHistoryRow)))
    with pytest.raises(ValueError): TaskSubmissionBoundary(db_engine).check_ready('T','I')


def test_write_failure_rolls_back_marker_and_all_states(db_engine):
    from yijianjifa.storage.models import Setting
    seed(db_engine)
    boundary=TaskSubmissionBoundary(db_engine);claim=boundary.claim('T','I');digest=claim['digest']
    with db_engine.begin() as c:
        c.exec_driver_sql("CREATE TRIGGER reject_history_update BEFORE UPDATE ON publish_history BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(Exception): boundary.mark_before_final('T','I',digest,owner_token=claim['owner_token'])
    with session_scope(db_engine) as s:
        assert s.get(PublishTaskRow,'T').state=='running'
        assert s.get(TaskCenterRow,'T').state=='running'
        assert s.get(TaskItemRow,'I').state=='running'
        assert s.scalar(select(PublishHistoryRow)).state=='queued'
        assert not list(s.scalars(select(Setting).where(Setting.key.like('publish.final.%'))))


def test_concurrent_markers_only_one_commits(db_engine):
    from concurrent.futures import ThreadPoolExecutor
    seed(db_engine);claim=TaskSubmissionBoundary(db_engine).claim('T','I');digest=claim['digest']
    def mark(_):
        try: TaskSubmissionBoundary(db_engine).mark_before_final('T','I',digest,owner_token=claim['owner_token']);return True
        except ValueError: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(mark,range(2)))==[False,True]


def test_recovery_does_not_reset_uncertain_task(db_engine):
    from yijianjifa.app_services.publish_control import recover_local_software
    seed(db_engine);boundary=TaskSubmissionBoundary(db_engine)
    claim=boundary.claim('T','I')
    boundary.mark_before_final('T','I',claim['digest'],owner_token=claim['owner_token'])
    assert recover_local_software(db_engine)==[]
    with session_scope(db_engine) as s:
        assert s.get(PublishTaskRow,'T').state=='result_unknown'
        assert s.get(TaskItemRow,'I').state=='result_unknown'


@pytest.mark.parametrize('boundary',['claim','final'])
def test_v2_waiting_history_cannot_bypass_legacy_submission_boundary(db_engine,boundary):
    seed(db_engine);gate=TaskSubmissionBoundary(db_engine)
    claim=gate.claim('T','I') if boundary=='final' else None
    PublishHistoryRepository(db_engine).record('a'*64,'A','waiting_adapter')
    with pytest.raises(ValueError,match='target history'):
        if boundary=='claim':gate.claim('T','I')
        else:gate.mark_before_final('T','I',claim['digest'],owner_token=claim['owner_token'])
    from yijianjifa.storage.models import Setting
    with session_scope(db_engine) as s:
        assert not list(s.scalars(select(Setting).where(Setting.key.like('publish.final.%'))))
        assert s.get(TaskItemRow,'I').state=='running'
