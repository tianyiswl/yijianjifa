"""Supplier audio retrieval never forwards cloud credentials or follows a URL hop."""
import io
import json
from email.message import Message
import os
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from yijianjifa.features.mixing.tts.hosted import VoicePaused


NOW = 1_700_000_000
URL = 'https://audio.bytespeech.com/result.mp3?signature=PRIVATE-SIGNATURE'


def descriptor(**changes):
    return {'kind': 'supplier_url', 'job_id': 'original-job', 'url': URL,
            'expires_at': NOW + 3600, 'max_bytes': 32 * 1024 * 1024, **changes}


class Response(io.BytesIO):
    def __init__(self, audio=b'audio-data', *, headers=None, status=200):
        super().__init__(audio)
        self.headers = headers or {}
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        assert 0 < size <= 65536, 'the downloader must not allocate the whole response'
        return super().read(size)


def downloader(opener, **options):
    from yijianjifa.features.mixing.tts.supplier_download import SupplierAudioDownloader
    return SupplierAudioDownloader(opener=opener, wall_clock=lambda: NOW, **options)


def test_audio_is_streamed_to_the_exact_private_destination_without_cloud_credentials(tmp_path):
    audio = b'a' * 150000
    response = Response(audio, headers={'Content-Length': str(len(audio))})
    calls = []
    def opener(request, *, timeout):
        calls.append((request, timeout))
        return response
    target = tmp_path / 'chosen.partial.mp3'
    downloader(opener).download(descriptor(), target)
    assert target.read_bytes() == audio
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o600
    request, timeout = calls[0]
    assert request.get_method() == 'GET' and request.full_url == URL
    assert {key.lower(): value for key, value in request.header_items()} == {'accept': 'audio/*'}
    # Deadline subtraction can round a few ulps above the 60-second budget.
    assert 0 < timeout <= 60 + 1e-9 and len(response.read_sizes) >= 3
    assert response.closed


@pytest.mark.parametrize('url', [
    'http://audio.bytespeech.com/audio', 'https://127.0.0.1/audio',
    'https://bytespeech.com.evil.example/audio', 'https://evilbytespeech.com/audio',
    'https://user:secret@audio.bytespeech.com/audio', 'https://audio.bytespeech.com:444/audio',
    'https://audio.bytespeech.com/audio#fragment', 'https://audio.bytespeech.com./audio',
    'https://audio%2ebytespeech.com/audio', 'https://audio.bytespeech.com\\@127.0.0.1/audio',
    'https://音频.bytespeech.com/audio', 'https://audio.bytespeech.com/audio\nInjected: x',
])
def test_invalid_supplier_destinations_are_rejected_before_opening_or_writing(tmp_path, url):
    opener = Mock()
    target = tmp_path / 'download.partial'
    with pytest.raises(VoicePaused):
        downloader(opener).download(descriptor(url=url), target)
    opener.assert_not_called()
    assert not target.exists()


@pytest.mark.parametrize('url', ['https://bytespeech.com/a', 'https://bytetos.com/a',
                                 'https://bucket.tos.bytetos.com:443/a?token=x'])
def test_only_exact_allowed_domains_and_their_dot_subdomains_are_accepted(tmp_path, url):
    target = tmp_path / 'download.partial'
    downloader(lambda *a, **k: Response()).download(descriptor(url=url), target)
    assert target.read_bytes() == b'audio-data'


@pytest.mark.parametrize('encoded', ['%', '%0', '%gg', '%0f', '%09', '%1f', '%1B', '%7f', '%5C'])
def test_supplier_urls_reject_malformed_escapes_and_encoded_control_or_backslash(tmp_path, encoded):
    opener = Mock()
    with pytest.raises(VoicePaused) as error:
        downloader(opener).download(descriptor(url='https://audio.bytetos.com/result?token=' + encoded), tmp_path / 'partial')
    assert error.value.code == 'invalid_audio'
    opener.assert_not_called()


@pytest.mark.parametrize('length,allowed', [(8192, True), (8193, False)])
def test_signed_url_length_boundary_matches_supplier_policy(tmp_path, length, allowed):
    base = 'https://audio.bytespeech.com/result?token='
    url = base + 'x' * (length - len(base))
    opener = Mock(return_value=Response())
    target = tmp_path / 'partial'
    if allowed:
        downloader(opener).download(descriptor(url=url), target)
        assert target.read_bytes() == b'audio-data'
    else:
        with pytest.raises(VoicePaused) as error:
            downloader(opener).download(descriptor(url=url), target)
        assert error.value.code == 'invalid_audio'
        opener.assert_not_called()


