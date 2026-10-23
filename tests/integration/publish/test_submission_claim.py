import pytest
from concurrent.futures import ThreadPoolExecutor
from test_task_submission_boundary import seed
from yijianjifa.features.publish.task_submission_boundary import TaskSubmissionBoundary
from yijianjifa.storage.db import create_engine_for


def test_only_one_worker_acquires_preparation(db_engine):
    seed(db_engine)
    def acquire(_):
        try: return TaskSubmissionBoundary(db_engine).claim('T','I')
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(x is not None for x in pool.map(acquire,range(2)))==1


def test_reopened_process_cannot_reclaim_or_submit_without_owner(db_engine):
    seed(db_engine);boundary=TaskSubmissionBoundary(db_engine)
    claim=boundary.claim('T','I')
    reopened=create_engine_for(db_engine.url.database)
    try:
        other=TaskSubmissionBoundary(reopened)
        with pytest.raises(ValueError): other.claim('T','I')
        with pytest.raises(ValueError): other.mark_before_final('T','I',claim['digest'])
        with pytest.raises(ValueError): other.mark_before_final('T','I',claim['digest'],owner_token='wrong')
        boundary.mark_before_final('T','I',claim['digest'],owner_token=claim['owner_token'])
    finally: reopened.dispose()


def test_disabled_execution_never_creates_claim(db_engine):
    seed(db_engine,enabled=False)
    with pytest.raises(ValueError): TaskSubmissionBoundary(db_engine).claim('T','I')


def test_owner_secret_is_not_stored_in_database(db_engine):
    from sqlalchemy import select
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    seed(db_engine);claim=TaskSubmissionBoundary(db_engine).claim('T','I')
    with session_scope(db_engine) as s:
        rows=list(s.scalars(select(Setting).where(Setting.key.like('publish.claim.%'))))
        assert len(rows)==1
        assert claim['owner_token'] not in rows[0].value_json
