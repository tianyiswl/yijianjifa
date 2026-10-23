import json
import socket
import pytest
from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.downloads.network import CloudflareDoHResolver, PublicConnector

class Response:
    status=200
    def __init__(self,payload): self.body=json.dumps(payload).encode();self.closed=False
    def getheader(self,key): return 'application/dns-json' if key=='Content-Type' else str(len(self.body))
    def read(self,size): return self.body[:size]
    def close(self): self.closed=True
class Transport:
    def __init__(self,answers): self.answers=iter(answers);self.urls=[]
    def open(self,url,**kwargs): self.urls.append(url);return Response(next(self.answers))
def reply(t,value): return {'Status':0,'Answer':[{'type':t,'data':value}]}

def test_explicit_doh_returns_public_numeric_answers_only():
    transport=Transport([reply(1,'8.8.8.8'),reply(28,'2606:4700:4700::1111')])
    answers=CloudflareDoHResolver(transport=transport).resolve('www.douyin.com',443)
    assert len(answers)==2 and answers[0][4][0]=='8.8.8.8'
    assert all(url.startswith('https://cloudflare-dns.com/dns-query?name=www.douyin.com&type=') for url in transport.urls)

@pytest.mark.parametrize('payload',[reply(1,'127.0.0.1'),reply(1,'198.18.0.1'),{'Status':0,'Answer':[{'type':1,'data':'8.8.8.8'},{'type':1,'data':'192.168.1.1'}]},{'Status':0,'Answer':'bad'},{'Status':1}, {'Status':0,'Answer':[{'type':1,'data':'not-an-ip'}]}])
def test_doh_rejects_private_mixed_and_schema(payload):
    with pytest.raises(DownloadError): CloudflareDoHResolver(transport=Transport([payload,{'Status':0}])).resolve('www.douyin.com',443)

def test_doh_rejects_urls_and_internal_names_before_request():
    transport=Transport([])
    for name in ['https://www.douyin.com/?secret=x','localhost','thing.local','127.0.0.1']:
        with pytest.raises(DownloadError): CloudflareDoHResolver(transport=transport).resolve(name,443)
    assert transport.urls==[]

def test_doh_rejects_redirect_and_oversize():
    class CustomTransport:
        def __init__(self,response): self.response=response
        def open(self,*a,**kw): return self.response
    response=Response({}); response.status=302
    with pytest.raises(DownloadError): CloudflareDoHResolver(transport=CustomTransport(response)).resolve('www.douyin.com',443)
    assert response.closed
    response=Response({'oversize':'x'*65537})
    with pytest.raises(DownloadError) as e: CloudflareDoHResolver(transport=CustomTransport(response)).resolve('www.douyin.com',443)
    assert e.value.code=='size_limit' and response.closed

def test_doh_dns_budget_and_cancellation_close_blocked_socket(monkeypatch):
    import threading
    import yijianjifa.features.downloads.network as network
    monkeypatch.setattr(network,'CONNECT_TIMEOUT',.08)
    for cancelled in [False,True]:
        left,right=socket.socketpair()
        class Token:
            def cancelled(self): return cancelled
        class Parent:
            cancel=Token()
            error=None
        class BlockingTransport:
            def open(self,*args,guard,**kwargs):
                guard.attach(left)
                left.recv(1)
                guard.check()
        try:
            with pytest.raises(DownloadError) as e:
                CloudflareDoHResolver(transport=BlockingTransport()).resolve('www.douyin.com',443,guard=Parent())
            assert e.value.code==('cancelled' if cancelled else 'timeout')
        finally: left.close();right.close()

def test_bootstrap_uses_fixed_numeric_ip_without_resolver(monkeypatch):
    import yijianjifa.features.downloads.network as network
    seen=[]
    monkeypatch.setattr(PublicConnector,'open_tcp',lambda self,host,port:seen.append((host,port)))
    network._CloudflareBootstrap().open_tcp('cloudflare-dns.com',443)
    assert seen==[('1.1.1.1',443)]
    with pytest.raises(DownloadError): network._CloudflareBootstrap().open_tcp('other.invalid',443)