@pytest.mark.parametrize('label', ['-bad', 'bad-', 'bad_label', 'a' * 64, ''])
def test_each_subdomain_label_obeys_supplier_dns_policy(tmp_path, label):
    opener = Mock()
    with pytest.raises(VoicePaused) as error:
        downloader(opener).download(descriptor(url=f'https://{label}.bytespeech.com/result'), tmp_path / 'partial')
    assert error.value.code == 'invalid_audio'
    opener.assert_not_called()


def test_valid_encoded_signature_is_preserved_verbatim(tmp_path):
    url = 'https://bucket-1.bytetos.com/result?token=a%2Fb%2Bc%3D%25'
    seen = []
    def opener(request, **kwargs):
        seen.append(request.full_url)
        return Response()
    downloader(opener).download(descriptor(url=url), tmp_path / 'partial')
    assert seen == [url]


@pytest.mark.parametrize('changes', [
    {'kind': 'cloud_audio'}, {'job_id': ''}, {'job_id': None},
    {'expires_at': float('inf')}, {'expires_at': True},
    {'max_bytes': 33554433}, {'max_bytes': -1}, {'max_bytes': True},
])
def test_malformed_descriptors_cannot_relax_download_limits(tmp_path, changes):
    opener = Mock()
    with pytest.raises(VoicePaused):
        downloader(opener).download(descriptor(**changes), tmp_path / 'partial')
    opener.assert_not_called()


@pytest.mark.parametrize('expires_at', [NOW - 1, NOW])
def test_expired_descriptor_is_refreshable_without_network_or_partial(tmp_path, expires_at):
    opener = Mock()
    with pytest.raises(VoicePaused) as error:
        downloader(opener).download(descriptor(expires_at=expires_at), tmp_path / 'partial')
    assert error.value.code == 'audio_link_expired'
    opener.assert_not_called()
    assert not (tmp_path / 'partial').exists()


@pytest.mark.parametrize('status,code', [(401, 'audio_link_expired'), (403, 'audio_link_expired'),
                                      (302, 'audio_download_failed'), (503, 'audio_download_failed')])
def test_http_failure_is_sanitized_and_never_retried_or_redirected(tmp_path, status, code):
    calls = []
    def opener(request, **options):
        calls.append(request.full_url)
        raise HTTPError(URL, status, 'PRIVATE-ERROR', {'Location': 'http://127.0.0.1/private'}, None)
    with pytest.raises(VoicePaused) as error:
        downloader(opener).download(descriptor(), tmp_path / 'partial')
    assert error.value.code == code and len(calls) == 1
    assert all(secret not in str(error.value) for secret in ('PRIVATE', URL, '127.0.0.1'))
    assert error.value.__suppress_context__ and not (tmp_path / 'partial').exists()


@pytest.mark.parametrize('response', [Response(b''), Response(headers={'Content-Length': '0'}),
    Response(headers={'Content-Length': '-1'}), Response(headers={'Content-Length': 'bad'}),
    Response(headers={'Content-Length': '33554433'}), Response(b'a', headers={'Content-Length': '2'})])
def test_empty_invalid_or_truncated_audio_is_cleaned_up(tmp_path, response):
    target = tmp_path / 'partial'
    with pytest.raises(VoicePaused) as error:
        downloader(lambda *a, **k: response).download(descriptor(), target)
    assert error.value.code == 'invalid_audio'
    assert not target.exists() and response.closed


def test_limit_is_checked_even_without_content_length_and_partial_is_removed(tmp_path):
    response = Response(b'x' * 17)
    target = tmp_path / 'partial'
    with pytest.raises(VoicePaused) as error:
        downloader(lambda *a, **k: response, max_bytes=16).download(descriptor(), target)
    assert error.value.code == 'invalid_audio' and not target.exists()


def test_descriptor_may_narrow_but_not_expand_instance_limit(tmp_path):
    target = tmp_path / 'partial'
    with pytest.raises(VoicePaused) as error:
        downloader(lambda *a, **k: Response(b'12345'), max_bytes=16).download(descriptor(max_bytes=4), target)
    assert error.value.code == 'invalid_audio' and not target.exists()


