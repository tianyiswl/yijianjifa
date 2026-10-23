import http.client
import ipaddress
import secrets
import shutil
import socket
import ssl
import threading
import time
import os
import tempfile
import queue
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin
from .contracts import CancelToken, DownloadError, MediaSource, Progress
from .input import validate_url

MAX_BYTES = 2 * 1024**3
SPACE_MARGIN = 256 * 1024**2
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 60
TOTAL_TIMEOUT = 30 * 60

_active_guard = threading.local()
_ALLOWED_HEADERS = {name.lower(): name for name in ('User-Agent', 'Accept', 'Referer', 'Cookie', 'Authorization')}
_DEFAULT_HEADERS = {'User-Agent': 'Yijianjifa-Download/1', 'Accept': 'video/mp4',
                    'Accept-Encoding': 'identity', 'Connection': 'close'}


def _trusted_headers(headers: dict[str, str]) -> dict[str, str]:
    result = {}
    for name, value in headers.items():
        if (not isinstance(name, str) or name.lower() not in _ALLOWED_HEADERS
                or not isinstance(value, str)
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise DownloadError('unsafe_source')
        try:
            value.encode('latin-1')
        except UnicodeError:
            raise DownloadError('unsafe_source') from None
        canonical = _ALLOWED_HEADERS[name.lower()]
        if canonical in result:
            raise DownloadError('unsafe_source')
        result[canonical] = value
    return result


def _origin(url: str) -> tuple[str, str, int]:
    parts = validate_url(url)
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == 'https' else 80)


class _OperationGuard:
    """Interrupt blocked network I/O instead of waiting for parsing to return."""
    def __init__(self, cancel: CancelToken):
        self.cancel = cancel
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.phase_deadline: float | None = None
        self.error: str | None = None
        self.sock = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()

    def _watch(self):
        while not self.stopped.wait(0.01):
            try:
                self.check()
            except DownloadError:
                return

    def check(self):
        with self.lock:
            now = time.monotonic()
            if self.error is None:
                if self.cancel.cancelled():
                    self.error = 'cancelled'
                elif now >= self.deadline or (self.phase_deadline is not None and now >= self.phase_deadline):
                    self.error = 'timeout'
            if self.error is not None:
                if self.sock is not None:
                    try: self.sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    self.sock.close()
                raise DownloadError(self.error) from None

    def attach(self, sock):
        with self.lock:
            self.sock = sock
        self.check()

    def connecting(self):
        with self.lock:
            self.phase_deadline = time.monotonic() + CONNECT_TIMEOUT
        self.check()

    def connected(self):
        with self.lock:
            self.phase_deadline = None
        self.check()

    def close(self):
        self.stopped.set()
        self.watcher.join()


class SourceRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sources: dict[str, tuple[str, dict[str,str]]] = {}
    def put(self, url: str, *, headers: dict[str,str]) -> MediaSource:
        validate_url(url)
        headers = _trusted_headers(headers)
        handle = secrets.token_urlsafe(32)
        with self._lock: self._sources[handle] = (url, dict(headers))
        return MediaSource(handle)
    def lookup(self, source: MediaSource) -> tuple[str, dict[str,str]]:
        with self._lock:
            try:
                url, headers = self._sources[source.handle]
                return url, dict(headers)
            except KeyError: raise DownloadError('source_missing') from None
    def revoke(self, source: MediaSource) -> None:
        with self._lock: self._sources.pop(source.handle, None)

class PublicConnector:
    def __init__(self, *, resolver=None):
        self.resolver = resolver

    def open_tcp(self, hostname: str, port: int) -> socket.socket:
        if port not in (80,443): raise DownloadError('unsafe_source')
        started = time.monotonic()
        resolved = queue.Queue(maxsize=1)
        guard = getattr(_active_guard, 'value', None)
        try: numeric_host = ipaddress.ip_address(hostname)
        except ValueError: numeric_host = None
        def resolve():
            try:
                answers = (self.resolver.resolve(hostname, port, guard=guard)
                           if self.resolver is not None and numeric_host is None else socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM))
                resolved.put(answers)
            except Exception as error: resolved.put(error)
        threading.Thread(target=resolve, daemon=True).start()
        guard = getattr(_active_guard, 'value', None)
        while True:
            if guard is not None: guard.check()
            remaining = CONNECT_TIMEOUT - (time.monotonic() - started)
            if remaining <= 0: raise DownloadError('timeout')
            try:
                answers = resolved.get(timeout=min(0.05, remaining))
                break
            except queue.Empty:
                continue
        if isinstance(answers, DownloadError): raise DownloadError(answers.code) from None
        if isinstance(answers, Exception): raise DownloadError('network') from None
        if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
            raise DownloadError('unsafe_source')
        family, kind, protocol, _, address = answers[0]
        sock = socket.socket(family,kind,protocol)
        try:
            if guard is not None: guard.attach(sock)
            sock.settimeout(max(0.001, CONNECT_TIMEOUT - (time.monotonic() - started)))
            sock.connect(address)  # Numeric address: never resolve hostname a second time.
            return sock
        except BaseException:
            sock.close(); raise

