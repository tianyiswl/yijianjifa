from yijianjifa.features.library.preferences import LibraryPreferences


def test_keep_copy_defaults_off_for_video_and_music(db_engine):
    prefs = LibraryPreferences(db_engine)
    assert prefs.keep_copy("video") is False
    assert prefs.keep_copy("music") is False


def test_keep_copy_setting_persists_and_is_separate_by_kind(db_engine):
    prefs = LibraryPreferences(db_engine)
    prefs.set_keep_copy("video", True)

    reopened = LibraryPreferences(db_engine)
    assert reopened.keep_copy("video") is True
    assert reopened.keep_copy("music") is False
