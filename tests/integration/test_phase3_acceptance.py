from datetime import datetime, timedelta, timezone

from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.publish.executor import PublishExecutor, SubmissionEvidence
from yijianjifa.features.publish.schedule import PlatformLimits, ScheduleItem, validate_platform_window


def test_phase3_default_account_is_per_platform(db_engine):
    service = AccountService(db_engine)
    dy = service.create("douyin", "dy-1", "抖音A")
    future = service.create("future", "f-1", "未来平台A")
    service.set_default("douyin", dy.id)
    service.set_default("future", future.id)
    assert service.default("douyin").id == dy.id
    assert service.default("future").id == future.id


def test_phase3_result_unknown_blocks_retry_and_window_checks_each_item():
    executor = PublishExecutor()
    state = executor.classify(SubmissionEvidence(final_action_triggered=True))
    assert state == "result_unknown"
    assert not executor.retry_allowed(state)

    now = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
    result = validate_platform_window(
        [ScheduleItem(0, now + timedelta(days=13)), ScheduleItem(1, now + timedelta(days=14, minutes=1))],
        now=now,
        limits=PlatformLimits(min_hours=2, max_days=14),
    )
    assert result.invalid_indexes == [1]
