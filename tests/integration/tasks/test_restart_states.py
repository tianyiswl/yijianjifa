from pathlib import Path

from yijianjifa.storage.db import create_engine_for, migrate_to_head
from yijianjifa.features.tasks.service import TaskCenterService


def test_result_unknown_survives_restart_and_still_blocks_retry(tmp_path: Path):
    db = tmp_path / "restart.db"
    engine = create_engine_for(db)
    migrate_to_head(engine)
    center = TaskCenterService(engine)
    center.register_task("P1", kind="publish", name="待核对", state="running", snapshot_json='{}')
    center.record_issue(
        task_id="P1",
        state="result_unknown",
        evidence={"final_action_triggered": True},
        title="发布结果待确认",
        stage="最终提交后的平台回读",
    )
    engine.dispose()

    reopened_engine = create_engine_for(db)
    migrate_to_head(reopened_engine)
    reopened = TaskCenterService(reopened_engine)

    task = reopened.get("P1")
    assert task.state == "result_unknown"
    assert task.retry_allowed is False
    assert reopened.latest_issue("P1").evidence == {"final_action_triggered": True}


def test_restart_marks_running_tasks_interrupted_without_auto_resuming(tmp_path: Path):
    db = tmp_path / "interrupt.db"
    engine = create_engine_for(db)
    migrate_to_head(engine)
    center = TaskCenterService(engine)
    center.register_task("M1", kind="make", name="混剪", state="running", snapshot_json='{}')
    engine.dispose()

    reopened_engine = create_engine_for(db)
    migrate_to_head(reopened_engine)
    reopened = TaskCenterService(reopened_engine)
    candidates = reopened.recover_after_restart()

    assert reopened.get("M1").state == "interrupted"
    assert [x.id for x in candidates] == ["M1"]
    assert reopened.get("M1").state != "running"
