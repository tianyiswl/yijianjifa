import json
import sys
from types import SimpleNamespace

import pytest

from yijianjifa.app_services.cloud_voice_test import (
    LOCAL_TEST_KIND, LOCAL_TEST_VOICE, prepare_test_client, require_test_client,
    validate_test_endpoint,
)


@pytest.mark.parametrize('endpoint', [
    'https://127.0.0.1:8768', 'http://localhost:8768', 'http://0.0.0.0:8768',
    'http://example.com:8768', 'http://127.0.0.1', 'http://127.0.0.1:0',
    'http://127.0.0.1:8768/path', 'http://127.0.0.1:8768?token=x',
    'http://127.0.0.1:8768#x', 'http://name@127.0.0.1:8768',
])
def test_source_test_endpoint_cannot_reach_remote_or_ambiguous_destination(endpoint):
    with pytest.raises(ValueError):
        validate_test_endpoint(endpoint)


def test_source_test_override_is_forbidden_in_packaged_client(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    with pytest.raises(ValueError):
        validate_test_endpoint('http://127.0.0.1:8768')


def test_test_client_is_new_and_persistently_marked_without_copying_real_data(tmp_path):
    descriptor = tmp_path / 'service.json'
    descriptor.write_text(json.dumps({'kind': LOCAL_TEST_KIND, 'local_test': True,
                                      'endpoint': 'http://127.0.0.1:8768'}))
    data = tmp_path / 'client'
    assert prepare_test_client(descriptor, data) == 'http://127.0.0.1:8768'
    require_test_client(data)
    assert not (data / 'app.db').exists()
    (data / 'app.db').write_bytes(b'isolated-test-db')
    assert prepare_test_client(descriptor, data) == 'http://127.0.0.1:8768'
    assert (data / 'app.db').read_bytes() == b'isolated-test-db'


def test_test_client_refuses_existing_unmarked_data_without_mutating_it(tmp_path):
    descriptor = tmp_path / 'service.json'
    descriptor.write_text(json.dumps({'kind': LOCAL_TEST_KIND, 'local_test': True,
                                      'endpoint': 'http://127.0.0.1:8768'}))
    data = tmp_path / 'original'; data.mkdir(); original = data / 'app.db'
    original.write_bytes(b'original-db')
    with pytest.raises(ValueError):
        prepare_test_client(descriptor, data)
    assert original.read_bytes() == b'original-db'
    assert list(data.iterdir()) == [original]


def test_normal_service_descriptor_cannot_activate_local_test_mode(tmp_path):
    descriptor = tmp_path / 'service.json'; descriptor.write_text(json.dumps({'endpoint': 'http://127.0.0.1:8768'}))
    with pytest.raises(ValueError):
        prepare_test_client(descriptor, tmp_path / 'client')
    assert not (tmp_path / 'client').exists()


def test_marked_directory_cannot_link_its_database_to_original_data(tmp_path):
    descriptor = tmp_path / 'service.json'
    descriptor.write_text(json.dumps({'kind': LOCAL_TEST_KIND, 'local_test': True,
                                      'endpoint': 'http://127.0.0.1:8768'}))
    data = tmp_path / 'client'; prepare_test_client(descriptor, data)
    original = tmp_path / 'original.db'; original.write_bytes(b'original')
    (data / 'app.db').symlink_to(original)
    with pytest.raises(ValueError):
        require_test_client(data)
    with pytest.raises(ValueError):
        prepare_test_client(descriptor, data)
    assert original.read_bytes() == b'original'


def test_local_test_catalog_has_one_honestly_named_simulated_voice(tmp_path):
    from yijianjifa.app_services.voice_catalog import VoiceCatalog
    config = SimpleNamespace(status=lambda: {'configured': True, 'mode': 'hosted', 'local_test': True})
    result = VoiceCatalog(SimpleNamespace(data_dir=tmp_path), SimpleNamespace(), config).read()
    assert len(result['items']) == 1
    assert result['items'][0]['id'] == LOCAL_TEST_VOICE
    assert '系统语音模拟' in result['items'][0]['name']
    assert result['items'][0]['standard_sample'] is False


def test_local_test_flag_is_native_only_and_not_a_production_endpoint_change(tmp_path):
    from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess, CLOUD_VOICE_ENDPOINT
    paths = SimpleNamespace(data_dir=tmp_path)
    local = CloudVoiceAccess(paths, endpoint='http://127.0.0.1:8768', local_test=True)
    assert local.read()['local_test'] is True
    assert CloudVoiceAccess(paths, endpoint='').read().get('local_test', False) is False
    # Native test mode must not retarget the public business default or an
    # explicitly configured service, and never enables billing for test audio.
    assert CLOUD_VOICE_ENDPOINT == 'https://39.108.82.213'
    official = CloudVoiceAccess(paths, endpoint='https://voice.example.test')
    assert official.endpoint == 'https://voice.example.test'
    assert official.local_test is False
