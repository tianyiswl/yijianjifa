"""Authentication config/policy only; no window or network is opened."""
import pytest


def test_auth_navigation_only_allows_configured_https_hosts():
    from yijianjifa.webui.auth_isolation import AuthNavigation
    policy=AuthNavigation({'login.example.test','identity.example.test'})
    assert policy.allows('https://login.example.test/start?next=1')
    assert policy.allows('https://identity.example.test:443/callback')
    for url in ('http://login.example.test','https://login.example.test.evil.test',
                'https://user:password@login.example.test','https://login.example.test:444',
                'file:///tmp/private','p011media://asset/secret','javascript:alert(1)',
                'https://evil.test/','about:blank'):
        assert not policy.allows(url),url
    assert not policy.allows('https://login.example.test',new_window=True)


def test_native_auth_config_does_not_inherit_application_media_or_scripts():
    import subprocess
    from pathlib import Path
    root=Path(__file__).resolve().parents[3]
    python=root/'.venv-webui-probe312/bin/python'
    if not python.exists():pytest.skip('Mac Cocoa probe runtime not installed')
    result=subprocess.run([str(python),str(root/'tools/webui_probe/auth_config_check.py')],cwd=root,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
