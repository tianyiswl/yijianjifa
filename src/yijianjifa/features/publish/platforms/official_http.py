"""Wire primitives for the trusted official execution service, never public DTOs."""
from dataclasses import dataclass, field
import http.client
import io
import math
import ipaddress
import json
import re
import socket
import ssl
import time
from types import MappingProxyType
from urllib.parse import urlencode


class ProtocolError(ValueError):
    def __init__(self, code='adapter_failed'):
        super().__init__(code if code in {'adapter_failed','timeout','configuration_required','permission_missing','account_mismatch','result_unknown'} else 'adapter_failed')


def remaining(deadline):
    value=deadline-time.monotonic()
    if value<=0:raise ProtocolError('timeout')
    return min(value,10.0)


def text(value, *, limit=256):
    if type(value) is not str or not value or len(value)>limit or any(ord(c)<32 for c in value):raise ProtocolError()
    return value


def reference(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value):raise ProtocolError()
    return value


def json_bytes(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')


def json_object(raw):
    def pairs(items):
        obj={}
        for key,value in items:
            if key in obj:raise ProtocolError()
            obj[key]=value
        return obj
    def constant(_):raise ProtocolError()
    def finite_float(raw):
        number=float(raw)
        if not math.isfinite(number):raise ProtocolError()
        return number
    try:
        if type(raw) is not bytes or len(raw)>1048576:raise ProtocolError()
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant,parse_float=finite_float)
        if type(value) is not dict:raise ProtocolError()
        return value
    except (ValueError,UnicodeError,RecursionError):raise ProtocolError() from None


@dataclass(frozen=True)
class Request:
    method: str
    host: str = field(repr=False)
    path: str
    query: tuple = field(default=(),repr=False)
    body: bytes = field(default=b'',repr=False)
    headers: dict = field(default_factory=dict,repr=False)

    def __post_init__(self):
        if self.method not in {'GET','POST'} or type(self.body) is not bytes:raise ProtocolError()
        if not re.fullmatch(r'[a-z0-9.-]+',self.host) or not re.fullmatch(r'/[A-Za-z0-9_/-]+',self.path):raise ProtocolError()
        pairs=tuple(self.query)
        if any(type(k) is not str or type(v) is not str or any(c in k+v for c in '\r\n') for k,v in pairs):raise ProtocolError()
        headers=dict(self.headers)
        if len({k.lower() for k in headers})!=len(headers):raise ProtocolError()
        for key,value in headers.items():
            if not re.fullmatch(r'[A-Za-z0-9-]+',key) or type(value) is not str or any(c in value for c in '\r\n'):raise ProtocolError()
        object.__setattr__(self,'query',pairs);object.__setattr__(self,'headers',MappingProxyType(headers))


_ROUTES={
    'member.bilibili.com':{'/arcopen/fn/archive/video/init','/arcopen/fn/archive/video/complete','/arcopen/fn/archive/add-by-utoken','/arcopen/fn/archive/view','/arcopen/fn/archive/type/list','/arcopen/fn/user/account/info','/arcopen/fn/user/account/scopes'},
    'openupos.bilivideo.com':{'/video/v2/part/upload'},
    'open.kuaishou.com':{'/openapi/photo/start_upload','/openapi/photo/publish','/openapi/photo/info'},
}
_GATEWAY_ROUTES={'/api/upload/fragment','/api/upload/resume','/api/upload/complete'}


class _NumericTLSConnection(http.client.HTTPConnection):
    """No resolver: connect only to an operator-approved numeric address.

    HTTP authority, TLS SNI and certificate hostname verification retain the
    official hostname. The same deadline bounds connect and TLS handshake.
    """
    default_port=443

    def __init__(self,host,address,context,deadline):
        super().__init__(host,port=443,timeout=remaining(deadline))
        self._address,self._context,self._deadline=address,context,deadline

    def connect(self):
        family=socket.AF_INET6 if self._address.version==6 else socket.AF_INET
        self.sock=socket.socket(family,socket.SOCK_STREAM)
        try:
            self.sock.settimeout(remaining(self._deadline))
            self.sock.connect((str(self._address),443))
            self.sock.settimeout(remaining(self._deadline))
            self.sock=self._context.wrap_socket(self.sock,server_hostname=self.host)
            self.sock.settimeout(remaining(self._deadline))
        except BaseException:
            self.close()
            raise


class _DeadlineReader(io.RawIOBase):
    def __init__(self,sock,deadline):
        self.sock,self.deadline=sock,deadline
        # Preserve the socket file reference even when HTTPConnection closes its
        # handle for a Connection: close response before consuming its body.
        self.raw=sock.makefile('rb',buffering=0)
    def readable(self):return True
    def readinto(self,buffer):
        # HTTPResponse catches ValueError while parsing chunk sizes. An IO
        # deadline must use TimeoutError, otherwise it becomes IncompleteRead
        # and is incorrectly reported as a malformed response.
        try: timeout=remaining(self.deadline)
        except ProtocolError: raise TimeoutError() from None
        self.sock.settimeout(timeout)
        return self.raw.readinto(buffer)
    def close(self):
        try:self.raw.close()
        finally:super().close()


