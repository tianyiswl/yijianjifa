from yijianjifa.features.presets.models import DailyRule, NormalPublishPreset


def test_publish_preset_stores_rule_not_past_concrete_datetime():
    p = NormalPublishPreset(
        name="日更",
        schedule_rule=DailyRule(first_delay_hours=3, per_day=3, days=5, interval_hours=3),
    )
    payload = p.model_dump_json()
    assert "2026-" not in payload
    compact = payload.replace(" ", "")
    assert '"first_delay_hours":3.0' in compact
    assert '"per_day":3' in compact
    assert '"days":5' in compact


def test_schedule_rule_rejects_nonpositive_values():
    try:
        DailyRule(first_delay_hours=0, per_day=3, days=5, interval_hours=3)
    except ValueError:
        pass
    else:
        raise AssertionError("zero first delay must be rejected")
