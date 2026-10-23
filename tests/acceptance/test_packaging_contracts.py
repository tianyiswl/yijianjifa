from pathlib import Path
import tomllib


def test_build_extra_declares_pyinstaller():
    data = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))
    deps = data['project']['optional-dependencies']['build']
    assert any('pyinstaller' in dep.lower() for dep in deps)


def test_windows_and_macos_packaging_scripts_exist_and_keep_user_data_outside_bundle():
    required = [
        Path('packaging/windows/yijianjifa.spec'),
        Path('packaging/windows/build.ps1'),
        Path('packaging/windows/installer.iss'),
        Path('packaging/macos/yijianjifa.spec'),
        Path('packaging/macos/build.sh'),
        Path('packaging/macos/make_dmg.sh'),
    ]
    for path in required:
        assert path.is_file(), path
    combined = '\n'.join(p.read_text(encoding='utf-8') for p in required)
    assert 'packaging/resources' in combined.replace('\\', '/')
    assert 'user_data_dir' not in combined  # installers must not embed/delete runtime user data paths


def test_os_acceptance_docs_start_unverified():
    for name in ['windows-install.md', 'macos-install.md']:
        text = Path('docs/acceptance', name).read_text(encoding='utf-8')
        assert 'UNVERIFIED' in text
        assert 'PASS' not in text
