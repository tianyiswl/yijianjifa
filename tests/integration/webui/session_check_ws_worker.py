"""The production child probe against loopback HTML, using actual Chromium."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from yijianjifa.webui import chromium_publish, session_check, session_check_process

request=json.loads(Path(sys.argv[1]).read_text())
home=os.environ['P011_OFFLINE_CHECK_HOME']
url=urlsplit(home)
origin=url.scheme+'://'+url.netloc
trace=Path(os.environ['P011_OFFLINE_CHECK_TRACE'])

def verified(value):
    if type(value) is not dict or value.get('ready') is not True or value.get('completed_sends')!=4:
        raise ValueError('offline document is not ready')
    with trace.open('a') as output:
        output.write(json.dumps({'document_started':value['document_started'],'completed_sends':value['completed_sends']})+'\n')
    return request['account']['platform_user_id'],'Synthetic offline document'

def provider(engine,paths):
    checker=session_check.make_session_checker(None,paths,platform='douyin',home=home,origins={origin},
        profile_for=lambda store,uid:Path(request['data_dir'])/'profile-source',restore=lambda *args:None,
        expression='() => ({ready:window.__offlineReady===true,completed_sends:window.__offlineSent,document_started:performance.timeOrigin})',verify=verified)
    return SimpleNamespace(check_session=checker)

options=chromium_publish.browser_options
def offline_options(mode):
    value=options(mode)
    value['args']=['--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1']
    return value
chromium_publish.browser_options=offline_options
chromium_publish.make_chromium_login=provider
raise SystemExit(session_check_process.worker_main(sys.argv[1:]))
