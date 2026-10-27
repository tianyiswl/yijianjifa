from pathlib import Path
import tomllib
from yijianjifa import APP_VERSION
from yijianjifa.webui.app_bundle import application_script


def test_displayed_version_matches_package_and_installer():
    root = Path(__file__).resolve().parents[3]
    assert tomllib.loads((root / 'pyproject.toml').read_text(encoding="utf-8"))['project']['version'] == APP_VERSION
    assert f'#define MyAppVersion "{APP_VERSION}"' in (root / 'packaging/windows/installer.iss').read_text(encoding="utf-8")
    assert application_script().startswith(f'const P011Version="{APP_VERSION}";')


def test_existing_instance_notice_identifies_the_copy_that_did_not_start():
    import sys
    from yijianjifa.webui.windows_app import existing_instance_notice
    title, message = existing_instance_notice()
    assert APP_VERSION in title and '尚未启动' in title
    assert '另一个窗口' in message and '正常退出' in message
    assert str(Path(sys.executable).resolve()) in message


def test_startup_version_receipt_requires_both_live_page_and_window_versions():
    from yijianjifa.webui.windows_app import startup_version_receipt
    title = f'一剪即发 {APP_VERSION} · Windows 测试版'
    assert startup_version_receipt(title, APP_VERSION)['version_consistent'] is True
    assert startup_version_receipt(title, '0.0.0')['version_consistent'] is False
    assert startup_version_receipt('一剪即发 0.0.0 · Windows 测试版', APP_VERSION)['version_consistent'] is False
