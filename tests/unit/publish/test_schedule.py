from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from yijianjifa.features.publish.schedule import (
    PlatformLimits,
    ScheduleItem,
    ScheduleRule,
    build_schedule,
    validate_platform_window,
)


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 23, 6, 0, tzinfo=TZ)


def test_interval_step_supports_two_point_five_hours():
    rule = ScheduleRule.interval(start=datetime(2026, 9, 24, 9, 0, tzinfo=TZ), hours=2.5)
    items = build_schedule(rule, count=2, now=NOW)
    assert items[1].local_time.strftime("%H:%M") == "11:30"


def test_daily_schedule_uses_daily_count_days_and_interval():
    rule = ScheduleRule.daily(
        start=datetime(2026, 9, 24, 9, 0, tzinfo=TZ),
        daily_count=3,
        days=2,
        interval_hours=3,
    )
    items = build_schedule(rule, count=6, now=NOW)
    assert [x.local_time.strftime("%m-%d %H:%M") for x in items] == [
        "09-24 09:00", "09-24 12:00", "09-24 15:00",
        "09-25 09:00", "09-25 12:00", "09-25 15:00",
    ]


def test_every_item_is_checked_against_fourteen_day_limit():
    items = [
        ScheduleItem(0, NOW + timedelta(days=13, hours=23)),
        ScheduleItem(1, NOW + timedelta(days=14, minutes=1)),
    ]
    result = validate_platform_window(items, now=NOW, limits=PlatformLimits(min_hours=2, max_days=14))
    assert result.invalid_indexes == [1]


def test_less_than_two_hours_requires_explicit_user_choice():
    item = ScheduleItem(0, NOW + timedelta(hours=1, minutes=30))
    result = validate_platform_window([item], now=NOW, limits=PlatformLimits(min_hours=2, max_days=14))
    assert result.items[0].required_choice == {"software_schedule", "publish_now", "change_time"}
    assert result.items[0].silent_fallback is False


def test_exact_two_hour_and_fourteen_day_boundaries_are_allowed():
    items = [ScheduleItem(0, NOW + timedelta(hours=2)), ScheduleItem(1, NOW + timedelta(days=14))]
    result = validate_platform_window(items, now=NOW, limits=PlatformLimits(min_hours=2, max_days=14))
    assert result.invalid_indexes == []
    assert all(not x.required_choice for x in result.items)


def test_count_cannot_exceed_daily_rule_capacity():
    rule = ScheduleRule.daily(
        start=datetime(2026, 9, 24, 9, 0, tzinfo=TZ),
        daily_count=3,
        days=2,
        interval_hours=3,
    )
    try:
        build_schedule(rule, count=7, now=NOW)
    except ValueError as exc:
        assert "capacity" in str(exc)
    else:
        raise AssertionError("expected capacity error")


def test_exact_time_is_shared_without_interval_or_daily_capacity():
    when=NOW+timedelta(hours=3)
    rule=ScheduleRule(mode='exact',start=when,interval_hours=3,daily_count=1,days=1)
    items=build_schedule(rule,count=3,now=NOW)
    assert [x.local_time for x in items]==[when]*3
    assert [x.index for x in items]==[0,1,2]


def test_custom_daily_times_sort_and_repeat_by_calendar_day():
    rule=ScheduleRule(mode='daily',start=datetime(2026,9,24,23,0,tzinfo=TZ),interval_hours=3,daily_count=2,days=2,daily_times=('18:45','08:15'))
    result=build_schedule(rule,count=4,now=NOW)
    assert [x.local_time.strftime('%m-%d %H:%M') for x in result]==['09-24 08:15','09-24 18:45','09-25 08:15','09-25 18:45']


def test_custom_daily_times_reject_invalid_or_ambiguous_input():
    import pytest
    from dataclasses import replace
    rule=ScheduleRule(mode='daily',start=NOW,interval_hours=3,daily_count=2,days=1,daily_times=('08:15','18:45'))
    for times in [(),('08:15',),('08:15','08:15'),('24:00','08:15'),('8:15','18:45'),('08:15:00','18:45'),('','18:45'),(None,'18:45')]:
        with pytest.raises(ValueError):replace(rule,daily_times=times)
    with pytest.raises(ValueError):replace(rule,mode='interval')


def test_custom_daily_clocks_still_validate_past_and_future_limit_per_item():
    past=ScheduleRule(mode='daily',start=NOW,interval_hours=3,daily_count=2,days=1,daily_times=('05:00','09:00'))
    checked=validate_platform_window(build_schedule(past,count=2,now=NOW),now=NOW,limits=PlatformLimits(2,14))
    assert checked.items[0].error_code=='scheduled_time_in_past'
    assert checked.items[1].error_code is None and not checked.items[1].required_choice
    future=ScheduleRule(mode='daily',start=NOW+timedelta(days=14),interval_hours=3,daily_count=1,days=2,daily_times=('06:00',))
    checked=validate_platform_window(build_schedule(future,count=2,now=NOW),now=NOW,limits=PlatformLimits(2,14))
    assert checked.items[0].error_code is None
    assert checked.items[1].error_code=='beyond_platform_window'


def test_daily_groups_cross_midnight_without_colliding():
    from datetime import datetime, timezone, timedelta
    start=datetime(2026,9,29,20,tzinfo=timezone(timedelta(hours=8)))
    rule=ScheduleRule.daily(start=start,daily_count=3,days=2,interval_hours=3)
    items=build_schedule(rule,count=6,now=start-timedelta(hours=3))
    assert [x.local_time.isoformat() for x in items]==[
        '2026-09-29T20:00:00+08:00','2026-09-29T23:00:00+08:00',
        '2026-09-30T02:00:00+08:00','2026-09-30T20:00:00+08:00',
        '2026-09-30T23:00:00+08:00','2026-10-01T02:00:00+08:00']
    assert len({x.local_time for x in items})==6
