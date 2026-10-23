import importlib.util
import json
from pathlib import Path

import pytest


def collect(root, endpoint):
    source = Path(__file__).resolve().parents[2] / 'packaging/release_configuration.py'
    spec = importlib.util.spec_from_file_location('p011_release_configuration', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.cloud_voice_data(root, environ={} if endpoint is None else {'P011_RELEASE_CLOUD_VOICE_ENDPOINT': endpoint})


def test_unconfigured_build_does_not_bundle_stale_operator_settings(tmp_path):
    old = tmp_path / 'build/release-config/cloud-voice-service.json'
    old.parent.mkdir(parents=True)
    old.write_text('{"endpoint":"https://old.example.test"}')
    assert collect(tmp_path, None) == []
    assert collect(tmp_path, '') == []
    assert json.loads(old.read_text())['endpoint'] == 'https://old.example.test'


def test_explicit_official_build_writes_only_a_nonsecret_endpoint(tmp_path):
    datas = collect(tmp_path, 'https://voice.example.test/')
    assert len(datas) == 1 and datas[0][1] == '.'
    assert json.loads(Path(datas[0][0]).read_text()) == {'endpoint': 'https://voice.example.test'}


@pytest.mark.parametrize('endpoint', ['http://example.test', 'https://user:pass@example.test',
                                     'https://example.test?token=secret', 'https://example.test/#fragment',
                                     'https://example.test:not-a-port'])
def test_unsafe_service_metadata_refuses_to_build_without_writing_credentials(tmp_path, endpoint):
    with pytest.raises(ValueError, match='HTTPS'):
        collect(tmp_path, endpoint)
    assert not (tmp_path / 'build').exists()


@pytest.mark.parametrize('endpoint', [
    'https://example.test/\ud800', 'https://example.test\x00',
    'https://example.test/\x1f', 'https://example.test/\x7f',
    'https://example.test/\nother', 'https://exa\tmple.test',
    'https://example.test\n', 'https://example.test\t',
    'https://exam ple.test', 'https://exam\u00a0ple.test',
    'https://example.test?', 'https://example.test#', 'https://@example.test',
])
def test_malformed_endpoint_is_rejected_before_any_build_directory_or_file(tmp_path, endpoint):
    with pytest.raises(ValueError, match='HTTPS'):
        collect(tmp_path, endpoint)
    assert not (tmp_path / 'build').exists()


def test_invalid_build_endpoint_does_not_overwrite_previous_generated_configuration(tmp_path):
    target = tmp_path / 'build/release-config/cloud-voice-service.json'
    target.parent.mkdir(parents=True)
    original = '{"endpoint":"https://voice.example.test"}\n'
    target.write_text(original)
    with pytest.raises(ValueError, match='HTTPS'):
        collect(tmp_path, 'https://example.test/\ud800')
    assert target.read_text() == original
