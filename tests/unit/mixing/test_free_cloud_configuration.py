"""A public source checkout must not opt into the operator's paid service."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess


def cloud(tmp_path):
    return CloudVoiceAccess(SimpleNamespace(data_dir=tmp_path / 'data'), opener=Mock(), vault=Mock())


def test_unconfigured_source_keeps_local_use_free_without_connecting_to_cloud(tmp_path, monkeypatch):
    monkeypatch.delenv('P011_CLOUD_VOICE_ENDPOINT', raising=False)
    monkeypatch.delattr(sys, 'frozen', raising=False)
    service = cloud(tmp_path)
    assert service.endpoint == ''
    assert service.read()['enabled'] is False
    service.opener.assert_not_called()
    service.vault.get_password.assert_not_called()


def test_explicit_cloud_configuration_preserves_existing_credential_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv('P011_CLOUD_VOICE_ENDPOINT', 'https://voice.example.test/')
    configured = cloud(tmp_path)
    explicit = CloudVoiceAccess(configured.paths, endpoint='https://voice.example.test', opener=Mock(), vault=Mock())
    assert configured.endpoint == explicit.endpoint
    assert configured.service == explicit.service
    configured.opener.assert_not_called()


def test_official_frozen_build_uses_its_explicit_bundled_service_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv('P011_CLOUD_VOICE_ENDPOINT', raising=False)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    (tmp_path / 'cloud-voice-service.json').write_text(json.dumps({'endpoint': 'https://voice.example.test'}))
    assert cloud(tmp_path).endpoint == 'https://voice.example.test'
    monkeypatch.setenv('P011_CLOUD_VOICE_ENDPOINT', '')
    assert cloud(tmp_path).endpoint == ''


@pytest.mark.parametrize('configuration', [
    '{broken', 'null', '[]', '{"endpoint":123}',
    '{"endpoint":"http://example.test"}',
    '{"endpoint":"https://user:password@example.test"}',
    '{"endpoint":"https://example.test?token=not-a-real-token"}',
])
def test_bad_optional_service_configuration_fails_closed_without_blocking_local_use(tmp_path, monkeypatch, configuration):
    monkeypatch.delenv('P011_CLOUD_VOICE_ENDPOINT', raising=False)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    (tmp_path / 'cloud-voice-service.json').write_text(configuration)
    service = cloud(tmp_path)
    assert service.endpoint == ''
    assert service.read()['enabled'] is False
    service.opener.assert_not_called()


def test_explicit_constructor_endpoint_still_rejects_an_insecure_remote_service(tmp_path):
    with pytest.raises(ValueError):
        CloudVoiceAccess(SimpleNamespace(data_dir=tmp_path), endpoint='http://example.test')


@pytest.mark.parametrize('endpoint', [
    'https://example.test/\ud800', 'https://example.test\x00',
    'https://example.test/\x1f', 'https://example.test/\x7f',
    'https://example.test/\nother', 'https://exa\tmple.test',
    'https://exam ple.test', 'https://exam\u00a0ple.test',
    'https://example.test?', 'https://example.test#', 'https://@example.test',
])
def test_malformed_frozen_endpoint_disables_cloud_without_crashing_local_startup(tmp_path, monkeypatch, endpoint):
    # Escaped JSON is a real way for malformed Unicode/controls to reach a build.
    monkeypatch.delenv('P011_CLOUD_VOICE_ENDPOINT', raising=False)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    (tmp_path / 'cloud-voice-service.json').write_text(json.dumps({'endpoint': endpoint}), encoding='utf-8')
    service = cloud(tmp_path)
    assert service.endpoint == '' and service.read()['enabled'] is False
    service.opener.assert_not_called()
    service.vault.get_password.assert_not_called()
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('endpoint', ['https://example.test/\nother', 'https://exa\tmple.test',
                                     'https://example.test\n', 'https://example.test\t'])
def test_environment_controls_disable_cloud_before_urlsplit_can_discard_them(tmp_path, monkeypatch, endpoint):
    monkeypatch.setenv('P011_CLOUD_VOICE_ENDPOINT', endpoint)
    service = cloud(tmp_path)
    assert service.endpoint == '' and service.read()['enabled'] is False
    service.opener.assert_not_called()
    service.vault.get_password.assert_not_called()


@pytest.mark.parametrize('endpoint', ['https://example.test/\ud800', 'https://example.test/\nother',
                                     'https://exam ple.test', 'https://example.test?',
                                     'https://example.test#', 'https://example.test:not-a-port'])
def test_explicit_constructor_rejects_malformed_endpoints_before_namespace_hash(tmp_path, endpoint):
    with pytest.raises(ValueError, match='HTTPS'):
        CloudVoiceAccess(SimpleNamespace(data_dir=tmp_path), endpoint=endpoint, opener=Mock(), vault=Mock())