def test_network_failure_does_not_reveal_signed_url_and_cleans_partial(tmp_path):
    class BrokenResponse(Response):
        def read(self, size=-1):
            if self.tell():
                raise URLError('PRIVATE-SIGNATURE ' + URL)
            return super().read(1)
    target = tmp_path / 'partial'
    with pytest.raises(VoicePaused) as error:
        downloader(lambda *a, **k: BrokenResponse()).download(descriptor(), target)
    assert error.value.code == 'audio_download_failed'
    assert 'PRIVATE' not in str(error.value) and not target.exists()


def test_total_deadline_stops_slow_chunk_progress(tmp_path):
    now = [0.0]
    class SlowResponse(Response):
        def read(self, size=-1):
            now[0] += .6
            return super().read(1)
    target = tmp_path / 'partial'
    with pytest.raises(VoicePaused) as error:
        downloader(lambda *a, **k: SlowResponse(), timeout=1, clock=lambda: now[0]).download(descriptor(), target)
    assert error.value.code == 'audio_download_failed' and not target.exists()


def test_existing_destination_is_never_overwritten_or_removed(tmp_path):
    target = tmp_path / 'existing'
    target.write_bytes(b'keep-existing')
    with pytest.raises(VoicePaused):
        downloader(lambda *a, **k: Response()).download(descriptor(), target)
    assert target.read_bytes() == b'keep-existing'


def test_disk_failure_removes_only_the_new_partial(tmp_path, monkeypatch):
    import os
    target, keep = tmp_path / 'partial', tmp_path / 'keep'
    keep.write_bytes(b'keep')
    monkeypatch.setattr(os, 'fsync', Mock(side_effect=OSError('PRIVATE-PATH')))
    with pytest.raises(VoicePaused) as error:
        downloader(lambda *a, **k: Response()).download(descriptor(), target)
    assert error.value.code == 'audio_download_failed'
    assert not target.exists() and keep.read_bytes() == b'keep'


def test_default_transport_refuses_dns_answers_with_any_nonpublic_ip(tmp_path, monkeypatch):
    import socket
    from yijianjifa.features.mixing.tts.supplier_download import SupplierAudioDownloader
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    connect = Mock(side_effect=AssertionError('must not connect to private DNS answers'))
    monkeypatch.setattr(socket, 'socket', connect)
    with pytest.raises(VoicePaused) as error:
        SupplierAudioDownloader(wall_clock=lambda: NOW).download(descriptor(), tmp_path / 'partial')
    assert error.value.code == 'audio_download_failed'
    connect.assert_not_called()


def test_duplicate_content_length_is_rejected_instead_of_accepting_one_header(tmp_path):
    headers = Message()
    headers.add_header('Content-Length', '1')
    headers.add_header('Content-Length', '1000000')
    target = tmp_path / 'partial'
    with pytest.raises(VoicePaused) as error:
        downloader(lambda *a, **k: Response(b'a', headers=headers)).download(descriptor(), target)
    assert error.value.code == 'invalid_audio' and not target.exists()


def test_production_connection_pins_public_dns_answer_and_keeps_tls_hostname(monkeypatch):
    import socket
    import time
    from yijianjifa.features.mixing.tts.supplier_download import _PublicHTTPSConnection
    answers = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('9.9.9.9', 443))])
    monkeypatch.setattr(socket, 'getaddrinfo', answers)
    connection = _PublicHTTPSConnection('audio.bytespeech.com', time.monotonic() + 10)
    raw, secure, context = Mock(), Mock(), Mock()
    monkeypatch.setattr(socket, 'socket', lambda *a: raw)
    context.wrap_socket.return_value = secure
    connection._context = context
    connection.connect()
    assert answers.call_count == 1
    raw.connect.assert_called_once_with(('9.9.9.9', 443))
    context.wrap_socket.assert_called_once_with(raw, server_hostname='audio.bytespeech.com', do_handshake_on_connect=False)
    assert connection.sock is secure
    secure.do_handshake.assert_called_once_with()
    connection.abort()


