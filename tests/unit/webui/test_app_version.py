from pathlib import Path
import tomllib
from yijianjifa import APP_VERSION
from yijianjifa.webui.app_bundle import application_script


def test_displayed_version_matches_package_and_installer():
    root = Path(__file__).resolve().parents[3]
    assert tomllib.loads((root / 'pyproject.toml').read_text(encoding="utf-8"))['project']['version'] == APP_VERSION
    assert f'#define MyAppVersion "{APP_VERSION}"' in (root / 'packaging/windows/installer.iss').read_text(encoding="utf-8")
    assert application_script().startswith(f'const P011Version="{APP_VERSION}";')
