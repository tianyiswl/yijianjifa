from yijianjifa.features.finished.covers import CoverChoice, validate_frame_time


def test_frame_time_must_be_inside_video():
    assert validate_frame_time(10_000, 0).ok
    assert validate_frame_time(10_000, 9_999).ok
    assert not validate_frame_time(10_000, 10_000).ok
    assert not validate_frame_time(10_000, -1).ok


def test_cover_choice_frame_requires_valid_timestamp():
    choice = CoverChoice.frame(3_000)
    assert choice.kind == "frame"
    assert choice.frame_ms == 3_000
