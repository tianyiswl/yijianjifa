const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

const original=()=>({id:'channels-login',delivery_id:'original-target',account_id:'channels-account',account_name:'视频号甲',platform:'channels',kind:'login',reason:'login_required',final_started:false,browser_login:true,external_browser:true,message:'请登录原账号',view_revision:1,view_state:'ready',view_mode:'browser'});
const responses=calls=>calls.filter(c=>['multi_publish.intervention.respond','publish.intervention.respond'].includes(c.op));
const consent=v=>v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标');
const feedback=v=>v.find(n=>n.dataset.feedback==='true');
const noMirror=v=>{for(const predicate of [n=>n.tag==='img',n=>n.dataset.frame==='true',n=>n.dataset.frameControls==='true',n=>n.textContent==='验证遇到问题？',n=>n.textContent==='更新画面',n=>n.textContent==='向下滚动'])assert.throws(()=>v.find(predicate));};
async function fixture({request={},respond}={}){
 let r={...original(),...request};const calls=[];
 const v=await mount(async(op,p)=>{
  calls.push({op,p});if(op==='publish.intervention.status')return {request:r?.delivery_id?null:r};
  if(op==='multi_publish.intervention.status')return {request:!r?.delivery_id||p.delivery_id&&p.delivery_id!==r?.delivery_id?null:r};
  if(op==='multi_publish.intervention.defer')return {deferred:true,delivery_id:p.delivery_id};
  if(['multi_publish.intervention.respond','publish.intervention.respond'].includes(op)){
   if(respond)return respond(p,r);if(p.action==='visible'){r.view_revision++;r.view_state='queued';r.view_action='visible';}
   return {accepted:true,view_revision:r.view_revision};
  }throw Error(op);
 },'globals',['publish_intervention']);return {v,calls,r,setRequest:value=>r=value};
}

for(const [platform,label] of [['channels','视频号'],['douyin','抖音'],['kuaishou','快手'],['bilibili','B站']])for(const reason of ['login_required','identity_unavailable','identity_mismatch'])test(platform+' '+reason+' uses the real browser login controls without a screenshot or pointer surface',async()=>{
 const {v,calls}=await fixture({request:{platform,reason,image:'stale-screenshot'}});noMirror(v);
 assert.match(v.find(n=>n.dataset.message==='true').textContent,new RegExp('请在打开的浏览器中登录原'+label+'账号'));
 assert(v.find(n=>n.tag==='button'&&n.textContent==='打开登录页面'));
 assert(v.find(n=>n.tag==='button'&&n.textContent==='已登录，继续'));
 assert.throws(()=>v.find(n=>/账号身份（UID）/.test(n.textContent)));
 assert(consent(v));assert.equal(responses(calls).length,0);v.dispose();
});

test('opening the original browser does not need consent or continue the publishing target',async()=>{
 const {v,calls,r}=await fixture();await v.click('打开登录页面');
 assert.deepEqual(responses(calls).map(c=>c.p),[{id:r.id,delivery_id:r.delivery_id,action:'visible'}]);
 assert.equal(consent(v).checked,undefined);assert.match(feedback(v).textContent,/正在打开登录页面/);
 assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,true);
 r.external_browser=true;r.view_state='ready';await v.tick();
 assert.match(feedback(v).textContent,/登录页面已打开/);assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,false);
 assert.equal(calls.some(c=>/execution\.start|publish\.confirm|account\.login\.start|account\.backend/.test(c.op)),false);noMirror(v);v.dispose();
});

test('a real browser ready response without an image finishes the opening feedback immediately',async()=>{
 const {v}=await fixture({respond:(p,r)=>({accepted:true,view_revision:2,request:{...r,view_revision:2,view_state:'ready',view_mode:'browser',external_browser:true}})});
 await v.click('打开登录页面');assert.match(feedback(v).textContent,/登录页面已打开/);noMirror(v);v.dispose();
});

