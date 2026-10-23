"""Synthetic credentials, local HTTP only: no platform or user profiles."""
import hashlib
import hmac
import importlib
import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


def module(name):
    path='yijianjifa.features.publish.platforms.'+name
    assert importlib.util.find_spec(path) is not None, 'official client module is not implemented'
    return importlib.import_module(path)


@pytest.fixture
def local_server():
    calls=[]; responses=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.respond()
        def do_POST(self):self.respond()
        def respond(self):
            body=self.rfile.read(int(self.headers.get('Content-Length','0')))
            calls.append((self.command,self.path,dict(self.headers),body))
            response=responses.pop(0) if responses else {'code':0}
            if callable(response):response=response(calls[-1])
            status,raw=response if isinstance(response,tuple) else (200,json.dumps(response).encode())
            self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=False
    thread=threading.Thread(target=server.serve_forever);thread.start()
    yield server.server_address,calls,responses
    server.shutdown();server.server_close();thread.join()


def executor(local_server,*,bili=False):
    http=module('official_http')
    address,_,_=local_server
    transport=http.HttpExecutor(test_address=address,gateway_hosts={'approved.example'})
    if not bili:return transport
    signer=module('bilibili.signing').BilibiliSigner('synthetic-key','SENTINEL-secret','SENTINEL-access',clock=lambda:1700000000,nonce=lambda:'fixed-nonce')
    class Signed:
        issuer=signer.issuer
        def execute(self,request,*,deadline):
            if request.host=='member.bilibili.com':request=signer.sign(request)
            return transport.execute(request,deadline=deadline)
    return Signed()


def test_signature_signs_exact_chinese_bytes_with_independent_vector():
    http=module('official_http');signing=module('bilibili.signing')
    body='{"title":"测试"}'.encode()
    request=http.Request('POST','member.bilibili.com','/arcopen/fn/archive/add-by-utoken',body=body)
    signed=signing.BilibiliSigner('key','secret','access',clock=lambda:1700000000,nonce=lambda:'nonce').sign(request)
    canonical=('x-bili-accesskeyid:key\nx-bili-content-md5:'+hashlib.md5(body).hexdigest()+'\nx-bili-signature-method:HMAC-SHA256\nx-bili-signature-nonce:nonce\nx-bili-signature-version:2.0\nx-bili-timestamp:1700000000')
    expected=hmac.new(b'secret',canonical.encode(),hashlib.sha256).hexdigest()
    assert signed.headers['Authorization']==expected
    assert signed.headers['Access-Token']=='access' and signed.body==body
    assert 'access' not in repr(signed) and 'secret' not in repr(signing.BilibiliSigner('key','secret','access'))
    assert signing.BilibiliSigner('key','secret','access',clock=lambda:1700000000,nonce=lambda:'nonce').sign(http.Request('GET','member.bilibili.com','/x')).headers['x-bili-content-md5']=='d41d8cd98f00b204e9800998ecf8427e'


@pytest.mark.parametrize('raw',[b'{"code":0,"code":1}',b'{"code":NaN}',b'[]',b'\xff',b'{'])
def test_strict_json_rejects_ambiguous_envelopes(raw):
    http=module('official_http')
    with pytest.raises(http.ProtocolError):http.json_object(raw)


@pytest.mark.parametrize('host',['evil.example','member.bilibili.com:443','user@member.bilibili.com','member.bilibili.com\r\nX:evil'])
def test_executor_rejects_unapproved_hosts_without_network(host):
    http=module('official_http')
    with pytest.raises(http.ProtocolError):http.HttpExecutor().execute(http.Request('GET',host,'/x'),deadline=time.monotonic()+1)