class _DeadlineSocket:
    def __init__(self,sock,deadline):self.sock,self.deadline=sock,deadline
    def __getattr__(self,name):return getattr(self.sock,name)
    def makefile(self,mode):
        if mode!='rb':raise ProtocolError()
        return io.BufferedReader(_DeadlineReader(self.sock,self.deadline))
    def sendall(self,data):
        view=memoryview(data)
        while view:
            self.sock.settimeout(remaining(self.deadline))
            sent=self.sock.send(view[:65536])
            if sent<=0:raise ProtocolError()
            view=view[sent:]


class HttpExecutor:
    """Synchronous, no proxies/redirects/retries. test_address is operator/test-only.

    A production host configures gateway_hosts only after confirming TLS and
    gateway ownership. Production additionally requires pre-resolved numeric
    addresses supplied by the trusted operator; absent mappings fail closed.
    No DNS runs on the publishing worker. Address refresh belongs to an external
    bounded/supervised operator process, never a detached job here. Neither this
    constructor nor overrides are browser APIs.
    """
    def __init__(self, *, gateway_hosts=(), test_address=None, resolved_addresses=None):
        self.gateway_hosts=frozenset(gateway_hosts);self.test_address=test_address
        for host in self.gateway_hosts:
            if not re.fullmatch(r'[a-z0-9.-]+',host) or host in _ROUTES:raise ProtocolError()
        if test_address is not None and (type(test_address) is not tuple or len(test_address)!=2 or test_address[0]!='127.0.0.1' or type(test_address[1]) is not int):raise ProtocolError()
        if resolved_addresses is not None and type(resolved_addresses) is not dict:raise ProtocolError('configuration_required')
        addresses={}
        for host,value in (resolved_addresses or {}).items():
            if host not in _ROUTES and host not in self.gateway_hosts:raise ProtocolError('configuration_required')
            if type(value) is not str or '%' in value:raise ProtocolError('configuration_required')
            try:addresses[host]=ipaddress.ip_address(value)
            except ValueError:raise ProtocolError('configuration_required') from None
        self._addresses=MappingProxyType(addresses)
        self._tls=ssl.create_default_context() if addresses else None
        if self._tls is not None and (not self._tls.check_hostname or self._tls.verify_mode!=ssl.CERT_REQUIRED):raise ProtocolError('configuration_required')

    def execute(self,request,*,deadline):
        allowed=_ROUTES.get(request.host,_GATEWAY_ROUTES if request.host in self.gateway_hosts else set())
        if request.path not in allowed:raise ProtocolError('configuration_required')
        timeout=remaining(deadline)
        if self.test_address:
            connection=http.client.HTTPConnection(*self.test_address,timeout=timeout)
        else:
            address=self._addresses.get(request.host)
            if address is None:raise ProtocolError('configuration_required')
            connection=_NumericTLSConnection(request.host,address,self._tls,deadline)
        response=None
        try:
            path=request.path+('?' + urlencode(request.query) if request.query else '')
            headers={'Accept':'application/json',**request.headers}
            connection.connect()
            connection.sock=_DeadlineSocket(connection.sock,deadline)
            connection.request(request.method,path,body=request.body,headers=headers)
            transport_socket=connection.sock
            if transport_socket:transport_socket.settimeout(remaining(deadline))
            response=connection.getresponse()
            if not 200<=response.status<300:raise ProtocolError()
            raw=bytearray()
            while not response.isclosed():
                # Exact-length read1 may close the response file (and its last
                # socket reference). Do not touch that socket again after EOF.
                # Actual reads remain bounded by _DeadlineReader.readinto.
                remaining(deadline)
                part=response.read1(min(65536,1048577-len(raw)))
                if not part:break
                raw.extend(part)
                if len(raw)>1048576:raise ProtocolError()
            if response.length not in (None,0):raise ProtocolError()
            remaining(deadline)
            return json_object(bytes(raw))
        except ProtocolError:raise
        except (TimeoutError,socket.timeout):raise ProtocolError('timeout') from None
        except (OSError,http.client.HTTPException,ValueError):raise ProtocolError() from None
        finally:
            try:
                if response is not None:response.close()
            finally:connection.close()


def envelope(value,key,success):
    if type(value) is not dict or type(value.get(key)) is not int or value[key]!=success:raise ProtocolError()
    return value


def safe_filename(value):
    value=text(value,limit=200)
    if any(c in value for c in '\\/"') or not re.fullmatch(r'[\w .-]+',value):raise ProtocolError()
    return value