test('polling a browser opening preserves the same account checkbox and never repeatedly opens or resumes the session',async()=>{
 const {v,calls,r}=await fixture({request:{external_browser:false,view_state:'queued'}});const check=consent(v);check.checked=true;
 assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,true);
 r.external_browser=true;r.view_state='ready';for(let i=0;i<4;i++)await v.tick();
 assert.equal(consent(v),check);assert.equal(check.checked,true);assert.equal(responses(calls).length,0);noMirror(v);
 await v.click('已登录，继续');assert.deepEqual(responses(calls).at(-1).p,{id:r.id,delivery_id:r.delivery_id,action:'done',consent_same_identity:true});
 assert.equal(calls.some(c=>/execution\.start|publish\.confirm|target\.reprepare/.test(c.op)),false);v.dispose();
});

test('continuing requires original account consent even when the native browser is already open',async()=>{
 const {v,calls,r}=await fixture({request:{external_browser:true,view_state:'ready'}});await v.click('已登录，继续');assert.equal(responses(calls).length,0);
 assert.match(feedback(v).textContent,/确认仍使用/);consent(v).checked=true;await v.click('已登录，继续');
 assert.deepEqual(responses(calls).at(-1).p,{id:r.id,delivery_id:r.delivery_id,action:'done',consent_same_identity:true});v.dispose();
});

test('a native browser worker failure is visible without an image and permits an explicit retry',async()=>{
 const {v,calls,r}=await fixture();await v.click('打开登录页面');r.view_state='failed';r.view_error='登录页面未能打开，请重试';await v.tick();
 assert.match(feedback(v).textContent,/登录页面未能打开，请重试/);assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,false);
 await v.click('打开登录页面');assert.equal(responses(calls).length,2);assert.match(feedback(v).textContent,/正在打开登录页面/);noMirror(v);v.dispose();
});

test('automatic browser-opening failure is shown before the user clicks and a bridge failure remains retryable',async()=>{
 const {v,r}=await fixture({request:{view_state:'failed',view_error:'原登录窗口启动失败'},respond:()=>{throw Error('原浏览器暂时无法唤起');}});
 assert.match(feedback(v).textContent,/原登录窗口启动失败/);await v.click('打开登录页面');assert.match(feedback(v).textContent,/原浏览器暂时无法唤起/);
 for(let i=0;i<2;i++)await v.tick();assert.match(feedback(v).textContent,/原浏览器暂时无法唤起/);assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,false);noMirror(v);v.dispose();
});

test('closing the browser prompt leaves it deferred and reopening the requested target never creates a mirrored page',async()=>{
 const {v,calls,r}=await fixture();await v.click('关闭');for(let i=0;i<2;i++)await v.tick();assert.throws(()=>consent(v));
 assert.equal(await v.window.p011OpenPublishVerification(r.delivery_id),true);noMirror(v);assert(consent(v));
 assert.equal(responses(calls).length,0);assert.equal(calls.some(c=>/execution\.start|publish\.confirm/.test(c.op)),false);v.dispose();
});

test('browser mode has trusted controls and never grants a scripted open or continue',async()=>{
 const {v,calls}=await fixture();consent(v).checked=true;
 for(const label of ['打开登录页面','已登录，继续'])v.find(n=>n.tag==='button'&&n.textContent===label).onclick({isTrusted:false});
 await v.flush();assert.equal(responses(calls).length,0);v.dispose();
});

test('an older browser snapshot cannot finish an opening revision or claim the browser opened',async()=>{
 const {v,r}=await fixture();await v.click('打开登录页面');const revision=r.view_revision;
 r.view_revision=revision-1;r.view_state='ready';await v.tick();assert.match(feedback(v).textContent,/正在打开登录页面/);
 r.view_revision=revision;r.external_browser=false;await v.tick();assert.match(feedback(v).textContent,/正在打开登录页面/);
 r.external_browser=true;await v.tick();assert.match(feedback(v).textContent,/登录页面已打开/);v.dispose();
});