def test_bilibili_local_http_exact_upload_final_and_same_reference(local_server,tmp_path):
    client_module=module('bilibili.client');_,calls,responses=local_server
    client=client_module.BilibiliClient(executor(local_server,bili=True),chunk_size=3)
    video=tmp_path/'clip.mp4';video.write_bytes(b'abcdefg')
    responses.extend([{'code':0,'data':{'upload_token':'SENTINEL-upload'}},{'code':0},{'code':0},{'code':0},{'code':0}])
    upload=client.upload(video,deadline=time.monotonic()+5)
    assert [c[3] for c in calls[1:4]]==[b'abc',b'def',b'g']
    assert [parse_qs(urlsplit(c[1]).query)['part_number'] for c in calls[1:4]]==[['1'],['2'],['3']]
    assert all('Authorization' not in c[2] and 'Access-Token' not in c[2] for c in calls[1:4])
    assert calls[0][3]==b'{"name":"clip.mp4","utype":"0"}'
    assert calls[4][3]==b'' and 'fragment_count' not in calls[4][1]
    fields={'title':'标题','body':'正文','tags':'tag1,tag2','category_id':138,'copyright':'original','cover':None}
    responses.append({'code':0,'data':{'resource_id':'BV-Synthetic'}})
    receipt=client.submit(upload,fields,deadline=time.monotonic()+5)
    assert receipt=={'resource_id':'BV-Synthetic'}
    assert json.loads(calls[-1][3])=={'title':'标题','tid':138,'tag':'tag1,tag2','copyright':1,'desc':'正文'}
    responses.append({'code':0,'data':{'resource_id':'BV-Synthetic','addit_info':{'state':0}}})
    assert client.query(receipt,deadline=time.monotonic()+5)=='published'
    assert parse_qs(urlsplit(calls[-1][1]).query)=={'resource_id':['BV-Synthetic']}
    assert 'SENTINEL' not in repr(upload)


def test_bilibili_observed_scopes_categories_and_query_types(local_server):
    bili=module('bilibili.client');_,calls,responses=local_server;client=bili.BilibiliClient(executor(local_server,bili=True))
    responses.extend([{'code':0,'data':{'openid':'sub'}},{'code':0,'data':{'openid':'sub','scopes':['USER_INFO','ARC_BASE']}},{'code':0,'data':[{'id':160,'parent':0,'name':'生活','children':[{'id':138,'parent':160,'name':'搞笑'}]}]}])
    assert client.identity_scopes(deadline=time.monotonic()+5)==('sub',frozenset({'USER_INFO','ARC_BASE'}))
    assert client.categories(deadline=time.monotonic()+5)==[{'id':138,'label':'生活 / 搞笑'}]
    for state in (True,'0',1,None):
        responses.append({'code':0,'data':{'resource_id':'BV-Synthetic','addit_info':{'state':state}}})
        assert client.query({'resource_id':'BV-Synthetic'},deadline=time.monotonic()+5)=='result_unknown'
    responses.append({'code':0,'data':{'resource_id':'different','addit_info':{'state':0}}})
    with pytest.raises(module('official_http').ProtocolError):client.query({'resource_id':'BV-Synthetic'},deadline=time.monotonic()+5)


@pytest.mark.parametrize('code',[True,False,0.0,'0',1])
def test_bilibili_rejects_false_success_and_missing_receipt(local_server,code):
    bili=module('bilibili.client');http=module('official_http');_,_,responses=local_server
    responses.append({'code':code,'data':{'upload_token':'SENTINEL-upload'}})
    with pytest.raises(http.ProtocolError):bili.BilibiliClient(executor(local_server,bili=True)).categories(deadline=time.monotonic()+5)


def test_kuaishou_exact_top_level_upload_resume_multipart_and_processing(local_server,tmp_path):
    kwai=module('kuaishou.client');_,calls,responses=local_server
    client=kwai.KuaishouClient(executor(local_server),gateway_hosts={'approved.example'},chunk_size=3)
    video=tmp_path/'clip.mp4';video.write_bytes(b'abcdefg');cover=tmp_path/'cover.png';cover.write_bytes(b'cover-bytes')
    responses.extend([{'result':1,'upload_token':'SENTINEL-upload','endpoint':'approved.example'},
        {'result':1,'existed':False,'fragment_index':-1,'fragment_list':[],'fragment_index_bytes':0},
        {'result':1,'checksum':'a','size':3},{'result':1,'checksum':'b','size':3},{'result':1,'checksum':'c','size':1},{'result':1}])
    upload=client.upload(video,deadline=time.monotonic()+5)
    assert [c[3] for c in calls[2:5]]==[b'abc',b'def',b'g']
    assert [parse_qs(urlsplit(c[1]).query)['fragment_id'] for c in calls[2:5]]==[['0'],['1'],['2']]
    assert calls[-1][3]==b'' and parse_qs(urlsplit(calls[-1][1]).query)['fragment_count']==['3']
    responses.append({'result':1,'video_info':{'photo_id':'photo-synthetic','pending':True}})
    assert client.submit(upload,'标题\n正文\n#标签',cover,deadline=time.monotonic()+5)=={'photo_id':'photo-synthetic'}
    assert b'name="caption"' in calls[-1][3] and '标题\n正文\n#标签'.encode() in calls[-1][3]
    assert b'name="cover"; filename="cover.png"' in calls[-1][3]
    assert calls[-1][2]['Content-Type'].startswith('multipart/form-data; boundary=')
    for pending,want in [(True,'processing'),(False,'platform_accepted'),(1,'result_unknown'),('false','result_unknown'),(None,'result_unknown')]:
        responses.append({'result':1,'video_info':{'photo_id':'photo-synthetic','pending':pending}})
        assert client.query({'photo_id':'photo-synthetic'},deadline=time.monotonic()+5)==want
    assert 'SENTINEL' not in repr(upload)


