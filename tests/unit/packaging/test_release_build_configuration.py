"""Exercise build configuration without PyInstaller, service calls or user data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
CHECKER = ROOT / 'packaging/check_release_configuration.py'


def environment(endpoint=None):
    result = {key: value for key, value in os.environ.items()
              if key not in {'P011_RELEASE_CLOUD_VOICE_ENDPOINT', 'P011_CLOUD_VOICE_ENDPOINT'}}
    result['P011_BUILD_PYTHON'] = sys.executable
    if endpoint is not None:
        result['P011_RELEASE_CLOUD_VOICE_ENDPOINT'] = endpoint
    return result


def inspect(*args, endpoint=None):
    return subprocess.run([sys.executable, str(CHECKER), *args],
                          env=environment(endpoint), text=True, capture_output=True)


def test_official_build_cannot_silently_omit_cloud_configuration():
    # Removing the official-mode requirement would deliver a package that disables paid accounts.
    result = inspect('--official')
    assert result.returncode != 0
    assert 'Official builds require P011_RELEASE_CLOUD_VOICE_ENDPOINT' in result.stderr


def test_third_party_build_without_endpoint_stays_cloud_off():
    result = inspect()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['cloud_voice_enabled'] is False


def test_official_build_configuration_must_survive_freezing(tmp_path):
    # Missing or changed generated metadata must stop packaging before delivery.
    endpoint = 'https://voice.example.test'
    missing = inspect('--official', '--frozen-root', str(tmp_path), endpoint=endpoint)
    assert missing.returncode != 0
    config = tmp_path / 'cloud-voice-service.json'
    config.write_text('{"endpoint":"https://wrong.example.test"}')
    mismatch = inspect('--official', '--frozen-root', str(tmp_path), endpoint=endpoint)
    assert mismatch.returncode != 0
    config.write_text('{"endpoint":"https://voice.example.test"}')
    verified = inspect('--official', '--frozen-root', str(tmp_path), endpoint=endpoint + '/')
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)['frozen_config_verified'] is True


def test_unconfigured_build_rejects_a_stale_bundled_operator_endpoint(tmp_path):
    (tmp_path / 'cloud-voice-service.json').write_text('{"endpoint":"https://old.example.test"}')
    result = inspect('--frozen-root', str(tmp_path))
    assert result.returncode != 0
    assert 'Unexpected cloud voice configuration' in result.stderr


@pytest.mark.parametrize('endpoint', ['http://voice.example.test', 'https://user:secret@example.test',
                                     'https://example.test?token=secret',
                                     'https://example.test\n', 'https://exam ple.test',
                                     'https://example.test?', 'https://@example.test'])
def test_build_inspection_rejects_invalid_or_secret_bearing_metadata(endpoint):
    result = inspect('--official', endpoint=endpoint)
    assert result.returncode != 0
    assert 'credential-free HTTPS endpoint' in result.stderr
    assert 'secret' not in result.stdout + result.stderr


def test_mac_build_entry_checks_official_configuration_before_native_building():
    script = str(ROOT / 'packaging/macos/build.sh')
    bash = shutil.which('bash')
    assert bash, '此构建入口检查需要 Bash'
    missing = subprocess.run([bash, script, '--official', '--check-configuration'],
                             env=environment(), text=True, capture_output=True)
    assert missing.returncode != 0
    assert 'Official builds require P011_RELEASE_CLOUD_VOICE_ENDPOINT' in missing.stderr
    configured = subprocess.run([bash, script, '--official', '--check-configuration'],
                                env=environment('https://voice.example.test'), text=True, capture_output=True)
    assert configured.returncode == 0, configured.stderr
    assert json.loads(configured.stdout)['cloud_voice_enabled'] is True


@pytest.mark.skipif(shutil.which('pwsh') is None, reason='Windows entry contract requires PowerShell 7')
def test_windows_build_entry_checks_official_configuration_before_building():
    args = [shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-File',
            str(ROOT / 'packaging/windows/build.ps1'), '-Official', '-CheckConfiguration']
    missing = subprocess.run(args, env=environment(), text=True, capture_output=True)
    assert missing.returncode != 0
    assert 'Official builds require P011_RELEASE_CLOUD_VOICE_ENDPOINT' in missing.stderr
    configured = subprocess.run(args, env=environment('https://voice.example.test'),
                                text=True, capture_output=True)
    assert configured.returncode == 0, configured.stderr
    assert json.loads(configured.stdout)['cloud_voice_enabled'] is True
