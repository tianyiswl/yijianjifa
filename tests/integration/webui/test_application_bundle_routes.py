import json
import shutil
import subprocess

import pytest

from yijianjifa.webui.app_bundle import application_script


def test_packaged_application_registers_separate_publish_modules():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node is required for bundled JavaScript registration')
    script=application_script()
    # Suppress automatic startup only; evaluate the actual packaged definitions.
    script=script.rsplit('\n',1)[0]
    probe="const vm=require('node:vm'),fs=require('node:fs');const c={};vm.createContext(c);vm.runInContext(fs.readFileSync(0,'utf8'),c);process.stdout.write(vm.runInContext('JSON.stringify({templates:Object.keys(P011Templates),views:Object.keys(P011Views),distinct:P011Views.publish!==P011Views.multi_publish})',c));"
    result=subprocess.run([node,'-e',probe],input=script,text=True, encoding="utf-8",capture_output=True,check=True)
    routes=json.loads(result.stdout)
    for kind in ('templates','views'):
        assert 'publish' in routes[kind] and 'multi_publish' in routes[kind]
    assert routes['distinct'] is True


def test_daily_workspace_opens_first_and_route_title_follows_visible_page():
    """Wrong launch route or a stale page title must be observable to users."""
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for bundled JavaScript startup')
    definitions, startup = application_script().rsplit('\n', 1)
    probe = r"""
const vm=require('node:vm'),fs=require('node:fs'),data=JSON.parse(fs.readFileSync(0,'utf8'));
let recovery=0;const c={crypto:require('node:crypto').webcrypto,window:{webkit:{messageHandlers:{p011App:{postMessage:async raw=>{if(JSON.parse(raw).operation==='task.recovery')recovery++;return JSON.stringify({ok:true,data:{tasks:[]}})}}}}},document:{title:'',body:{dataset:{}},head:{querySelectorAll:()=>[],append(){}},querySelectorAll:()=>[],createElement:()=>({})}};
vm.createContext(c);vm.runInContext(data.definitions,c);
vm.runInContext(`const mounts=[];
for(const name of Object.keys(P011Views))P011Views[name]=()=>{mounts.push(name);return ()=>{}};
P011ApplyFeatures=()=>{};
P011UseTerm=P011Notifications=P011PublishIntervention=P011PublishProgress=P011DatePicker=()=>()=>{};`,c);
vm.runInContext(data.startup,c);
const first={...vm.runInContext('({view:P011Router.current,title:document.title})',c),recovery};
vm.runInContext("P011Router.go('multi_publish')",c);
process.stdout.write(JSON.stringify({first,next:vm.runInContext('({view:P011Router.current,title:document.title})',c)}));
"""
    result = subprocess.run([node, '-e', probe], input=json.dumps(dict(definitions=definitions, startup=startup)),
                            text=True, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['first'] == {'view': 'mixing', 'title': '一剪即发 · 混剪制作', 'recovery': 1}
    assert observed['next'] == {'view': 'multi_publish', 'title': '一剪即发 · 多平台分发'}
