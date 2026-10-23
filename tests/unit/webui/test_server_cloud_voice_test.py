"""The real server source test must not reuse the local demo's data or sessions."""
import json
import sys
from types import SimpleNamespace

import pytest


def test_server_test_prepares_and_reuses_only_its_own_marked_directory(tmp_path):
    from yijianjifa.app_services.cloud_voice_server_test import prepare_server_test_client
    data = tmp_path / 'server-client'
    assert prepare_server_test_client(data) == 'https://39.108.82.213'
    (data / 'app.db').write_bytes(b'isolated-server-test')
    assert prepare_server_test_client(data) == 'https://39.108.82.213'
    assert (data / 'app.db').read_bytes() == b'isolated-server-test'


def test_server_test_refuses_original_and_simulated_client_data(tmp_path):
    from yijianjifa.app_services.cloud_voice_server_test import prepare_server_test_client
    from yijianjifa.app_services.cloud_voice_test import LOCAL_TEST_KIND, prepare_test_client
    original = tmp_path / 'original'; original.mkdir()
    (original / 'app.db').write_bytes(b'original')
    descriptor = tmp_path / 'demo.json'
    descriptor.write_text(json.dumps({'kind': LOCAL_TEST_KIND, 'local_test': True,
                                      'endpoint': 'http://127.0.0.1:8768'}))
    demo = tmp_path / 'demo-client'; prepare_test_client(descriptor, demo)
    for data in (original, demo):
        before = {item.name: item.read_bytes() for item in data.iterdir()}
        with pytest.raises(ValueError):
            prepare_server_test_client(data)
        assert {item.name: item.read_bytes() for item in data.iterdir()} == before


def test_server_test_rejects_redirected_data_and_moved_markers(tmp_path):
    from yijianjifa.app_services.cloud_voice_server_test import prepare_server_test_client
    data = tmp_path / 'client'; prepare_server_test_client(data)
    linked = tmp_path / 'linked'; linked.symlink_to(data, target_is_directory=True)
    with pytest.raises(ValueError): prepare_server_test_client(linked)
    other = tmp_path / 'original.db'; other.write_bytes(b'keep')
    (data / 'app.db').symlink_to(other)
    with pytest.raises(ValueError): prepare_server_test_client(data)
    (data / 'app.db').unlink()
    moved = tmp_path / 'moved'; data.rename(moved)
    with pytest.raises(ValueError): prepare_server_test_client(moved)
    assert other.read_bytes() == b'keep'


def test_server_test_rejects_packaged_and_relative_startup_without_writes(tmp_path, monkeypatch):
    from yijianjifa.app_services.cloud_voice_server_test import prepare_server_test_client
    with pytest.raises(ValueError): prepare_server_test_client('client')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    data = tmp_path / 'frozen-client'
    with pytest.raises(ValueError): prepare_server_test_client(data)
    assert not data.exists()


def test_server_test_account_vault_cannot_reuse_demo_or_original_session(tmp_path):
    from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess
    from yijianjifa.app_services.cloud_voice_server_test import prepare_server_test_client
    class Vault:
        def __init__(self): self.values = {}
        def get_password(self, service, key): return self.values.get((service, key))
        def set_password(self, service, key, value): self.values[service, key] = value
    vault = Vault()
    local = CloudVoiceAccess(SimpleNamespace(data_dir=tmp_path / 'local'),
                             endpoint='http://127.0.0.1:8768', local_test=True, vault=vault)
    local._save_session(dict(account_id='local-id', username='demo',
                            access_token='test-access', refresh_token='test-refresh'))
    data = tmp_path / 'server'
    endpoint = prepare_server_test_client(data)
    remote = CloudVoiceAccess(SimpleNamespace(data_dir=data), endpoint=endpoint, vault=vault)
    assert local.identity()['account_id'] == 'local-id'
    assert remote.identity() == {'account_id': None, 'username': None}
    state = remote.read()
    assert state['enabled'] is True
    assert state['authenticated'] is False
    assert state['local_test'] is False
