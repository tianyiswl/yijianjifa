"""Fresh-interpreter fake Chromium, never imports or starts a real browser."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from yijianjifa.webui import session_check_process, playwright_runtime, chromium_publish

stage, filename = sys.argv[1:]
request = json.loads(Path(filename).read_text())
trace = Path(os.environ['P011_FAKE_CHECK_TRACE'])
account = request['account']
platform = account['platform']
uid = os.environ.get('P011_FAKE_UID', account['platform_user_id'])
origins = {'douyin': 'https://creator.douyin.com', 'kuaishou': 'https://cp.kuaishou.com',
           'channels': 'https://channels.weixin.qq.com', 'bilibili': 'https://member.bilibili.com'}
origin = origins[platform]

def event(name):
    with trace.open('a') as output:
        output.write(json.dumps({'event': name, 'pid': os.getpid()}) + '\n')

def block(name):
    event(name)
    if stage == name:
        while True: time.sleep(.05)

class Page:
    def __init__(self): self.closed = False
    def goto(self, url, **options): event('visit')
    def evaluate(self, expression, budget):
        block('evaluate')
        if 'p011ReadOnlySurface' in expression:
            return dict(origin=origin, login_required=False, challenge=False)
        if platform == 'douyin':
            return dict(uid=uid, uid_after=uid, nickname='Synthetic', status=0, profile_status=0, after_status=0)
        return dict(origin=origin, logged_in=True, uid=uid, profile_uid=uid, nickname='Synthetic', expired=False)
    def wait_for_timeout(self, duration): pass
    def is_closed(self): return self.closed

class Context:
    def __init__(self): self.pages = [Page()]
    def route(self, *args): pass
    def route_web_socket(self, *args): pass
    def set_default_timeout(self, *args): pass
    def cookies(self): return []
    def add_cookies(self, rows):
        assert rows
        event('restore.cookies')
    def add_init_script(self, script): event('restore.storage')
    def close(self): block('close'); self.pages[0].closed = True

class Driver:
    exclusive = True
    def __init__(self):
        self.chromium = SimpleNamespace(launch_persistent_context=self.launch)
        self.node = None
    def launch(self, profile, **options):
        assert options['headless'] is True and options['accept_downloads'] is False
        assert options['service_workers'] == 'block'
        assert (Path(profile) / 'Default' / 'Cookies').is_file()
        event('launch')
        if os.environ.get('P011_FAKE_DETACHED'):
            script = '''const cp=require('node:child_process');
const child=cp.spawn(process.argv[1],['-c',process.argv[2],process.argv[3]],
 {detached:true,stdio:['ignore','ignore','ignore','pipe','pipe']});
child.on('exit',()=>process.exit(0));'''
            child = '''import json,os,pathlib,signal,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
os.fstat(3);os.fstat(4)
with pathlib.Path(sys.argv[1]).open('a') as f:
 f.write(json.dumps({'event':'detached','pid':os.getpid(),'pgid':os.getpgrp()})+'\\n')
time.sleep(60)
'''
            self.node = subprocess.Popen(['node', '-e', script, sys.executable, child, str(trace)],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            until = time.monotonic() + 3
            while not any(json.loads(line)['event'] == 'detached' for line in trace.read_text().splitlines()):
                if time.monotonic() >= until: raise TimeoutError('fake browser gate did not execute')
                time.sleep(.01)
        return Context()
    def stop(self):
        block('stop')
        if self.node is not None:
            self.node.terminate(); self.node.wait(timeout=1)

playwright_runtime.acquire_playwright = Driver
chromium_publish.native_to_chromium = lambda blob, expected: [dict(name='synthetic', value='synthetic',
                                                               domain='creator.douyin.com', path='/', secure=True)]
raise SystemExit(session_check_process.worker_main([filename]))
