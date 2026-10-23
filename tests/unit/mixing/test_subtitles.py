from yijianjifa.features.mixing.subtitles import build_subtitle_spec


def test_full_time_subtitle_blank_override_uses_copy_text_but_never_title():
    spec = build_subtitle_spec(
        spoken_enabled=False,
        full_enabled=True,
        batch_full_text="",
        copy_full_text="",
        title="TITLE",
    )
    assert spec.full_time_text is None
    assert spec.warning_code == "full_subtitle_empty"


def test_blank_batch_override_uses_copy_full_text():
    spec = build_subtitle_spec(
        spoken_enabled=True,
        full_enabled=True,
        batch_full_text="",
        copy_full_text="每条自己的全程字幕",
        title="TITLE",
    )
    assert spec.spoken_enabled is True
    assert spec.full_time_text == "每条自己的全程字幕"
    assert spec.warning_code is None


def test_batch_full_time_override_is_render_only_and_does_not_mutate_source_value():
    original = "original"
    spec = build_subtitle_spec(
        spoken_enabled=True,
        full_enabled=True,
        batch_full_text="batch",
        copy_full_text=original,
        title="A",
    )
    assert spec.full_time_text == "batch"
    assert original == "original"


def test_full_time_disabled_ignores_both_batch_and_copy_text():
    spec = build_subtitle_spec(
        spoken_enabled=True,
        full_enabled=False,
        batch_full_text="batch",
        copy_full_text="copy",
        title="A",
    )
    assert spec.full_time_text is None
    assert spec.warning_code is None
