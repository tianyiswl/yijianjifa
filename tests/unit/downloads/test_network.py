import io
import pytest
from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.downloads.network import PublicConnector, SecureFetcher, SourceRegistry

class Cancel:
    def __init__(self, value=False): self.value = value
    def cancelled(self): return self.value

class Response(io.BytesIO):
    def __init__(self, body=b'abc', status=200, headers=None):
        super().__init__(body); self.status=status; self.headers=headers or {}
    def getheader(self, key, default=None): return self.headers.get(key, default)

class Transport:
    def __init__(self, responses): self.responses=iter(responses); self.urls=[]
    def open(self, url): self.urls.append(url); return next(self.responses)


def run(tmp_path, transport, **kwargs):
    registry=SourceRegistry(); source=registry.put('https://example.com/a.mp4', headers={})
    fetcher=SecureFetcher(registry, transport=transport, **kwargs)
    destination=tmp_path/'a.mp4'
    return fetcher, source, destination

@pytest.mark.parametrize('responses,code', [
    ([Response(status=302, headers={'Location':'http://127.0.0.1/a'})], 'unsafe_source'),
    ([Response(status=302, headers={'Location':'/next'})]*6, 'redirect_limit'),
    ([Response(b'abcdef')], 'size_limit'),
])
def test_redirects_and_stream_limit_cleanup(tmp_path, responses, code):
    transport=Transport(responses)
    fetcher,source,path=run(tmp_path,transport,max_bytes=5)
    with pytest.raises(DownloadError) as error: fetcher.download(source,path,cancel=Cancel(),progress=lambda p:None)
    assert error.value.code == code
    assert not path.exists() and list(tmp_path.iterdir()) == []
    assert all('127.0.0.1' not in url for url in transport.urls)


def test_success_disk_and_cancel(tmp_path):
    fetcher,source,path=run(tmp_path,Transport([Response()]))
    events=[]; fetcher.download(source,path,cancel=Cancel(),progress=events.append)
    assert path.read_bytes()==b'abc' and events[-1].current==3
    path.unlink()
    for cancelled,free,code in [(True,10**12,'cancelled'),(False,0,'disk_space')]:
        fetcher,source,path=run(tmp_path,Transport([Response()]), free_space=lambda p:free)
        with pytest.raises(DownloadError) as error: fetcher.download(source,path,cancel=Cancel(cancelled),progress=events.append)
        assert error.value.code==code and not path.exists()


def test_connector_pins_dns_and_rejects_all_unsafe_answers(monkeypatch):
    import socket
    calls=[]
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    class Sock:
        def settimeout(self,t): assert 0 < t <= 15
        def connect(self,address): calls.append(address)
        def close(self): pass
    monkeypatch.setattr(socket,'socket',lambda *a:Sock())
    PublicConnector().open_tcp('example.com',443)
    assert calls==[('8.8.8.8',443)]
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(DownloadError): PublicConnector().open_tcp('example.com',443)
    assert len(calls)==1

@pytest.mark.parametrize('phase', ['open', 'read', 'total'])
def test_timeouts_cleanup(tmp_path, phase):
    import socket
    class TimedTransport:
        def open(self,url):
            if phase=='open': raise socket.timeout()
            class TimedResponse(Response):
                def read(self,n):
                    if phase=='read': raise socket.timeout()
                    return super().read(n)
            return TimedResponse()
    times=iter([0,0,0,1800]) if phase=='total' else None
    kwargs={'clock':lambda:next(times)} if times else {}
    fetcher,source,path=run(tmp_path,TimedTransport(),**kwargs)
    with pytest.raises(DownloadError) as error:
        fetcher.download(source,path,cancel=Cancel(),progress=lambda p:None)
    assert error.value.code=='timeout'
    assert list(tmp_path.iterdir())==[]


def test_cancel_during_stream_cleanup(tmp_path):
    cancel=Cancel()
    class CancellingResponse(Response):
        def read(self,n): cancel.value=True; return b'abc'
    fetcher,source,path=run(tmp_path,Transport([CancellingResponse()]))
    with pytest.raises(DownloadError) as error:
        fetcher.download(source,path,cancel=cancel,progress=lambda p:None)
    assert error.value.code=='cancelled' and list(tmp_path.iterdir())==[]


def test_default_boundaries_and_registry_revoke():
    from yijianjifa.features.downloads.network import MAX_BYTES, SPACE_MARGIN, CONNECT_TIMEOUT, IDLE_TIMEOUT, TOTAL_TIMEOUT
    assert (MAX_BYTES,SPACE_MARGIN,CONNECT_TIMEOUT,IDLE_TIMEOUT,TOTAL_TIMEOUT)==(2*1024**3,256*1024**2,15,60,1800)
    registry=SourceRegistry(); source=registry.put('https://example.com/a.mp4',headers={})
    registry.revoke(source)
    with pytest.raises(DownloadError) as error: registry.lookup(source)
    assert error.value.code=='source_missing'