class _Transport:
    def __init__(self, connector):
        self.connector=connector
        self.remaining=TOTAL_TIMEOUT
    def open(self, url, *, headers=None, guard=None):
        started = time.monotonic()
        parts = validate_url(url)
        host = parts.hostname
        if guard is not None: guard.connecting()
        _active_guard.value = guard
        try:
            sock = self.connector.open_tcp(host, parts.port or (443 if parts.scheme=='https' else 80))
        finally:
            _active_guard.value = None
        try:
            if guard is not None: guard.attach(sock)
            connect_remaining = min(CONNECT_TIMEOUT, self.remaining) - (time.monotonic() - started)
            if connect_remaining <= 0: raise DownloadError('timeout')
            sock.settimeout(connect_remaining)
            if parts.scheme=='https':
                sock=ssl.create_default_context().wrap_socket(sock,server_hostname=host,do_handshake_on_connect=False)
                if guard is not None: guard.attach(sock)
                sock.do_handshake()
            if guard is not None: guard.connected()
            sock.settimeout(min(IDLE_TIMEOUT, self.remaining))
            connection=http.client.HTTPConnection(host)
            connection.sock=sock
            path=parts.path or '/'
            if parts.query: path+='?'+parts.query
            # Preserve every ASCII byte, existing escape and signed query ordering.
            # Only Unicode code points become UTF-8 escapes for the ASCII wire line.
            path = ''.join(char if ord(char) < 128 else quote(char, safe='') for char in path)
            request_headers = dict(_DEFAULT_HEADERS)
            request_headers.update(headers or {})
            connection.request('GET',path,headers=request_headers)
            return connection.getresponse()
        except BaseException:
            sock.close(); raise

class SecureFetcher:
    def __init__(self, registry: SourceRegistry, *, connector=None, transport=None, max_bytes=MAX_BYTES, free_space=None, clock=time.monotonic):
        self.registry=registry
        self.transport=transport or _Transport(connector or PublicConnector())
        self.max_bytes=max_bytes
        self.free_space=free_space or (lambda p:shutil.disk_usage(p).free)
        self.clock=clock
    def download(self, source: MediaSource, destination: Path, *, cancel: CancelToken, progress: Callable[[Progress],None]) -> None:
        started=self.clock(); temporary=None; response=None
        guard = _OperationGuard(cancel)
        def check():
            guard.check()
            if cancel.cancelled(): raise DownloadError('cancelled')
            if self.clock()-started>=TOTAL_TIMEOUT: raise DownloadError('timeout')
        try:
            check()
            url,headers=self.registry.lookup(source)
            for hop in range(6):
                check(); validate_url(url)
                if isinstance(self.transport, _Transport): self.transport.remaining=max(0.001,TOTAL_TIMEOUT-(self.clock()-started))
                if isinstance(self.transport, _Transport):
                    response=self.transport.open(url, headers=headers, guard=guard)
                else:
                    response=self.transport.open(url)
                check()
                if response.status in (301,302,303,307,308):
                    location=response.getheader('Location')
                    response.close(); response=None
                    if hop==5: raise DownloadError('redirect_limit')
                    if not location: raise DownloadError('network')
                    next_url=urljoin(url,location); validate_url(next_url)
                    if _origin(next_url) != _origin(url):
                        headers={key:value for key,value in headers.items() if key not in ('Cookie','Authorization')}
                    url=next_url
                    continue
                break
            if response.status!=200: raise DownloadError('network')
            length=response.getheader('Content-Length')
            try: total=int(length) if length is not None else None
            except ValueError: raise DownloadError('network') from None
            if total is not None and (total<0 or total>self.max_bytes): raise DownloadError('size_limit')
            required=(total if total is not None else self.max_bytes)+SPACE_MARGIN
            if self.free_space(destination.parent)<required: raise DownloadError('disk_space')
            descriptor,name=tempfile.mkstemp(prefix='.download-',suffix='.part',dir=destination.parent)
            temporary=Path(name)
            current=0
            with os.fdopen(descriptor,'wb') as output:
                while True:
                    check()
                    if isinstance(self.transport, _Transport):
                        # Keep a blocked body read inside the overall deadline.
                        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                        if sock is not None:
                            sock.settimeout(min(IDLE_TIMEOUT, max(0.001, TOTAL_TIMEOUT-(self.clock()-started))))
                    reader = response.read1 if isinstance(response, http.client.HTTPResponse) else response.read
                    chunk=reader(64*1024); check()
                    if not chunk: break
                    current+=len(chunk)
                    if current>self.max_bytes: raise DownloadError('size_limit')
                    if self.free_space(destination.parent)<len(chunk)+SPACE_MARGIN: raise DownloadError('disk_space')
                    output.write(chunk); progress(Progress('download',current,total))
            if total is not None and current!=total: raise DownloadError('network')
            check(); temporary.replace(destination); temporary=None
        except (socket.timeout, TimeoutError):
            guard.check()
            raise DownloadError('timeout') from None
        except DownloadError: raise
        except (OSError,http.client.HTTPException,UnicodeError):
            guard.check()
            raise DownloadError('network') from None
        finally:
            guard.close()
            if response is not None: response.close()
            if temporary is not None: temporary.unlink(missing_ok=True)


