"""Exact framing success and truncation controls, real loopback only."""
import json,socket,threading,time
import pytest
from yijianjifa.features.publish.platforms.official_http import HttpExecutor,Request,ProtocolError

@pytest.mark.parametrize('framing',['length-close','length-keepalive','chunked-close','truncated-valid-json','invalid-json'])
def test_complete_response_and_truncation_contract(framing):
    server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1)
    address=server.getsockname();stop=threading.Event()
    expected={'code':0,'data':'文'*40000};raw=json.dumps(expected,ensure_ascii=False).encode()
    if framing=='truncated-valid-json':raw=b'{"code":0}'
    if framing=='invalid-json':raw=b'{"code":'
    def serve():
        c,_=server.accept()
        try:
            c.recv(4096)
            if framing=='chunked-close':
                c.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
                for i in range(0,len(raw),4096):
                    part=raw[i:i+4096];c.sendall(f'{len(part):x}\r\n'.encode()+part+b'\r\n')
                c.sendall(b'0\r\n\r\n')
            else:
                size=len(raw)+(10 if framing=='truncated-valid-json' else 0)
                connection='keep-alive' if framing=='length-keepalive' else 'close'
                c.sendall(f'HTTP/1.1 200 OK\r\nContent-Length: {size}\r\nConnection: {connection}\r\n\r\n'.encode()+raw)
                if framing=='length-keepalive':stop.wait(1)
        except (BrokenPipeError,ConnectionResetError):pass
        finally:c.close();server.close()
    thread=threading.Thread(target=serve);thread.start()
    try:
        executor=HttpExecutor(test_address=address);request=Request('GET','member.bilibili.com','/arcopen/fn/archive/type/list')
        if framing in {'truncated-valid-json','invalid-json'}:
            with pytest.raises(ProtocolError):executor.execute(request,deadline=time.monotonic()+.5)
        else:assert executor.execute(request,deadline=time.monotonic()+.5)==expected
    finally:stop.set();thread.join(1)
    assert not thread.is_alive()
