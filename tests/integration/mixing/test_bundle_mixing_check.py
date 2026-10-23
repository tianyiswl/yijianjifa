import pytest

from yijianjifa.webui import bundle_check


def test_developer_bundle_check_verifies_real_chinese_media_and_failure_log(
        db_engine, app_paths):
    check = getattr(bundle_check, 'check_mixing', None)
    assert callable(check), 'Frozen check must exercise actual mixing and persisted failure attribution'
    result = check(db_engine, app_paths)
    assert result == {'chinese_source': True, 'rendered': True,
                      'failure_material': '损坏的中文测试素材.mp4',
                      'failure_stage': '读取素材视频信息', 'saved_execution_log': True}
