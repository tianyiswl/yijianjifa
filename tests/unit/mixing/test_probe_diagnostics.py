"""Media probing must survive Windows locales and identify unusable inputs."""
import io
import json
from pathlib import Path
import subprocess

import pytest

from yijianjifa.features.mixing import ffmpeg_renderer as probe


MATERIAL = Path('私人素材/河北玻璃钢排水沟厂家，U型排水槽支持定制.mp4')


def _output(monkeypatch, stdout):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b'')
    monkeypatch.setattr(probe.subprocess, 'run', run)


def _error(call, *, stage, reason_fragment=None, error_type=None):
    with pytest.raises(ValueError) as caught:
        call()
    error = caught.value
    assert type(error).__name__ == 'MediaProbeError'
    assert error.stage == stage
    assert error.material == MATERIAL.name
    assert error.reason and error.reason in str(error)
    assert MATERIAL.name in str(error)
    assert stage in str(error)
    assert str(MATERIAL.parent) not in str(error)
    assert error.diagnostic == {
        'stage': stage, 'material': MATERIAL.name,
        'reason': error.reason, 'error_type': error.error_type,
    }
    if reason_fragment:
        assert reason_fragment in error.reason
    if error_type:
        assert error.error_type == error_type
    return error


def test_ffprobe_utf8_metadata_survives_windows_gbk_reader(monkeypatch):
    """A locale-decoding reader crash used to become json.loads(None)."""
    raw = json.dumps({
        'streams': [{'index': 0, 'codec_type': 'video'}],
        'format': {'filename': str(MATERIAL), 'duration': '3.000000'},
    }, ensure_ascii=False).encode('utf-8')

    def windows_run(command, **kwargs):
        if kwargs.get('text') or kwargs.get('universal_newlines'):
            try:
                stdout = io.TextIOWrapper(io.BytesIO(raw), encoding=kwargs.get('encoding') or 'gbk').read()
            except UnicodeDecodeError:
                # Windows subprocess' reader thread fails before appending its
                # result, and communicate() then returns an empty buffer as None.
                stdout = None
        else:
            stdout = raw
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b'')

    monkeypatch.setattr(probe.subprocess, 'run', windows_run)
    result = probe._probe(MATERIAL)
    assert result['format']['filename'] == str(MATERIAL)
    assert result['format']['duration'] == '3.000000'


@pytest.mark.parametrize('as_bytes', [False, True])
def test_probe_supports_json_bytes_and_text_test_adapters(monkeypatch, as_bytes):
    data = '{"format":{"duration":"1.250"},"streams":[{"index":0}]}'
    _output(monkeypatch, data.encode('utf-8') if as_bytes else data)
    assert probe._probe(MATERIAL)['format']['duration'] == '1.250'
    assert probe._has_audio(MATERIAL) is True


@pytest.mark.parametrize('function,stage', [
    (probe._probe, '读取素材视频信息'),
    (probe._has_audio, '检查素材音轨'),
])
@pytest.mark.parametrize('stdout', [None, b'', b'  \n', ''])
def test_empty_probe_output_is_an_attributed_error(monkeypatch, function, stage, stdout):
    _output(monkeypatch, stdout)
    _error(lambda: function(MATERIAL), stage=stage,
           reason_fragment='没有返回', error_type='ProbeOutputError')


@pytest.mark.parametrize('stdout,error_type', [
    (b'not JSON', 'JSONDecodeError'),
    (b'\xff\xfe\xff', 'UnicodeDecodeError'),
])
def test_invalid_probe_json_has_material_and_readable_reason(monkeypatch, stdout, error_type):
    _output(monkeypatch, stdout)
    _error(lambda: probe._probe(MATERIAL), stage='读取素材视频信息',
           reason_fragment='数据', error_type=error_type)


@pytest.mark.parametrize('value', [None, [], {'format': None}, {'format': {}},
    {'format': {'duration': 'N/A'}}, {'format': {'duration': 0}},
    {'format': {'duration': '-1'}}, {'format': {'duration': 'NaN'}},
    {'format': {'duration': 'Infinity'}}, {'format': {'duration': True}},
    {'format': {'duration': '1'}, 'streams': None},
    {'format': {'duration': '1'}, 'streams': [None]},
])
def test_probe_rejects_invalid_structure_and_duration(monkeypatch, value):
    _output(monkeypatch, json.dumps(value).encode())
    _error(lambda: probe._probe(MATERIAL), stage='读取素材视频信息',
           error_type='ProbeSchemaError')


@pytest.mark.parametrize('value', [None, [], {}, {'streams': None},
    {'streams': [None]}, {'streams': [{}]}, {'streams': [{'index': -1}]},
    {'streams': [{'index': True}]},
])
def test_audio_probe_rejects_invalid_structure_instead_of_assuming_silence(monkeypatch, value):
    _output(monkeypatch, json.dumps(value).encode())
    _error(lambda: probe._has_audio(MATERIAL), stage='检查素材音轨',
           error_type='ProbeSchemaError')


def test_valid_no_audio_result_remains_silent(monkeypatch):
    _output(monkeypatch, b'{"streams": []}')
    assert probe._has_audio(MATERIAL) is False


def test_tool_failure_is_bounded_and_does_not_expose_full_source_path(monkeypatch):
    private = Path('/Users/private-owner/customer-source') / MATERIAL.name
    def failed(command, **kwargs):
        text = ('x' * 1000 + '\n' + str(private) + ': moov atom not found')
        raise subprocess.CalledProcessError(1, command, stderr=text.encode('utf-8'))
    monkeypatch.setattr(probe.subprocess, 'run', failed)
    error = _error(lambda: probe._probe(private), stage='读取素材视频信息',
                   reason_fragment='moov atom not found', error_type='CalledProcessError')
    assert '/Users/private-owner' not in str(error)
    assert len(error.reason) <= 500


@pytest.mark.parametrize('failure,error_type,reason', [
    (FileNotFoundError('private executable path'), 'FileNotFoundError', '工具'),
    (subprocess.TimeoutExpired(['ffprobe'], 30), 'TimeoutExpired', '超时'),
])
def test_probe_startup_and_timeout_errors_are_attributed(monkeypatch, failure, error_type, reason):
    def failed(command, **kwargs):
        raise failure
    monkeypatch.setattr(probe.subprocess, 'run', failed)
    _error(lambda: probe._has_audio(MATERIAL), stage='检查素材音轨',
           reason_fragment=reason, error_type=error_type)