@pytest.mark.parametrize('endpoint',['http://approved.example','uploader.test.gifshow.com','evil.example','approved.example/path'])
def test_kuaishou_rejects_insecure_or_unapproved_returned_gateway(local_server,tmp_path,endpoint):
    kwai=module('kuaishou.client');_,calls,responses=local_server;video=tmp_path/'clip.mp4';video.write_bytes(b'x')
    responses.append({'result':1,'upload_token':'SENTINEL-upload','endpoint':endpoint})
    with pytest.raises(module('official_http').ProtocolError):kwai.KuaishouClient(executor(local_server),gateway_hosts={'approved.example'}).upload(video,deadline=time.monotonic()+5)
    assert len(calls)==1


def test_kwai_business_auth_is_server_side_and_never_forwarded_to_gateway(local_server):
    kwai=module('kuaishou.client');http=module('official_http');_,calls,responses=local_server
    assert hasattr(kwai,'KuaishouAuthorizedExecutor'),'operator executor is missing'
    authorized=kwai.KuaishouAuthorizedExecutor(executor(local_server),'synthetic-app','SENTINEL-access')
    responses.extend([{'result':1},{'result':1}])
    authorized.execute(http.Request('POST','open.kuaishou.com','/openapi/photo/start_upload'),deadline=time.monotonic()+5)
    authorized.execute(http.Request('POST','approved.example','/api/upload/fragment',(('upload_token','SENTINEL-upload'),),b'part'),deadline=time.monotonic()+5)
    assert parse_qs(urlsplit(calls[0][1]).query)=={'app_id':['synthetic-app'],'access_token':['SENTINEL-access']}
    assert parse_qs(urlsplit(calls[1][1]).query)=={'upload_token':['SENTINEL-upload']}
    assert 'SENTINEL' not in repr(authorized)


@pytest.mark.parametrize('response',[(302,b'{}'),(403,b'SENTINEL-access'),(200,b'x'*1048577)])
def test_http_response_rejects_redirect_failure_and_oversize_without_secret(local_server,response):
    http=module('official_http');_,calls,responses=local_server;responses.append(response)
    with pytest.raises(http.ProtocolError) as caught:executor(local_server).execute(http.Request('GET','member.bilibili.com','/arcopen/fn/archive/view'),deadline=time.monotonic()+5)
    assert 'SENTINEL' not in str(caught.value) and len(calls)==1


@pytest.mark.parametrize('resume',[
    {'existed':True,'fragment_index':0,'fragment_list':[0,0],'fragment_index_bytes':3},
    {'existed':True,'fragment_index':0,'fragment_list':[True],'fragment_index_bytes':3},
    {'existed':True,'fragment_index':2,'fragment_list':[2],'fragment_index_bytes':3},
    {'existed':True,'fragment_index':0,'fragment_list':[0],'fragment_index_bytes':2},
    {'existed':False,'fragment_index':-1,'fragment_list':[0],'fragment_index_bytes':0}])
def test_kwai_resume_rejects_conflicting_remote_chunks_without_final(local_server,tmp_path,resume):
    kwai=module('kuaishou.client');_,calls,responses=local_server
    path=tmp_path/'clip.mp4';path.write_bytes(b'abc')
    responses.extend([{'result':1,'upload_token':'SENTINEL-upload','endpoint':'approved.example'},{'result':1,**resume}])
    with pytest.raises(module('official_http').ProtocolError):kwai.KuaishouClient(executor(local_server),gateway_hosts={'approved.example'},chunk_size=3).upload(path,deadline=time.monotonic()+5)
    assert len(calls)==2


@pytest.mark.parametrize('data',[
    [{'id':True,'parent':0,'name':'bad'}],
    [{'id':160,'parent':0,'name':'g','children':[{'id':138,'parent':99,'name':'bad'}]}],
    [{'id':160,'parent':0,'name':'g','children':[{'id':138,'parent':160,'name':'a'},{'id':138,'parent':160,'name':'b'}]}],
    {'categories':[]},
])
def test_bili_categories_reject_invalid_parent_bool_and_duplicate_ids(local_server,data):
    bili=module('bilibili.client');local_server[2].append({'code':0,'data':data})
    with pytest.raises(module('official_http').ProtocolError):bili.BilibiliClient(executor(local_server,bili=True)).categories(deadline=time.monotonic()+5)


