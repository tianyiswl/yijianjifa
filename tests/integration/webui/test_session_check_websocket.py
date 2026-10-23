"""Actual production WS routing, real offline Chromium, and owned deadlines."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socketserver
import sys
import threading
import time

import pytest


def chromium_available():
    if importlib.util.find_spec('playwright') is None:return False
    if os.environ.get('PLAYWRIGHT_BROWSERS_PATH'):
        cache=Path(os.environ['PLAYWRIGHT_BROWSERS_PATH'])
    elif sys.platform=='darwin':cache=Path.home()/'Library/Caches/ms-playwright'
    elif os.name=='nt':cache=Path(os.environ.get('LOCALAPPDATA',''))/'ms-playwright'
    else:cache=Path.home()/'.cache/ms-playwright'
    return any(cache.glob('chromium-*'))


@pytest.mark.skipif(not chromium_available(),reason='Optional real Chromium is not installed')
def test_production_probe_blocks_all_websocket_network_without_blocking_future_evaluations(tmp_path,monkeypatch):
    from yijianjifa.features.accounts.session_store import SessionStore
    from yijianjifa.webui import session_check_process as owned
    from yijianjifa.webui.session_check import CHECK_SECONDS
    observations={'documents':0,'ws_connections':0,'ws_bytes':0}
    class WebSocketListener(socketserver.BaseRequestHandler):
        def handle(self):
            observations['ws_connections']+=1
            self.request.settimeout(.2)
            try:observations['ws_bytes']+=len(self.request.recv(8192))
            except TimeoutError:pass
    ws=socketserver.ThreadingTCPServer(('127.0.0.1',0),WebSocketListener)
    ws.daemon_threads=True
    class Document(BaseHTTPRequestHandler):
        def do_GET(self):
            observations['documents']+=1
            content=('''<!doctype html><title>Synthetic offline account check</title>
<script>window.__offlineReady=false;window.__offlineSent=0;window.__sockets=[];
for(let i=0;i<4;i++){
 const socket=new WebSocket('ws://127.0.0.1:%d/readonly-'+i);
 socket.onopen=()=>{
  socket.send('synthetic-write-attempt');
  window.__offlineSent++;
  if(window.__offlineSent===4)window.__offlineReady=true;
 };
 window.__sockets.push(socket);
}</script><p>Loopback-only websocket regression fixture</p>''' % ws.server_address[1]).encode()
            self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(content)))
            self.end_headers();self.wfile.write(content)
        def log_message(self,*args):pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Document)
    threads=[threading.Thread(target=server.serve_forever,daemon=True) for server in (http,ws)]
    for thread in threads:thread.start()
    data=tmp_path/'data';data.mkdir();(data/'profile-source').mkdir()
    store=SessionStore(data/'profiles')
    ref=store.put('offline-account',b'Synthetic archive; no real account or cookies')
    directory=data/'profiles'/'.offline-check';directory.mkdir(mode=0o700)
    trace=tmp_path/'documents.jsonl'
    monkeypatch.setenv('P011_OFFLINE_CHECK_HOME','http://127.0.0.1:'+str(http.server_address[1])+'/home')
    monkeypatch.setenv('P011_OFFLINE_CHECK_TRACE',str(trace))
    original=owned._command
    worker=Path(__file__).with_name('session_check_ws_worker.py')
    monkeypatch.setattr(owned,'_command',lambda kind='worker':[sys.executable,str(worker)] if kind=='worker' else original(kind))
    # This checks real process/browser routing, not a shortened deadline. Match
    # production so cold interpreter/browser startup does not consume the whole
    # budget before either document is read; hard-deadline tests are separate.
    request=dict(version=1,platform='douyin',data_dir=str(data),directory=str(directory),deadline=time.monotonic()+CHECK_SECONDS,
                 account=dict(id='offline-account',platform='douyin',platform_user_id='synthetic-uid',session_ref=ref))
    try:
        result,cleaned=owned.run_check(request,threading.Event())
        assert cleaned,'All real Chromium/SDK children must be confirmed terminated'
        assert {k:v for k,v in result.items() if k!='diagnostic'}=={'state':'valid','platform_user_id':'synthetic-uid'}, result
        from yijianjifa.webui.session_check_diagnostics import sanitize_diagnostic
        assert sanitize_diagnostic(result['diagnostic']) is not None
        readings=[json.loads(line) for line in trace.read_text().splitlines()]
        documents=[reading['document_started'] for reading in readings]
        assert len(set(documents))==2,'The probe must reach identity reads in two separate live documents'
        assert all(reading['completed_sends']==4 for reading in readings),'Each document must complete four onopen/send callbacks'
        assert observations['documents']>=2
        assert observations['ws_connections']==0 and observations['ws_bytes']==0
    finally:
        for server in (http,ws):server.shutdown();server.server_close()
        for thread in threads:thread.join(2)
