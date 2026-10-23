from yijianjifa.features.tasks.service import TaskCenterService


def test_task_center_lists_filters_and_preserves_distinct_states(db_engine):
    center = TaskCenterService(db_engine)
    center.register_task("M1", kind="make", name="产品A混剪", state="running", snapshot_json='{"n":10}')
    center.register_task("P1", kind="publish", name="墨白发布", state="platform_scheduled", account_id="A1", snapshot_json='{}')
    center.register_task("P2", kind="publish", name="结果核对", state="result_unknown", account_id="A1", snapshot_json='{}')

    assert [x.id for x in center.list(kind="make")] == ["M1"]
    assert {x.id for x in center.list(kind="publish")} == {"P1", "P2"}
    assert center.get("P1").state == "platform_scheduled"
    assert center.get("P2").state == "result_unknown"
    assert center.get("P2").retry_allowed is False


def test_waiting_task_order_can_change_without_changing_snapshots(db_engine):
    center = TaskCenterService(db_engine)
    center.register_task("M1", kind="make", name="一", state="waiting", snapshot_json='{"slot":1}', queue_order=1)
    center.register_task("M2", kind="make", name="二", state="waiting", snapshot_json='{"slot":2}', queue_order=2)
    before = {x.id: x.snapshot_json for x in center.list(kind="make")}

    center.move_waiting("M2", direction="up")

    assert [x.id for x in center.list(kind="make")] == ["M2", "M1"]
    assert {x.id: x.snapshot_json for x in center.list(kind="make")} == before
