from yijianjifa.os_integration.paths import get_app_paths


def test_app_paths_are_inside_platform_app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("YJJF_TEST_DATA_DIR", str(tmp_path))
    paths = get_app_paths()
    assert paths.data_dir == tmp_path
    assert paths.managed_media_dir.parent == tmp_path
