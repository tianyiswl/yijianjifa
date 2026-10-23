"""Run the native surface script against the observed Bili body and widgets."""
import json
import subprocess

import pytest

from yijianjifa.webui.session_check import SURFACE


def challenge_surface(kind):
    program = r'''const fs=require('node:fs'),input=JSON.parse(fs.readFileSync(0,'utf8'));
    function element(tag,attributes={}) {
      return {tagName:tag.toUpperCase(),id:'',className:'',src:'',title:'',placeholder:'',
        type:'',isConnected:true,parentElement:null,children:[],textContent:'',...attributes,
        getClientRects(){return [{width:this.width??200,height:this.height??100}];},
        getAttribute(name){return this[name]??null;},querySelectorAll(){return [];}};
    }
    const html=element('html'),body=element('body');body.parentElement=html;
    const nodes=[html,body];
    function append(node,parent=body){node.parentElement=parent;parent.children.push(node);nodes.push(node);return node;}
    const kind=input.kind;
    if(kind==='body-adapt'||kind==='body-with-widget')body.className='risk-captcha-adapt-pc risk-captcha-adapt';
    if(kind==='html-adapt')html.className='risk-captcha-adapt';
    if(kind==='widget'||kind==='body-with-widget')append(element('div',{className:'geetest_panel',textContent:'请完成验证'}));
    if(kind==='iframe')append(element('iframe',{src:'https://captcha.example.test/verify'}));
    if(kind==='sms')append(element('input',{placeholder:'请输入验证码'}));
    if(kind==='hidden-widget')append(element('div',{className:'captcha-panel',hidden:true}));
    if(kind==='hidden-ancestor'){
      const parent=append(element('div',{hidden:true}));
      append(element('iframe',{src:'https://captcha.example.test/verify'}),parent);
    }
    if(kind==='transparent-widget')append(element('div',{className:'captcha-panel',opacity:'0'}));
    if(kind==='small-widget')append(element('div',{className:'captcha-panel',width:39,height:19}));
    if(kind==='ordinary-panel')append(element('div',{className:'profile-panel',textContent:'身份验证资料已完善'}));
    if(kind==='ordinary-iframe')append(element('iframe',{src:'https://help.example.test/article'}));
    global.location={origin:'https://member.bilibili.com',pathname:'/platform/home'};
    global.getComputedStyle=node=>({display:node.hidden?'none':'block',visibility:'visible',opacity:node.opacity??'1'});
    global.document={body,documentElement:html,querySelectorAll(selector){
      if(selector==='body *')return nodes.filter(node=>node!==html&&node!==body);
      if(selector==='input')return nodes.filter(node=>node.tagName==='INPUT');
      if(selector.includes('captcha'))return nodes.filter(node=>node.tagName==='IFRAME'||node.className.includes('captcha')||node.id.includes('captcha')||node.className.includes('geetest'));
      if(selector.includes('password'))return nodes.filter(node=>node.tagName==='INPUT'&&node.type==='password');
      return [];
    }};
    process.stdout.write(JSON.stringify(eval('('+input.script+')')()));'''
    completed = subprocess.run(['node', '-e', program], input=json.dumps({'kind': kind, 'script': SURFACE}),
                               text=True, capture_output=True, timeout=5, check=True)
    return json.loads(completed.stdout)


@pytest.mark.parametrize('kind', ['body-adapt', 'html-adapt'])
def test_global_captcha_adaptation_class_does_not_require_platform_verification(kind):
    value = challenge_surface(kind)
    assert value == {'origin': 'https://member.bilibili.com', 'login_required': False, 'challenge': False}


@pytest.mark.parametrize('kind', ['widget', 'body-with-widget', 'iframe', 'sms'])
def test_real_verification_widget_still_blocks_even_with_a_body_adaptation_class(kind):
    assert challenge_surface(kind)['challenge'] is True


@pytest.mark.parametrize('kind', ['hidden-widget', 'hidden-ancestor', 'transparent-widget',
                                 'small-widget', 'ordinary-panel', 'ordinary-iframe'])
def test_hidden_or_non_challenge_surface_does_not_require_verification(kind):
    assert challenge_surface(kind)['challenge'] is False
