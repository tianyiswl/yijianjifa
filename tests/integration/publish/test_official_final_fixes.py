"""Synchronous deadline and strict format regressions; loopback only."""
import socket,threading,time,struct,zlib
import pytest
from yijianjifa.features.publish.platforms.official_http import HttpExecutor,Request,ProtocolError,json_object
from yijianjifa.features.publish.platforms.official_authorization import _png

@pytest.mark.parametrize('raw',[b'{"n":1e999}',b'{"n":-1e999}',b'{"a":[{"n":1e999}]}'])
def test_json_overflow_is_rejected(raw):
    with pytest.raises(ProtocolError):json_object(raw)

@pytest.mark.parametrize('scan_filter',range(6))
def test_every_png_scanline_has_valid_filter(tmp_path,scan_filter):
    bad=255 if scan_filter==5 else scan_filter
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    raw=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',1,2,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00\x01\x02\x03'+bytes([bad])+b'\x01\x02\x03'))+chunk(b'IEND',b'')
    path=tmp_path/'owned.png';path.write_bytes(raw)
    if bad==255:
        with pytest.raises(ProtocolError):_png(path)
    else:_png(path)

@pytest.mark.parametrize('phase',['status','headers','chunked'])
def test_dripping_response_cannot_extend_absolute_deadline(phase):
    server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1)
    address=server.getsockname()
    def serve():
        c,_=server.accept()
        try:
            c.recv(4096)
            if phase=='status':parts=[bytes([b]) for b in b'HTTP/1.1 200 OK\r\n']
            elif phase=='headers':
                c.sendall(b'HTTP/1.1 200 OK\r\n');parts=[b'X-Test: a\r\n']*16
            else:
                c.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n');parts=[b'1;'+b'x'*i for i in range(16)]
            for part in parts:time.sleep(.02);c.sendall(part)
            c.sendall(b'Content-Length: 2\r\n\r\n{}')
        except (BrokenPipeError,ConnectionResetError):pass
        finally:c.close();server.close()
    thread=threading.Thread(target=serve);thread.start();start=time.monotonic()
    try:
        with pytest.raises(ProtocolError,match='timeout'):
            HttpExecutor(test_address=address).execute(Request('GET','member.bilibili.com','/arcopen/fn/archive/type/list'),deadline=start+.08)
        assert time.monotonic()-start<.18
    finally:thread.join(1)
    assert not thread.is_alive()


def test_expired_chunk_header_read_is_timeout_not_malformed_response():
    """http.client catches ValueError while parsing chunk sizes; expiry is IO."""
    import http.client
    from yijianjifa.features.publish.platforms.official_http import _DeadlineSocket
    client, server = socket.socketpair()
    response = None
    try:
        server.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n')
        response = http.client.HTTPResponse(_DeadlineSocket(client, time.monotonic()+5))
        response.begin()
        response.fp.raw.deadline = time.monotonic()-1
        with pytest.raises(TimeoutError):
            response.read1(1)
    finally:
        if response is not None: response.close()
        client.close(); server.close()