def test_transport_tls_uses_original_hostname_and_fixed_headers(monkeypatch):
    from yijianjifa.features.downloads.network import _Transport
    import ssl, http.client
    events=[]
    class Sock:
        def settimeout(self,t): events.append(('idle',t))
        def close(self): pass
        def do_handshake(self): pass
    class Connector:
        def open_tcp(self,host,port): events.append(('connect',host,port)); return Sock()
    class Context:
        def wrap_socket(self,sock,server_hostname,**kwargs): events.append(('tls',server_hostname)); return sock
    class Connection:
        def __init__(self,host): pass
        def request(self,method,path,headers): events.append(('request',method,path,headers))
        def getresponse(self): return Response()
    monkeypatch.setattr(ssl,'create_default_context',lambda:Context())
    monkeypatch.setattr(http.client,'HTTPConnection',Connection)
    _Transport(Connector()).open('https://example.com/a.mp4?sig=private')
    assert events[0]==('connect','example.com',443)
    assert 0 < events[1][1] <= 15
    assert events[2:4]==[('tls','example.com'),('idle',60)]
    assert events[4][3]['Accept-Encoding']=='identity'

@pytest.mark.parametrize('stop', ['deadline', 'cancel'])
def test_slow_drip_header_is_interrupted_and_socket_closed(tmp_path, monkeypatch, stop):
    import socket, threading, time, http.client
    import yijianjifa.features.downloads.network as network
    cancel=Cancel(); active=threading.Event(); closed=threading.Event()
    monkeypatch.setattr(network,'TOTAL_TIMEOUT',0.12 if stop=='deadline' else 2)
    class Sock:
        def settimeout(self,t): pass
        def shutdown(self,how): closed.set()
        def close(self): closed.set()
    class Connector:
        def open_tcp(self,host,port): return Sock()
    class SlowConnection:
        def __init__(self,host): pass
        def request(self,*a,**kw): pass
        def getresponse(self):
            active.set()
            # Simulate repeated header bytes arriving faster than the idle timeout.
            for _ in range(100):
                if closed.wait(.01): raise OSError('private header SECRET')
            return Response()
    monkeypatch.setattr(http.client,'HTTPConnection',SlowConnection)
    registry=SourceRegistry(); source=registry.put('http://example.com/a.mp4',headers={})
    fetcher=SecureFetcher(registry,connector=Connector()); destination=tmp_path/'a.mp4'
    if stop=='cancel':
        def cancelling(): active.wait(); cancel.value=True
        thread=threading.Thread(target=cancelling); thread.start()
    started=time.monotonic()
    with pytest.raises(DownloadError) as error:
        fetcher.download(source,destination,cancel=cancel,progress=lambda p:None)
    assert error.value.code==('timeout' if stop=='deadline' else 'cancelled')
    assert time.monotonic()-started < .5 and closed.is_set()
    assert list(tmp_path.iterdir())==[] and 'SECRET' not in str(error.value)


def test_trusted_header_validation_and_redaction():
    for headers in [{'X-Arbitrary':'SECRET'},{'Cookie':'SECRET\r\nInjected: yes'}, {'Authorization':'SECRET\x00'}, {'Cookie':'SECRET\x7f'}]:
        with pytest.raises(DownloadError) as error:
            SourceRegistry().put('https://example.com/a.mp4',headers=headers)
        assert 'SECRET' not in str(error.value)

@pytest.mark.parametrize('next_url,credentials', [('https://example.com/next.mp4',True),('https://other.example/next.mp4',False)])
def test_backend_headers_forwarded_and_cross_origin_credentials_removed(tmp_path, monkeypatch, next_url, credentials):
    import http.client, ssl
    captured=[]
    class Sock:
        def settimeout(self,t): pass
        def shutdown(self,how): pass
        def close(self): pass
        def do_handshake(self): pass
    class Connector:
        def open_tcp(self,*a): return Sock()
    class Context:
        def wrap_socket(self,sock,**kwargs): return sock
    class Connection:
        def __init__(self,host): pass
        def request(self,method,path,headers): captured.append(headers)
        def getresponse(self):
            return Response(status=302,headers={'Location':next_url}) if len(captured)==1 else Response()
    monkeypatch.setattr(ssl,'create_default_context',lambda:Context())
    monkeypatch.setattr(http.client,'HTTPConnection',Connection)
    registry=SourceRegistry(); source=registry.put('https://example.com/a.mp4',headers={'Cookie':'SECRET','Authorization':'Bearer SECRET','Referer':'https://example.com/','User-Agent':'Backend/1','Accept':'video/*'})
    SecureFetcher(registry,connector=Connector()).download(source,tmp_path/'a.mp4',cancel=Cancel(),progress=lambda p:None)
    assert captured[0]['Cookie']=='SECRET' and captured[0]['Authorization']=='Bearer SECRET'
    assert ('Cookie' in captured[1])==credentials and ('Authorization' in captured[1])==credentials
    assert captured[1]['Accept-Encoding']=='identity' and captured[1]['User-Agent']=='Backend/1'

