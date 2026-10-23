from datetime import datetime,timedelta,timezone
from yijianjifa.features.publish.schedule import PlatformLimits,ScheduleItem,validate_platform_window

def test_product_window_inclusive_edges_and_strict_no_software_choice():
    now=datetime(2026,9,28,2,0,tzinfo=timezone.utc)
    offsets=[timedelta(hours=2.5)-timedelta(microseconds=1),timedelta(hours=2.5),timedelta(days=13),timedelta(days=13,microseconds=1)]
    result=validate_platform_window([ScheduleItem(i,now+d) for i,d in enumerate(offsets)],now=now,limits=PlatformLimits(2.5,13,reject_short=True))
    assert result.invalid_indexes==[0,3]
    assert result.items[0].error_code=='before_schedule_window'
    assert result.items[3].error_code=='beyond_platform_window'
    assert all(not item.required_choice for item in result.items)
    assert all(result.items[i].error_code is None for i in [1,2])