def test_default_opener_does_not_follow_redirect_or_load_proxy_credentials(tmp_path, monkeypatch):
    from yijianjifa.features.mixing.tts import supplier_download
    connections, requests = [], []
    class Connection:
        def __init__(self, host, deadline):
            self.deadline = deadline
            connections.append(host)
        def request(self, method, path, *, headers):
            requests.append((method, path, headers))
        def getresponse(self):
            return Response(status=302, headers={'Location': 'https://other.bytetos.com/private'})
        def abort(self): pass
        def close(self): pass
    monkeypatch.setenv('HTTPS_PROXY', 'http://secret-user:PRIVATE-PROXY@127.0.0.1:3128')
    monkeypatch.setattr(supplier_download, '_PublicHTTPSConnection', Connection)
    with pytest.raises(VoicePaused) as error:
        supplier_download.SupplierAudioDownloader(wall_clock=lambda: NOW).download(descriptor(), tmp_path / 'partial')
    assert error.value.code == 'audio_download_failed'
    assert connections == ['audio.bytespeech.com']
    assert requests == [('GET', '/result.mp3?signature=PRIVATE-SIGNATURE', {'Accept': 'audio/*'})]


def test_symbolic_destination_never_changes_or_removes_the_existing_file(tmp_path):
    original = tmp_path / 'original'
    original.write_bytes(b'original')
    target = tmp_path / 'partial'
    target.symlink_to(original)
    with pytest.raises(VoicePaused):
        downloader(lambda *a, **k: Response()).download(descriptor(), target)
    assert original.read_bytes() == b'original' and target.is_symlink()


def dns_answer(address):
    import socket
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    endpoint = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', endpoint)


def doh_reply(**changes):
    return {'Status': 0, 'TC': False,
            'Question': [{'name': 'audio.bytespeech.com.', 'type': 1}],
            'Answer': [{'name': 'audio.bytespeech.com.', 'type': 5, 'TTL': 60,
                        'data': 'cdn.example.net.'},
                       {'name': 'cdn.example.net.', 'type': 1, 'TTL': 60,
                        'data': '9.9.9.9'}], **changes}


def install_doh(monkeypatch, payload=None, *, status=200, raw=None, failure=None):
    from yijianjifa.features.mixing.tts import supplier_download
    calls = []
    class Connection:
        def __init__(self, host, *, timeout, context):
            calls.append({'host': host, 'timeout': timeout, 'context': context})
        def request(self, method, path, *, headers):
            calls[-1].update(method=method, path=path, headers=headers)
        def getresponse(self):
            if failure:
                raise failure
            response = io.BytesIO(raw if raw is not None else json.dumps(payload or doh_reply()).encode())
            response.status = status
            return response
        def close(self):
            calls[-1]['closed'] = True
    monkeypatch.setattr(supplier_download.http.client, 'HTTPSConnection', Connection)
    return calls


@pytest.mark.parametrize('addresses', [
    ['198.18.0.239'], ['::ffff:0:c612:ef'],
    ['198.18.0.239', '::ffff:0:c612:ef'],
    ['9.9.9.10', '198.19.255.254'],
])
def test_fake_dns_uses_only_hostname_in_strict_bounded_https_lookup(monkeypatch, addresses):
    import socket
    import ssl
    import time
    from urllib.parse import parse_qs, urlsplit
    from yijianjifa.features.mixing.tts import supplier_download
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [dns_answer(ip) for ip in addresses])
    monkeypatch.setenv('HTTPS_PROXY', 'http://user:PRIVATE-PROXY@127.0.0.1:3128')
    calls = install_doh(monkeypatch)
    result = supplier_download._public_addresses('audio.bytespeech.com', time.monotonic() + 1)
    assert result == [dns_answer('9.9.9.9')]
    assert len(calls) == 1
    call = calls[0]
    assert call['host'] == 'dns.google' and call['method'] == 'GET'
    assert 0 < call['timeout'] <= 1
    assert call['context'].check_hostname is True
    assert call['context'].verify_mode == ssl.CERT_REQUIRED
    parts = urlsplit(call['path'])
    assert parts.path == '/resolve'
    assert parse_qs(parts.query) == {'name': ['audio.bytespeech.com'], 'type': ['A'],
                                      'edns_client_subnet': ['0.0.0.0/0']}
    assert call['headers'] == {'Accept': 'application/dns-json'} and call['closed']
    assert 'PRIVATE' not in call['path'] and 'signature' not in call['path']


def test_normal_public_dns_does_not_send_a_doh_query(monkeypatch):
    import socket
    import time
    from yijianjifa.features.mixing.tts import supplier_download
    answers = [dns_answer('9.9.9.9'), dns_answer('2606:4700:4700::1111')]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: answers)
    calls = install_doh(monkeypatch)
    assert supplier_download._public_addresses('audio.bytespeech.com', time.monotonic() + 1) == answers
    assert calls == []


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
                                  '100.64.0.1', '192.0.2.1', '::1', 'fd00::1',
                                  '::ffff:0:a00:1', '::ffff:c612:ef'])