for(const request of [{final_started:true},{inspection_id:'original-inspection'}])test('browser-login flag cannot replace post-submit verification or an inspection viewer: '+JSON.stringify(request),async()=>{
 const {v}=await fixture({request});assert(v.find(n=>n.tag==='img'));assert.throws(()=>v.find(n=>n.tag==='button'&&n.textContent==='打开登录页面'));v.dispose();
});

test('ordinary publishing uses its own exact request without multi-platform consent or screenshot controls',async()=>{
 const {v,calls,r}=await fixture({request:{delivery_id:undefined,platform:'douyin'}});noMirror(v);
 assert.throws(()=>consent(v));assert.match(v.find(n=>n.dataset.message==='true').textContent,/原抖音账号/);
 await v.click('打开登录页面');assert.deepEqual(responses(calls).at(-1),{op:'publish.intervention.respond',p:{id:r.id,action:'visible',code:''}});
 r.view_state='ready';await v.tick();assert.match(feedback(v).textContent,/登录页面已打开/);
 await v.click('已登录，继续');assert.deepEqual(responses(calls).at(-1),{op:'publish.intervention.respond',p:{id:r.id,action:'done',code:''}});
 assert.equal(calls.some(c=>c.op==='multi_publish.intervention.respond'),false);v.dispose();
});

test('ordinary native login can be closed without calling a nonexistent multi-platform defer',async()=>{
 const {v,calls,r}=await fixture({request:{delivery_id:undefined,platform:'douyin'}});await v.click('稍后处理');
 assert.deepEqual(responses(calls).at(-1),{op:'publish.intervention.respond',p:{id:r.id,action:'cancel',code:''}});
 assert.equal(calls.some(c=>c.op==='multi_publish.intervention.defer'),false);v.dispose();
});

test('account management can display only the exact ordinary native request before opening its browser',async()=>{
 const {v,calls,r}=await fixture({request:{delivery_id:undefined,platform:'douyin'}});
 assert.equal(await v.window.p011OpenPublishVerification({request_id:'different',account_id:r.account_id}),false);
 assert.equal(await v.window.p011OpenPublishVerification({request_id:r.id,account_id:'another'}),false);
 assert.equal(await v.window.p011OpenPublishVerification({request_id:r.id,account_id:r.account_id}),true);noMirror(v);
 assert.equal(responses(calls).length,0);v.dispose();
});

test('a browser show requested by account management updates an already successful dialog on its newer revision',async()=>{
 const {v,r}=await fixture();assert.match(feedback(v).textContent,/登录页面已打开/);
 r.view_revision++;r.view_action='visible';r.view_state='queued';await v.tick();assert.match(feedback(v).textContent,/正在打开登录页面/);
 r.view_state='failed';r.view_error='原登录窗口已关闭，请重新打开';await v.tick();assert.match(feedback(v).textContent,/原登录窗口已关闭/);
 assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,false);v.dispose();
});

test('a newer external visible revision replaces an acknowledged pending browser action and can finish',async()=>{
 const {v,r}=await fixture();await v.click('打开登录页面');assert.equal(r.view_revision,2);
 r.view_revision=3;r.view_action='visible';r.view_state='queued';await v.tick();assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,true);
 r.view_revision=2;r.view_state='ready';await v.tick();assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,true);
 r.view_revision=3;r.view_state='ready';await v.tick();assert.match(feedback(v).textContent,/登录页面已打开/);assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,false);v.dispose();
});

test('a newer external browser snapshot cannot finish a local display action before its response revision is acknowledged',async()=>{
 let release;const {v,r}=await fixture({respond:()=>new Promise(resolve=>release=()=>resolve({accepted:true,view_revision:3}))});await v.click('打开登录页面');
 r.view_revision=2;r.view_action='visible';r.view_state='ready';await v.tick();assert.match(feedback(v).textContent,/正在打开登录页面/);
 r.view_revision=3;await v.tick();assert.match(feedback(v).textContent,/正在打开登录页面/);assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,true);
 release();await v.flush();assert.match(feedback(v).textContent,/登录页面已打开/);assert.equal(v.find(n=>n.textContent==='打开登录页面').disabled,false);v.dispose();
});