class _CloudflareBootstrap:
    """A numeric public bootstrap; never recursively use the injected resolver."""
    def open_tcp(self, hostname, port):
        if hostname != 'cloudflare-dns.com' or port != 443:
            raise DownloadError('unsafe_source')
        return PublicConnector().open_tcp('1.1.1.1', 443)


class CloudflareDoHResolver:
    """Explicit probe opt-in only. Default PublicConnector never uses DoH."""
    def __init__(self, *, transport=None):
        self.transport = transport

    def resolve(self, hostname, port, *, guard=None):
        import json
        import re
        from urllib.parse import urlencode
        if (port not in (80,443) or not isinstance(hostname,str)
                or not re.fullmatch(r'(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',hostname)
                or '.' not in hostname or hostname.lower().endswith(('.local','.localhost'))):
            raise DownloadError('unsafe_source')
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise DownloadError('unsafe_source')
        class Token:
            def cancelled(self): return guard is not None and (guard.error is not None or guard.cancel.cancelled())
        dns_guard = _OperationGuard(Token())
        dns_guard.deadline = time.monotonic() + CONNECT_TIMEOUT
        answers = []
        try:
            for qtype, family, record_type in [('A',socket.AF_INET,1),('AAAA',socket.AF_INET6,28)]:
                dns_guard.check()
                transport = self.transport or _Transport(_CloudflareBootstrap())
                transport.remaining = max(.001,dns_guard.deadline-time.monotonic())
                url = 'https://cloudflare-dns.com/dns-query?' + urlencode({'name':hostname,'type':qtype})
                response = None
                try:
                    response = transport.open(url,headers={'Accept':'application/dns-json'},guard=dns_guard)
                    if response.status != 200: raise DownloadError('network')  # No redirects.
                    if 'application/dns-json' not in response.getheader('Content-Type').lower(): raise DownloadError('network')
                    length = response.getheader('Content-Length')
                    if length is not None and not 0<=int(length)<=65536: raise DownloadError('size_limit')
                    raw = response.read(65537)
                    dns_guard.check()
                    if len(raw)>65536: raise DownloadError('size_limit')
                    payload = json.loads(raw)
                    if not isinstance(payload,dict) or type(payload.get('Status')) is not int or payload.get('Status') != 0: raise DownloadError('network')
                    records = payload.get('Answer',[])
                    if not isinstance(records,list) or len(records)>128: raise DownloadError('network')
                    for record in records:
                        if not isinstance(record,dict): raise DownloadError('network')
                        kind = record.get('type')
                        if type(kind) is not int: raise DownloadError('network')
                        if kind == 5: continue  # CNAME text is never followed locally.
                        if kind not in (1,28): raise DownloadError('network')
                        address = ipaddress.ip_address(record['data'])
                        if not address.is_global: raise DownloadError('unsafe_source')
                        if kind != record_type or address.version != (4 if kind==1 else 6): raise DownloadError('network')
                        target = (str(address),port) if kind==1 else (str(address),port,0,0)
                        answers.append((family,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',target))
                finally:
                    if response is not None: response.close()
            if not answers: raise DownloadError('network')
            return answers
        except DownloadError: raise
        except (socket.timeout,TimeoutError):
            dns_guard.check()
            raise DownloadError('timeout') from None
        except Exception:
            dns_guard.check()
            raise DownloadError('network') from None
        finally:
            dns_guard.close()