@pytest.mark.parametrize('phase', ['dns', 'connect', 'tls'])
def test_cancel_interrupts_connection_phases(tmp_path, monkeypatch, phase):
    import socket, ssl, threading, time
    entered=threading.Event(); closed=threading.Event(); release=threading.Event()
    cancel=Cancel()
    def resolve(*args,**kwargs):
        if phase=='dns': entered.set(); release.wait(1)
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]
    class Sock:
        def settimeout(self,t): pass
        def connect(self,address):
            if phase=='connect': entered.set(); closed.wait(1); raise OSError('private SECRET')
        def do_handshake(self):
            if phase=='tls': entered.set(); closed.wait(1); raise OSError('private SECRET')
        def shutdown(self,how): closed.set()
        def close(self): closed.set()
    class Context:
        def wrap_socket(self,sock,**kwargs): return sock
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    monkeypatch.setattr(socket,'socket',lambda *a:Sock())
    monkeypatch.setattr(ssl,'create_default_context',lambda:Context())
    def cancelling(): entered.wait(1); cancel.value=True
    thread=threading.Thread(target=cancelling); thread.start()
    registry=SourceRegistry(); source=registry.put('https://example.com/a.mp4',headers={})
    started=time.monotonic()
    try:
        with pytest.raises(DownloadError) as error:
            SecureFetcher(registry).download(source,tmp_path/'a.mp4',cancel=cancel,progress=lambda p:None)
        import traceback
        assert error.value.code=='cancelled' and 'SECRET' not in str(error.value)
        assert 'private SECRET' not in ''.join(traceback.format_exception(error.value))
        assert time.monotonic()-started<.5 and list(tmp_path.iterdir())==[]
        if phase!='dns': assert closed.is_set()
    finally:
        release.set(); thread.join()

@pytest.mark.parametrize('redirect', [False, True])
def test_real_http_request_preparation_encodes_unicode_target(tmp_path, redirect):
    import io, traceback
    sockets=[]
    class OwnedSocket:
        def __init__(self, payload): self.payload=payload; self.closed=False; self.sent=b''
        def settimeout(self,value): pass
        def sendall(self,data): self.sent+=data
        def makefile(self,*args,**kwargs): return io.BytesIO(self.payload)
        def shutdown(self,how): self.closed=True
        def close(self): self.closed=True
    class Connector:
        def open_tcp(self,hostname,port):
            if redirect and not sockets:
                # Redirect can carry a Unicode target before HTTPConnection prepares it.
                payload=b'HTTP/1.1 302 Found\r\nLocation: /%E5%BD%B1%E7%89%87.mp4?token=SECRET&escaped=%2f\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            else:
                payload=b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc'
            sock=OwnedSocket(payload); sockets.append(sock); return sock
    registry=SourceRegistry()
    source=registry.put('http://example.com/'+('start.mp4' if redirect else '影片.mp4')+'?token=SECRET&escaped=%2f&label=影片',headers={})
    fetcher=SecureFetcher(registry,connector=Connector())
    if redirect:
        # HTTP headers cannot carry Unicode directly; emulate backend decoded Location
        # while retaining real request-line preparation on both requests.
        import http.client
        original=http.client.HTTPResponse.getheader
        from unittest.mock import patch
        def getheader(response,name,default=None):
            value=original(response,name,default)
            return '/影片.mp4?token=SECRET&escaped=%2f&label=影片' if name=='Location' and value else value
        context=patch.object(http.client.HTTPResponse,'getheader',getheader)
    else:
        from contextlib import nullcontext
        context=nullcontext()
    try:
        with context:
            fetcher.download(source,tmp_path/'a.mp4',cancel=Cancel(),progress=lambda p:None)
    except Exception as error:
        assert isinstance(error,DownloadError)
        assert 'SECRET' not in ''.join(traceback.format_exception(error))
        raise
    assert (tmp_path/'a.mp4').read_bytes()==b'abc'
    assert b'/%E5%BD%B1%E7%89%87.mp4?token=SECRET&escaped=%2f&label=%E5%BD%B1%E7%89%87' in sockets[-1].sent
    assert all(sock.closed for sock in sockets)
    assert sorted(p.name for p in tmp_path.iterdir())==['a.mp4']


def test_request_encoding_failure_is_private_safe_and_cleans_socket(tmp_path, monkeypatch):
    import http.client, traceback
    class OwnedSocket:
        closed=False
        def settimeout(self,value): pass
        def close(self): self.closed=True
        def shutdown(self,how): self.closed=True
    sock=OwnedSocket()
    class Connector:
        def open_tcp(self,*args): return sock
    def fail_encoding(connection,request):
        raise UnicodeEncodeError('ascii',request+'SECRET',0,1,'private SECRET')
    monkeypatch.setattr(http.client.HTTPConnection,'_encode_request',fail_encoding)
    registry=SourceRegistry(); source=registry.put('http://example.com/影片.mp4?token=SECRET',headers={})
    with pytest.raises(DownloadError) as error:
        SecureFetcher(registry,connector=Connector()).download(source,tmp_path/'a.mp4',cancel=Cancel(),progress=lambda p:None)
    assert error.value.code=='network'
    assert 'SECRET' not in ''.join(traceback.format_exception(error.value))
    assert sock.closed and list(tmp_path.iterdir())==[]