def test_no_final_after_failed_part_or_complete(local_server,tmp_path):
    bili=module('bilibili.client');path=tmp_path/'clip.mp4';path.write_bytes(b'abc')
    for fail_at in ('part','complete'):
        local_server[2].append({'code':0,'data':{'upload_token':'SENTINEL-upload'}})
        if fail_at=='complete':local_server[2].append({'code':0})
        local_server[2].append({'code':1})
        with pytest.raises(module('official_http').ProtocolError):bili.BilibiliClient(executor(local_server,bili=True)).upload(path,deadline=time.monotonic()+5)
    assert all('/add-by-utoken' not in c[1] for c in local_server[1])


def test_production_without_bounded_address_configuration_never_calls_dns(monkeypatch):
    http=module('official_http');resolver_calls=[]
    def stalled(*a,**k):resolver_calls.append(True);time.sleep(.15);raise OSError('synthetic DNS')
    monkeypatch.setattr(http.socket,'getaddrinfo',stalled)
    started=time.monotonic()
    with pytest.raises(http.ProtocolError) as caught:
        http.HttpExecutor().execute(http.Request('GET','member.bilibili.com','/arcopen/fn/archive/view'),deadline=started+.020)
    assert str(caught.value)=='configuration_required'
    assert not resolver_calls and time.monotonic()-started<.070


def test_numeric_production_connection_uses_no_dns_and_keeps_hostname_tls(monkeypatch):
    http=module('official_http');assert 'resolved_addresses' in __import__('inspect').signature(http.HttpExecutor).parameters
    import io,ssl
    events=[];sent=[]
    class Socket:
        def settimeout(self,value):events.append(('timeout',value))
        def connect(self,address):events.append(('connect',address))
        def send(self,data):sent.append(bytes(data[:7]));return len(data[:7])
        def makefile(self,*a,**k):return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{"code":0}')
        def close(self):events.append(('closed',True))
    class Context:
        check_hostname=True;verify_mode=ssl.CERT_REQUIRED
        def wrap_socket(self,sock,*,server_hostname):events.append(('tls',server_hostname));return sock
    def no_dns(*a,**k):raise AssertionError('system resolver must not run')
    monkeypatch.setattr(http.socket,'getaddrinfo',no_dns)
    monkeypatch.setattr(http.socket,'socket',lambda *a,**k:Socket())
    monkeypatch.setattr(http.ssl,'create_default_context',lambda:Context())
    result=http.HttpExecutor(resolved_addresses={'member.bilibili.com':'192.0.2.1'}).execute(http.Request('GET','member.bilibili.com','/arcopen/fn/archive/view'),deadline=time.monotonic()+.020)
    assert result=={'code':0}
    assert ('connect',('192.0.2.1',443)) in events and ('tls','member.bilibili.com') in events
    assert all(0<t<=.020 for event,t in events if event=='timeout')
    assert b'Host: member.bilibili.com' in b''.join(sent) and events[-1]==('closed',True)


def test_numeric_connection_timeout_is_bounded_and_socket_closed(monkeypatch):
    http=module('official_http');assert 'resolved_addresses' in __import__('inspect').signature(http.HttpExecutor).parameters
    events=[]
    class Socket:
        def settimeout(self,value):events.append(('timeout',value))
        def connect(self,address):events.append(('connect',address));raise TimeoutError('synthetic connect')
        def close(self):events.append(('closed',True))
    monkeypatch.setattr(http.socket,'getaddrinfo',lambda *a,**k:pytest.fail('no DNS'))
    monkeypatch.setattr(http.socket,'socket',lambda *a,**k:Socket())
    with pytest.raises(http.ProtocolError) as caught:
        http.HttpExecutor(resolved_addresses={'member.bilibili.com':'192.0.2.1'}).execute(http.Request('GET','member.bilibili.com','/arcopen/fn/archive/view'),deadline=time.monotonic()+.020)
    assert str(caught.value)=='timeout' and events[-1]==('closed',True)
    assert 0<events[0][1]<=.020


@pytest.mark.parametrize('addresses',[{'member.bilibili.com':'unbounded-dns.example'}, {'evil.example':'192.0.2.1'}, {'member.bilibili.com':'192.0.2.1:443'}])
def test_resolved_configuration_is_numeric_host_only(addresses):
    http=module('official_http');assert 'resolved_addresses' in __import__('inspect').signature(http.HttpExecutor).parameters
    with pytest.raises(http.ProtocolError):http.HttpExecutor(resolved_addresses=addresses)
