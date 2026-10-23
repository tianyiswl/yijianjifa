from datetime import datetime, timezone


def test_confirmed_account_switch_keeps_videos_schedule_but_clears_account_specific():
    from yijianjifa.features.commerce.models import CommerceDraft, FavoriteMusicChoice
    from yijianjifa.features.publish.schedule import ScheduleRule

    schedule = ScheduleRule.daily(
        start=datetime(2026, 9, 24, 9, tzinfo=timezone.utc),
        daily_count=3,
        days=5,
        interval_hours=3,
    )
    draft = CommerceDraft(
        account_id="A01",
        schedule=schedule,
        selected_video_ids=("v1", "v2"),
        store_pool=("s1", "s2"),
        music_choice=FavoriteMusicChoice(mode="fixed", ids=("m1",)),
        declarations={"v1": "ai_generated"},
        per_video_overrides={"v1": {"store_id": "s1", "declaration": "ai_generated"}},
    )

    changed = draft.switch_account("A02", confirmed=True)

    assert changed.selected_video_ids == draft.selected_video_ids
    assert changed.schedule == draft.schedule
    assert changed.store_pool == ()
    assert changed.music_choice.mode == "none"
    assert changed.declarations == {}
    assert changed.per_video_overrides == {}
    assert changed.account_id == "A02"


def test_unconfirmed_account_switch_does_not_change_draft():
    from yijianjifa.features.commerce.models import CommerceDraft

    draft = CommerceDraft(account_id="A01", selected_video_ids=("v1",), store_pool=("s1",))
    assert draft.switch_account("A02", confirmed=False) == draft


def test_account_switch_clears_store_assignments_and_manual_store_overrides():
    from yijianjifa.features.commerce.models import CommerceDraft

    draft = CommerceDraft(
        account_id="A01",
        selected_video_ids=("v1",),
        store_pool=("s1",),
        store_assignments={"v1": "s1"},
        manual_store_overrides={"v1": "s1"},
    )
    changed = draft.switch_account("A02", confirmed=True)
    assert changed.store_assignments == {}
    assert changed.manual_store_overrides == {}
