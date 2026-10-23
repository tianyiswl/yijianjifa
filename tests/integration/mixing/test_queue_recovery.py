from sqlalchemy import inspect, text

from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.storage.db import create_engine_for, migrate_to_head, migrate_to_revision, session_scope
from yijianjifa.storage.models import Setting


class FakeExecutor:
    def __init__(self, failures=()):
        self.failures = set(failures)

    def render_item(self, task_id, item_index, snapshot):
        if item_index in self.failures:
            raise RuntimeError("synthetic failure")
        return f"finished-{item_index}.mp4"


def test_recovery_marks_running_as_interrupted_and_does_not_auto_resume(db_engine):
    queue = MixQueue(db_engine, FakeExecutor())
    task = queue.enqueue({"quantity": 2})
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE mix_tasks SET state='running', auto_resume=1 WHERE id=:id"), {"id": task.id})

    ids = queue.recover_interrupted()

    row = queue.get(task.id)
    assert task.id in ids
    assert row.state == "interrupted"
    assert row.auto_resume is False


def test_failed_output_does_not_consume_replacement_slot_or_move_future_schedule(db_engine):
    queue = MixQueue(db_engine, FakeExecutor(failures={1}))
    task = queue.enqueue({"quantity": 3, "planned_publish_times": [100, 200, 300]})

    queue.run_once_until_idle()

    row = queue.get(task.id)
    assert row.completed_count == 3
    assert row.success_count == 2
    assert row.failure_count == 1
    assert row.planned_publish_times == [100, 200, 300]
    assert row.state == "completed_with_errors"


def test_cancel_remaining_keeps_completed_count_and_does_not_run_more(db_engine):
    queue = MixQueue(db_engine, FakeExecutor())
    task = queue.enqueue({"quantity": 3})
    queue._run_single_item(task.id)
    queue.cancel_remaining(task.id)
    queue.run_once_until_idle()
    row = queue.get(task.id)
    assert row.completed_count == 1
    assert row.state == "cancelled"


def test_migration_from_phase1_schema_to_mix_tasks_preserves_settings(tmp_path):
    engine = create_engine_for(tmp_path / "upgrade.db")
    migrate_to_revision(engine, "0001_initial")
    with session_scope(engine) as session:
        session.add(Setting(key='keep_video_copy', value_json='false'))

    migrate_to_head(engine)

    assert "mix_tasks" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        value = connection.execute(text("SELECT value_json FROM settings WHERE key='keep_video_copy'")).scalar_one()
    assert value == "false"
