from datetime import datetime, timedelta, timezone

from yijianjifa.features.publish.software_scheduler import SoftwareScheduler


def test_missed_software_schedule_waits_for_user_decision(db_engine):
    scheduler = SoftwareScheduler(db_engine)
    when = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
    item = scheduler.schedule(when=when, payload={"video_id": "V1"})

    scheduler.on_startup(now=when + timedelta(hours=1))

    current = scheduler.get(item.id)
    assert current.state == "missed_waiting_user"
    assert scheduler.auto_submitted(item.id) is False


def test_future_software_schedule_remains_waiting_on_startup(db_engine):
    scheduler = SoftwareScheduler(db_engine)
    now = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
    item = scheduler.schedule(when=now + timedelta(hours=3), payload={"video_id": "V1"})
    scheduler.on_startup(now=now)
    assert scheduler.get(item.id).state == "waiting"
