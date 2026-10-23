from yijianjifa import APP_NAME, APP_ID


def test_visible_app_name_is_yijianjifa():
    assert APP_NAME == "一剪即发"
    assert "P011" not in APP_NAME
    assert APP_ID == "com.yijianjifa.desktop"
