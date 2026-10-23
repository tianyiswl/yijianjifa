"""Bounded, credential-free retrieval of an already generated supplier audio job."""
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from .hosted import VoicePaused


_MAX_BYTES = 32 * 1024 * 1024
_CHUNK = 64 * 1024
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_FAKE_V4 = ipaddress.ip_network('198.18.0.0/15')
_TRANSLATED_V4 = ipaddress.ip_network('::ffff:0:0:0/96')
_MESSAGES = {
    'audio_link_expired': '原配音下载地址已过期，请重新获取原结果；不会重新生成或重复扣字数。',
    'invalid_audio': '原配音文件不完整或大小不符合要求，请重新获取原结果；不会重复扣字数。',
    'audio_download_failed': '原配音下载未完成，请重新获取原结果；不会重新生成或重复扣字数。',
}


class _InvalidAudio(Exception):
    pass


def _remaining(deadline, clock=time.monotonic):
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def _global_address(ip):
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _dns_name(value):
    if not isinstance(value, str):
        raise ValueError()
    value = value.removesuffix('.').lower()
    if not value or len(value) > 253 or any(not _LABEL.fullmatch(label) for label in value.split('.')):
        raise ValueError()
    return value


def _doh_answers(raw, host):
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get('Status')) is not int or value['Status'] != 0
            or value.get('TC', False) is not False):
        raise ValueError()
    questions = value.get('Question')
    if (not isinstance(questions, list) or len(questions) != 1 or not isinstance(questions[0], dict)
            or type(questions[0].get('type')) is not int or questions[0]['type'] != 1
            or _dns_name(questions[0].get('name')) != host):
        raise ValueError()
    records = value.get('Answer')
    if not isinstance(records, list) or not records:
        raise ValueError()
    aliases, addresses = {}, []
    for record in records:
        if not isinstance(record, dict) or type(record.get('type')) is not int:
            raise ValueError()
        owner = _dns_name(record.get('name'))
        if record['type'] == 5:
            target = _dns_name(record.get('data'))
            if owner in aliases and aliases[owner] != target:
                raise ValueError()
            aliases[owner] = target
        elif record['type'] == 1:
            if not isinstance(record.get('data'), str):
                raise ValueError()
            ip = ipaddress.ip_address(record.get('data'))
            if ip.version != 4 or not _global_address(ip):
                raise ValueError()
            addresses.append((owner, str(ip)))
        else:
            raise ValueError()
    chain, owner = set(), host
    while owner in aliases:
        if owner in chain:
            raise ValueError()
        chain.add(owner)
        owner = aliases[owner]
    chain.add(owner)
    if not addresses or set(aliases) - chain or any(owner not in chain for owner, _ in addresses):
        raise ValueError()
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))
            for ip in dict.fromkeys(ip for _, ip in addresses)]


def _doh_public_addresses(host, deadline):
    # Only this CDN hostname leaves the client, never the signed audio URL.
    host = _dns_name(host)
    if not any(host == suffix or host.endswith('.' + suffix) for suffix in ('bytespeech.com', 'bytetos.com')):
        raise OSError()
    deadline = min(deadline, time.monotonic() + 10)
    connection = http.client.HTTPSConnection('dns.google', timeout=_remaining(deadline),
                                              context=ssl.create_default_context())
    answers = queue.Queue(maxsize=1)
    def resolve():
        try:
            path = '/resolve?' + urlencode({'name': host, 'type': 'A', 'edns_client_subnet': '0.0.0.0/0'})
            connection.request('GET', path, headers={'Accept': 'application/dns-json'})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise OSError()
                raw = bytearray()
                while len(raw) <= 65536:
                    _remaining(deadline)
                    chunk = response.read1(min(4096, 65537 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                _remaining(deadline)
                if len(raw) > 65536:
                    raise OSError()
            answers.put(_doh_answers(raw, host))
        except Exception:
            answers.put(None)
        finally:
            connection.close()
    threading.Thread(target=resolve, daemon=True).start()
    try:
        result = answers.get(timeout=_remaining(deadline))
        if not result:
            raise OSError()
        return result
    except queue.Empty:
        raise TimeoutError() from None
    finally:
        sock = getattr(connection, 'sock', None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()


def _public_addresses(host, deadline):
    # DNS itself has no portable socket timeout. Bound the wait, then connect to
    # the checked numeric answer so a second DNS resolution cannot change it.
    answers = queue.Queue(maxsize=1)
    def resolve():
        try:
            answers.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP))
        except Exception:
            answers.put(None)
    threading.Thread(target=resolve, daemon=True).start()
    try:
        result = answers.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TimeoutError() from None
    if not result:
        raise OSError()
    fake = False
    for family, kind, protocol, _, address in result:
        try:
            ip = ipaddress.ip_address(address[0])
            fake_ip = (ip.version == 4 and ip in _FAKE_V4 or ip.version == 6 and ip in _TRANSLATED_V4
                       and ipaddress.IPv4Address(int(ip) & 0xffffffff) in _FAKE_V4)
            safe = _global_address(ip) or fake_ip
        except ValueError:
            safe = False
        if not safe or family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
            raise OSError()
        fake = fake or fake_ip
    # Proxy fake-IP answers are never connected to or accepted as public. Other
    # private/mixed answers still fail above, without sending an external query.
    return _doh_public_addresses(host, deadline) if fake else result


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, deadline):
        super().__init__(host, 443, timeout=_remaining(deadline))
        self.deadline, self.active_socket = deadline, None

    def abort(self):
        sock = self.active_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def connect(self):
        for family, kind, protocol, _, address in _public_addresses(self.host, self.deadline):
            raw = socket.socket(family, kind, protocol)
            self.active_socket = raw
            try:
                raw.settimeout(_remaining(self.deadline))
                raw.connect(address)
                secure = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
                self.active_socket = secure
                secure.settimeout(_remaining(self.deadline))
                secure.do_handshake()
                _remaining(self.deadline)
                self.sock = secure
                return
            except OSError:
                self.abort()
        raise OSError()