def test_fake_ip_mixed_with_other_nonpublic_dns_is_still_rejected(monkeypatch, address):
    import socket
    import time
    from yijianjifa.features.mixing.tts import supplier_download
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [dns_answer('198.18.0.239'), dns_answer(address)])
    calls = install_doh(monkeypatch)
    with pytest.raises(OSError):
        supplier_download._public_addresses('audio.bytespeech.com', time.monotonic() + 1)
    assert calls == []


@pytest.mark.parametrize('changes', [
    {'Status': 3}, {'Status': True}, {'TC': True},
    {'Question': [{'name': 'other.bytespeech.com.', 'type': 1}]},
    {'Question': [{'name': 'audio.bytespeech.com.', 'type': 28}]},
    {'Question': [{'name': 'audio.bytespeech.com.', 'type': True}]},
    {'Question': []}, {'Answer': []},
    {'Answer': [{'name': 'audio.bytespeech.com.', 'type': 1, 'data': '127.0.0.1'}]},
    {'Answer': [{'name': 'audio.bytespeech.com.', 'type': 1, 'data': '198.18.0.239'}]},
    {'Answer': [{'name': 'audio.bytespeech.com.', 'type': 1, 'data': 151587081}]},
    {'Answer': [{'name': 'audio.bytespeech.com.', 'type': 1, 'data': '9.9.9.9'},
                {'name': 'audio.bytespeech.com.', 'type': 1, 'data': '10.0.0.1'}]},
    {'Answer': [{'name': 'audio.bytespeech.com.', 'type': 1, 'data': '2606:4700:4700::1111'}]},
    {'Answer': [{'name': 'audio.bytespeech.com.', 'type': 28, 'data': '2606:4700:4700::1111'}]},
    {'Answer': [{'name': 'unrelated.example.', 'type': 1, 'data': '9.9.9.9'}]},
    {'Answer': [{'name': 'audio.bytespeech.com.', 'type': 5, 'data': 'https://bad.example/'}]},
])
def test_doh_requires_matching_complete_question_and_public_a_answers(monkeypatch, changes):
    import socket
    import time
    from yijianjifa.features.mixing.tts import supplier_download
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [dns_answer('198.18.0.239')])
    calls = install_doh(monkeypatch, doh_reply(**changes))
    with pytest.raises(OSError):
        supplier_download._public_addresses('audio.bytespeech.com', time.monotonic() + 1)
    assert len(calls) == 1 and calls[0]['closed']


@pytest.mark.parametrize('options', [
    {'status': 302}, {'status': 503}, {'raw': b'not JSON'}, {'raw': b' ' * 65537},
    {'failure': OSError('PRIVATE-TRANSPORT-DETAIL')},
])
def test_doh_failure_never_uses_original_fake_address(monkeypatch, options):
    import socket
    import time
    from yijianjifa.features.mixing.tts import supplier_download
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [dns_answer('198.18.0.239')])
    calls = install_doh(monkeypatch, **options)
    with pytest.raises(OSError):
        supplier_download._public_addresses('audio.bytespeech.com', time.monotonic() + 1)
    assert len(calls) == 1 and calls[0]['closed']


def test_doh_cannot_exceed_original_download_deadline(monkeypatch):
    import socket
    import threading
    import time
    from yijianjifa.features.mixing.tts import supplier_download
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [dns_answer('198.18.0.239')])
    release = threading.Event()
    class StalledConnection:
        def __init__(self, *a, **k): pass
        def request(self, *a, **k): release.wait(2)
        def close(self): pass
    monkeypatch.setattr(supplier_download.http.client, 'HTTPSConnection', StalledConnection)
    start = time.monotonic()
    try:
        with pytest.raises(OSError):
            supplier_download._public_addresses('audio.bytespeech.com', start + .05)
        assert time.monotonic() - start < .5
    finally:
        release.set()


def test_doh_uses_at_most_ten_seconds_of_a_long_download_budget(monkeypatch):
    import socket
    import time
    from yijianjifa.features.mixing.tts import supplier_download
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [dns_answer('198.18.0.239')])
    calls = install_doh(monkeypatch)
    assert supplier_download._public_addresses('audio.bytespeech.com', time.monotonic() + 60) == [dns_answer('9.9.9.9')]
    # Deadline subtraction can round a few ulps above ten on Windows.
    assert 0 < calls[0]['timeout'] <= 10 + 1e-9
