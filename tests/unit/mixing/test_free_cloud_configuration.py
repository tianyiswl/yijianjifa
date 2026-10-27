"""Source users can access cloud voice; free local use never auto-synthesizes."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess


def cloud(tmp_path):
    vault=Mock();vault.get_password.return_value=None
    return CloudVoiceAccess(SimpleNamespace(data_dir=tmp_path / 'data'), opener=Mock(), vault=vault)


def test_source_default_exposes_existing_cloud_business_without_automatic_requests(tmp_path, monkeypatch):
    monkeypatch.delenv('P011_CLOUD_VOICE_ENDPOINT', raising=False)
    monkeypatch.delattr(sys, 'frozen', raising=False)
    service = cloud(tmp_path)
    assert service.endpoint == 'https://39.108.82.213'
    assert service.read()['enabled'] is True
    assert service.read()['authenticated'] is False
    service.opener.assert_not_called()


def test_explicit_empty_environment_still_disables_cloud_and_credential_access(tmp_path,monkeypatch):
    monkeypatch.setenv('P011_CLOUD_VOICE_ENDPOINT','')
    service=cloud(tmp_path)
    assert service.endpoint=='' and service.read()['enabled'] is False
    service.opener.assert_not_called()
    service.vault.get_password.assert_not_called()


def test_source_default_keeps_same_account_namespace_as_official_service(tmp_path,monkeypatch):
    monkeypatch.delenv('P011_CLOUD_VOICE_ENDPOINT',raising=False)
    monkeypatch.delattr(sys,'frozen',raising=False)
    service=cloud(tmp_path)
    explicit=CloudVoiceAccess(service.paths,endpoint='https://39.108.82.213',opener=Mock(),vault=Mock())
    assert service.service==explicit.service


def test_default_cloud_service_does_not_replace_or_bill_system_voice(tmp_path,monkeypatch):
    from yijianjifa.features.mixing.tts import voice_services
    monkeypatch.delenv('P011_CLOUD_VOICE_ENDPOINT',raising=False)
    paths=SimpleNamespace(data_dir=tmp_path/'data',cache_dir=tmp_path/'cache')
    system=Mock();monkeypatch.setattr(voice_services,'system_tts_adapter',lambda:system)
    config,tts,service=voice_services.build_voice_services(paths)
    service.opener=Mock(side_effect=AssertionError('No automatic cloud request'))
    assert service.endpoint=='https://39.108.82.213'
    assert tts.freeze({'options':{},'copies':[]})=={'source':'system'}
    tts.synthesize('免费系统配音','',1,tmp_path/'system.wav')
    system.synthesize.assert_called_once()
    service.opener.assert_not_called()


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