class _Response:
    def __init__(self, response, connection, timer):
        self.response, self.connection, self.timer = response, connection, timer
        self.headers, self.status = response.headers, response.status

    def read(self, size):
        self.connection.active_socket.settimeout(_remaining(self.connection.deadline))
        chunk = self.response.read(size)
        _remaining(self.connection.deadline)
        return chunk

    def close(self):
        self.timer.cancel()
        try:
            self.response.close()
        finally:
            self.connection.abort()
            self.connection.close()


def _open_public_audio(request, *, timeout):
    """Direct HTTPS only: no redirects, proxy environment, cookies or auth hooks."""
    parts = urlsplit(request.full_url)
    connection = _PublicHTTPSConnection(parts.hostname, time.monotonic() + timeout)
    timer = threading.Timer(timeout, connection.abort)
    timer.daemon = True
    timer.start()
    try:
        target = (parts.path or '/') + ('?' + parts.query if parts.query else '')
        connection.request('GET', target, headers=dict(request.header_items()))
        _remaining(connection.deadline)
        return _Response(connection.getresponse(), connection, timer)
    except Exception:
        timer.cancel()
        connection.abort()
        connection.close()
        raise


class SupplierAudioDownloader:
    def __init__(self, *, opener=None, timeout=60, max_bytes=_MAX_BYTES,
                 clock=time.monotonic, wall_clock=time.time):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0
                or type(max_bytes) is not int or not 0 < max_bytes <= _MAX_BYTES):
            raise ValueError('配音下载时限或大小上限无效。')
        # An injected opener is an explicitly trusted test/transport boundary.
        # Default production IO always validates and pins public DNS answers.
        self.opener = _open_public_audio if opener is None else opener if callable(opener) else opener.open
        self.timeout, self.max_bytes = timeout, max_bytes
        self.clock, self.wall_clock = clock, wall_clock

    def _descriptor(self, value):
        if not isinstance(value, dict) or value.get('kind') != 'supplier_url':
            raise _InvalidAudio()
        job, url, expiry, maximum = (value.get(key) for key in ('job_id', 'url', 'expires_at', 'max_bytes'))
        if (not isinstance(job, str) or not job.strip() or len(job) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in job)
                or type(maximum) is not int or not 0 < maximum <= _MAX_BYTES
                or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry) or expiry <= 0
                or not isinstance(url, str) or not url or len(url) > 8192
                or any(not 33 <= ord(char) <= 126 for char in url)
                or '\\' in url or '#' in url or re.search(r'%(?![0-9a-f]{2})', url, re.I)
                or re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)', url, re.I)):
            raise _InvalidAudio()
        try:
            parts = urlsplit(url)
            host = parts.hostname or ''
            allowed = any(host == domain or host.endswith('.' + domain) for domain in ('bytespeech.com', 'bytetos.com'))
            labels = host.split('.')
            if (parts.scheme != 'https' or len(host) > 253 or not allowed
                    or any(not _LABEL.fullmatch(label) for label in labels)
                    or parts.netloc.lower() not in {host, host + ':443'}
                    or parts.port not in (None, 443) or parts.username is not None or parts.password is not None):
                raise ValueError()
        except ValueError:
            raise _InvalidAudio() from None
        return url, expiry, min(maximum, self.max_bytes)

    def download(self, descriptor, destination: Path) -> None:
        target, created, response = Path(destination), False, None
        try:
            url, expiry, limit = self._descriptor(descriptor)
            if expiry <= self.wall_clock():
                raise HTTPError('', 403, '', {}, None)
            deadline = self.clock() + self.timeout
            request = Request(url, headers={'Accept': 'audio/*'}, method='GET')
            response = self.opener(request, timeout=_remaining(deadline, self.clock))
            _remaining(deadline, self.clock)
            status = getattr(response, 'status', 200)
            if status != 200:
                raise HTTPError('', status, '', {}, None)
            all_lengths = getattr(response.headers, 'get_all', None)
            if callable(all_lengths) and len(all_lengths('Content-Length') or []) > 1:
                raise _InvalidAudio()
            length = response.headers.get('Content-Length')
            if length is not None:
                if not isinstance(length, str) or not re.fullmatch(r'[0-9]+', length.strip()):
                    raise _InvalidAudio()
                length = int(length)
                if not 0 < length <= limit:
                    raise _InvalidAudio()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created = True
            with os.fdopen(fd, 'wb') as output:
                total = 0
                while True:
                    _remaining(deadline, self.clock)
                    chunk = response.read(min(_CHUNK, limit - total + 1))
                    _remaining(deadline, self.clock)
                    if not isinstance(chunk, bytes):
                        raise _InvalidAudio()
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise _InvalidAudio()
                    output.write(chunk)
                if not total or (length is not None and total != length):
                    raise _InvalidAudio()
                output.flush()
                os.fsync(output.fileno())
                _remaining(deadline, self.clock)
        except Exception as error:
            if created:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            code = ('audio_link_expired' if isinstance(error, HTTPError) and error.code in (401, 403)
                    else 'invalid_audio' if isinstance(error, _InvalidAudio) else 'audio_download_failed')
            if isinstance(error, HTTPError):
                try:
                    error.close()
                except Exception:
                    pass
            raise VoicePaused(code, _MESSAGES[code]) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
