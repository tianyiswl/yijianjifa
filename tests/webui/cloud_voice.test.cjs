const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
function setup(calls,initial={},selectedVoice=''){
 let state={enabled:true,authenticated:true,mode:'hosted',account_id:'account',account_label:'测试用户',available_words:12345,reserved_words:0,pack:{price_fen:1990,words:10000},...initial};
 let catalog={items:[{id:'doubao:test',name:'测试声音',gender:'女声',style:'通用',language:'中文',standard_sample:true}],selected:selectedVoice,favorites:[],configured:true,mode:state.mode};
 if(selectedVoice==='doubao:old')catalog.items.push({id:'doubao:old',name:'旧声音',gender:'男声',style:'通用',language:'中文',standard_sample:true});
 return async(op,p)=>{calls.push([op,p]);if(op==='voice.cloud.read')return state;if(op==='voice.catalog')return catalog;if(op==='voice.settings.read')return {configured:false};
  if(op==='voice.cloud.redeem'){assert.equal(p.code,'TEST-CODE');state={...state,available_words:22345};return {credited_words:10000,available_words:22345};}
  if(op==='voice.cloud.login'){state={...state,authenticated:true};return state;}if(op==='voice.cloud.mode.save'){state={...state,mode:p.mode};catalog={...catalog,mode:p.mode};return state;}
  if(op==='voice.selection.save'){catalog={...catalog,...p};return catalog;}
  if(op==='voice.standard')return {url:'p011media://asset/sample'};
  throw Error(op);
 };
}
test('free system voice is usable without cloud registration and its optional charge is explained',async()=>{
 const calls=[],v=await mount(setup(calls,{authenticated:false}),'voices');await v.flush();
 const notice=v.getId('freeUsageNotice');
 assert.match(notice.textContent,/软件.*系统配音.*免费/);
 assert.match(notice.textContent,/云配音.*可选.*收费/);
 assert.match(v.getId('selectedName').textContent,/系统配音/);
 assert.ok(!calls.some(([op])=>op==='voice.cloud.register'||op==='voice.cloud.login'||op==='voice.audition.text'));
 await v.getId('cloudAccount').onclick();await v.flush();
 assert.match(contents(v.getId('dialog')),/仅.*云配音.*注册/);v.dispose();
});
test('cloud quota header exposes words and the 19.9 yuan pack',async()=>{
 const calls=[],v=await mount(setup(calls),'voices');await v.flush();
 assert.match(v.getId('cloudStatus').textContent,/12,345\s*字/);
 await v.getId('cloudRedeem').onclick();await v.flush();
 assert.ok(v.find(n=>n.textContent.includes('19.9 元／1 万字')));
 assert.ok(!v.getId('dialog').children.some(n=>n.textContent.includes('字符')));v.dispose();
});
test('redeeming refreshes balance and does not create speech',async()=>{
 const calls=[],v=await mount(setup(calls),'voices');await v.flush();
 await v.getId('cloudRedeem').onclick();await v.flush();
 v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='兑换码').value='TEST-CODE';await v.find(n=>n.tag==='button'&&n.textContent==='兑换').onclick({stopPropagation(){}});await v.flush();
 assert.match(v.getId('cloudStatus').textContent,/22,345\s*字/);assert.ok(!calls.some(([op])=>op==='voice.audition.text'));v.dispose();
});
test('login clears submitted password and remembers only public state in the view',async()=>{
 const calls=[],v=await mount(setup(calls,{authenticated:false}),'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await v.flush();
 v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音账号').value='tester';
 const password=v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音密码');password.value='a strong passphrase';await v.find(n=>n.tag==='button'&&n.textContent==='登录').onclick({stopPropagation(){}});await v.flush();
 assert.equal(password.value,'');assert.ok(calls.some(([op,p])=>op==='voice.cloud.login'&&p.username==='tester'));assert.match(v.getId('cloudAccount').textContent,/测试用户/);v.dispose();
});
test('text counter counts supplementary unicode as one word and fixed sample stays free',async()=>{
 const calls=[],v=await mount(setup(calls),'voices');v.getId('auditionText').value='𠀀A。';v.getId('auditionText').oninput();assert.equal(v.getId('textCount').textContent,'3 / 4000 字');
 await v.getId('standardAudition').onclick();await v.flush();assert.ok(!calls.some(([op])=>op==='voice.audition.text'));v.dispose();
});
test('audition word estimate keeps spaces inside the submitted text',()=>{
 const {words}=require('../../src/yijianjifa/webui/assets/cloud_voice.js');
 assert.equal(words('  你好，世界。\n 今天  上新！  '),19);
 assert.equal(words('新品A12，售价99元。\n详情\n请看这里。'),21);
 assert.equal(words('𠀀🙂A。'),4);
});
const contents=node=>[node.textContent,...node.children.map(contents)].join(' ');
const click=async(v,text)=>{await v.find(n=>n.tag==='button'&&n.textContent===text).onclick({stopPropagation(){}});await v.flush();};
test('signed-in account keeps everyday actions separate from collapsed account settings',async()=>{
 const calls=[],v=await mount(setup(calls,{balance_current:true}),'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await v.flush();
 const dialog=v.getId('dialog'),foot=dialog.querySelector('.foot'),settings=dialog.querySelector('details');
 assert.deepEqual(foot.children.map(n=>n.textContent),['退出登录','完成']);
 assert.equal(settings.open,false);assert.equal(settings.querySelector('summary').textContent,'账号设置');
 assert.match(contents(dialog),/测试用户/);assert.match(contents(dialog),/剩余 12,345 字/);
 assert.match(contents(dialog),/换电脑时，用这个账号登录/);
 assert.ok(!dialog.querySelectorAll('button').some(n=>n.textContent==='找回账号'));
 assert.ok(!calls.some(([op])=>['voice.cloud.logout','voice.cloud.logout_all','voice.cloud.recovery.export','voice.cloud.password.change'].includes(op)));
 await click(v,'完成');assert.equal(v.getId('modal').style.display,'none');v.dispose();
});
test('account does not present an old balance as current when refresh failed',async()=>{
 const v=await mount(setup([],{balance_current:false}),'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await v.flush();
 assert.match(contents(v.getId('dialog')),/字数暂未更新/);
 assert.doesNotMatch(contents(v.getId('dialog')),/剩余 12,345/);v.dispose();
});
test('account settings omit recovery files and retain password changes',async()=>{
 const v=await mount(setup([]),'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await v.flush();
 assert.doesNotMatch(contents(v.getId('dialog')),/找回文件|保存找回|恢复文件/);
 assert.match(contents(v.getId('dialog')),/修改密码/);v.dispose();
});
test('logout all devices requires the explained password confirmation before its existing command',async()=>{
 const calls=[],base=setup(calls),v=await mount(async(op,p)=>{
  if(op==='voice.cloud.logout_all'){calls.push([op,p]);return {};}
  return base(op,p);
 },'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await v.flush();
 v.getId('dialog').querySelector('details').open=true;await click(v,'退出所有设备');
 assert.match(contents(v.getId('dialog')),/包括当前电脑/);assert.match(contents(v.getId('dialog')),/字数.*保留/);
 assert.ok(!calls.some(([op])=>op==='voice.cloud.logout_all'));
 const password=v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音密码');password.value='test-password';
 await click(v,'确认退出所有设备');assert.equal(password.value,'');
 assert.equal(calls.filter(([op])=>op==='voice.cloud.logout_all').length,1);
 assert.equal(calls.find(([op])=>op==='voice.cloud.logout_all')[1].password,'test-password');v.dispose();
});
test('ordinary logout keeps its device-only command and never invokes bulk logout',async()=>{
 const calls=[],base=setup(calls),v=await mount(async(op,p)=>{
  if(op==='voice.cloud.logout'){calls.push([op,p]);return {};}
  return base(op,p);
 },'voices');await v.flush();await v.getId('cloudAccount').onclick();await v.flush();
 await click(v,'退出登录');assert.equal(calls.filter(([op])=>op==='voice.cloud.logout').length,1);
 assert.ok(!calls.some(([op])=>op==='voice.cloud.logout_all'||op==='voice.cloud.redeem'||op==='voice.audition.text'));v.dispose();
});
test('direct cloud selection blocks unavailable service without changing the saved source',async()=>{
 const calls=[],v=await mount(setup(calls,{mode:'own',enabled:false}),'voices');await v.flush();
 assert.equal(v.getId('useVoice').disabled,true);
 await v.getId('useVoice').onclick();await v.flush();
 assert.match(v.getId('toast').textContent,/暂未开放/);
 assert.ok(!calls.some(([op])=>op==='voice.cloud.mode.save'));
 assert.ok(!calls.some(([op])=>op==='voice.selection.save'||op==='voice.audition.text'));v.dispose();
});
test('direct system voice uses the original selection path without changing provider or generating speech',async()=>{
 const calls=[],v=await mount(setup(calls,{},'doubao:test'),'voices');await v.flush();
 assert.equal(v.getId('systemVoice').hidden,false);
 await v.getId('systemVoice').onclick();await v.flush();
 assert.equal(v.getId('systemVoice').hidden,true);
 assert.equal(calls.find(([op])=>op==='voice.selection.save')[1].selected,'');
 assert.ok(!calls.some(([op])=>op==='voice.cloud.mode.save'||op==='voice.audition.text'));
 assert.equal(v.getId('selectedName').textContent,'系统配音');v.dispose();
});
test('direct cloud selection takes unsigned legacy users to login without generating speech',async()=>{
 const calls=[],v=await mount(setup(calls,{mode:'own',authenticated:false},'doubao:test'),'voices');await v.flush();
 assert.equal(v.getId('useVoice').disabled,false);
 await v.getId('useVoice').onclick();await v.flush();
 assert.ok(!calls.some(([op])=>op==='voice.cloud.mode.save'));
 assert.ok(v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音账号'));
 assert.ok(!calls.some(([op])=>op==='voice.selection.save'||op==='voice.audition.text'));v.dispose();
});
test('registration warns that password recovery is unavailable',async()=>{
 const v=await mount(setup([],{authenticated:false}),'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await v.flush();
 assert.doesNotMatch(contents(v.getId('dialog')),/找回账号|恢复文件/);
 await click(v,'还没有账号？注册');
 assert.match(contents(v.getId('dialog')),/暂不提供密码找回/);
 assert.match(contents(v.getId('dialog')),/记住账号和密码/);v.dispose();
});
test('direct cloud selection explicitly replaces legacy preferences without provider credentials or generation',async()=>{
 const calls=[],v=await mount(setup(calls,{mode:'own'},'doubao:test'),'voices');await v.flush();
 assert.equal(v.getId('useVoice').disabled,false);
 await v.getId('useVoice').onclick();await v.flush();
 assert.equal(calls.filter(([op,p])=>op==='voice.cloud.mode.save'&&p.mode==='hosted').length,1);
 assert.equal(calls.filter(([op])=>op==='voice.selection.save').at(-1)[1].selected,'doubao:test');
 assert.ok(!calls.some(([op])=>op.startsWith('voice.settings.')));
 assert.ok(!calls.some(([op])=>op==='voice.audition.text'));v.dispose();
});
test('failed legacy cloud selection leaves the free system default rather than charging the old voice',async()=>{
 const calls=[],base=setup(calls,{mode:'own'},'doubao:old');
 const v=await mount(async(op,p)=>{
  if(op==='voice.selection.save'&&p.selected==='doubao:test'){calls.push([op,p]);throw Error('此音色暂不可用');}
  return base(op,p);
 },'voices');await v.flush();v.getId('voiceRows').children[0].onclick();
 await v.getId('useVoice').onclick();await v.flush();
 assert.equal(v.getId('selectedName').textContent,'系统配音');
 assert.match(v.getId('toast').textContent,/此音色暂不可用/);
 const writes=calls.filter(([op])=>op==='voice.selection.save'||op==='voice.cloud.mode.save');
 assert.equal(writes[0][0],'voice.selection.save');assert.equal(writes[0][1].selected,'');
 assert.ok(!calls.some(([op])=>op==='voice.audition.text'));v.dispose();
});
test('free system selection waits for the in-flight cloud default update to finish',async()=>{
 const calls=[],base=setup(calls,{mode:'own'},'doubao:test');let release;
 const waiting=new Promise(resolve=>{release=resolve;});
 const v=await mount(async(op,p)=>{if(op==='voice.cloud.mode.save')await waiting;return base(op,p);},'voices');await v.flush();
 v.getId('auditionText').value='不要在切换设置时生成';v.getId('auditionText').oninput();
 const choosing=v.getId('useVoice').onclick();await v.flush();
 assert.equal(v.getId('systemVoice').disabled,true);
 assert.equal(v.getId('customAudition').disabled,true);
 await v.getId('customAudition').onclick();
 await v.getId('systemVoice').onclick();await v.flush();release();await choosing;await v.flush();
 assert.equal(v.getId('systemVoice').disabled,false);
 assert.equal(calls.filter(([op,p])=>op==='voice.selection.save'&&p.selected==='').length,1);
 assert.ok(!calls.some(([op])=>op==='voice.audition.text'));v.dispose();
});
test('local test service visibly marks simulated speech and test credit without asking for payment',async()=>{
 const calls=[],v=await mount(setup(calls,{local_test:true}),'voices');await v.flush();
 assert.equal(v.getId('cloudTestNotice').hidden,false);
 assert.match(v.getId('cloudTestNotice').textContent,/本地测试/);
 assert.match(v.getId('cloudTestNotice').textContent,/系统声音模拟/);
 await v.getId('cloudRedeem').onclick();await v.flush();
 assert.match(contents(v.getId('dialog')),/无需付款/);
 assert.doesNotMatch(contents(v.getId('dialog')),/购买后/);v.dispose();
});
test('ordinary client does not display a local test notice',async()=>{
 const v=await mount(setup([]),'voices');await v.flush();
 assert.equal(v.getId('cloudTestNotice').hidden,true);v.dispose();
});
test('registration explains six-place password next to input and validates before submitting',async()=>{
 const calls=[],base=setup(calls,{authenticated:false});
 const v=await mount(async(op,p)=>{if(op==='voice.cloud.register'){calls.push([op,p]);return {};}return base(op,p);},'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await click(v,'还没有账号？注册');
 const password=v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音密码');
 const username=v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音账号');
 const submit=v.find(n=>n.tag==='button'&&n.textContent==='注册并登录');
 assert.match(contents(password.parentElement),/6—128位/);
 assert.equal(submit.disabled,true);
 username.value='tester';password.value='abc12';password.oninput();
 assert.equal(submit.disabled,true);assert.match(contents(password.parentElement),/还差1位/);
 await submit.onclick({stopPropagation(){}});assert.ok(!calls.some(([op])=>op==='voice.cloud.register'));
 password.value='abc123';password.oninput();assert.equal(submit.disabled,false);
 await submit.onclick({stopPropagation(){}});await v.flush();
 assert.ok(calls.some(([op,p])=>op==='voice.cloud.register'&&p.password==='abc123'));assert.equal(password.value,'');v.dispose();
});
test('registration blocks overlong passwords while typing without contacting the service',async()=>{
 const calls=[],v=await mount(setup(calls,{authenticated:false}),'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await click(v,'还没有账号？注册');
 const password=v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音密码');
 v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音账号').value='tester';
 password.value='a'.repeat(129);password.oninput();
 assert.equal(v.find(n=>n.tag==='button'&&n.textContent==='注册并登录').disabled,true);
 assert.match(contents(password.parentElement),/最多128位/);
 assert.ok(!calls.some(([op])=>op==='voice.cloud.register'));v.dispose();
});
test('registration counts non-BMP password characters the same as the service',async()=>{
 const calls=[],v=await mount(setup(calls,{authenticated:false}),'voices');await v.flush();
 await v.getId('cloudAccount').onclick();await click(v,'还没有账号？注册');
 const password=v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音密码');
 const submit=v.find(n=>n.tag==='button'&&n.textContent==='注册并登录');
 v.find(n=>n.tag==='input'&&n.attrs['aria-label']==='配音账号').value='tester';
 for(const [length,disabled] of [[5,true],[6,false],[128,false],[129,true]]){
  password.value='𠀀'.repeat(length);password.oninput();assert.equal(submit.disabled,disabled);
 }
 assert.ok(!calls.some(([op])=>op==='voice.cloud.register'));v.dispose();
});

test('first use restoration rule is inline and acknowledged once without modal',async()=>{
 const calls=[],base=setup(calls);let seen=false;
 const v=await mount(async(op,p)=>{
  if(op==='voice.cloud.policy.seen'){calls.push([op,p]);seen=true;return {recorded:true};}
  const result=await base(op,p);
  return op==='voice.cloud.read'?{...result,policy_notice_required:!seen,policy_notice:'原音频无法恢复时自动重新生成，按成功字数扣费。'}:result;
 },'voices');await v.flush();
 assert.equal(v.getId('cloudPolicyNotice').hidden,false);
 assert.match(v.getId('cloudPolicyText').textContent,/按成功字数扣费/);
 assert.equal(v.getId('dialog').children.length,0);
 await v.getId('cloudPolicySeen').onclick();await v.flush();
 assert.equal(v.getId('cloudPolicyNotice').hidden,true);
 assert.equal(calls.filter(([op])=>op==='voice.cloud.policy.seen').length,1);v.dispose();
});
