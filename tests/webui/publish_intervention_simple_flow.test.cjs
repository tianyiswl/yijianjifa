const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

async function fixture(overrides={}){
 let request={id:'sms-simple',delivery_id:'original-target',platform:'douyin',account_name:'演示账号',kind:'sms',final_started:true,sms_retry_after:60,...overrides};
 const calls=[];
 const v=await mount((op,p)=>{
  calls.push({op,p});
  if(op==='publish.intervention.status')return {request:null};
  if(op==='multi_publish.intervention.status')return {request};
  if(op==='multi_publish.intervention.defer')return {deferred:true,delivery_id:p.delivery_id};
  if(op==='multi_publish.intervention.respond'){
   request.view_revision=(request.view_revision||0)+1;request.view_action=p.action;request.view_state='queued';
   return {accepted:true,view_revision:request.view_revision};
  }
  throw Error(op);
 },'globals',['publish_intervention']);
 return {v,calls,request,setRequest:r=>request=r};
}
const footer=v=>v.find(n=>n.className==='p011-verification-actions');
const assistance=v=>v.find(n=>n.className==='p011-verification-assistance');
const responses=c=>c.filter(x=>x.op==='multi_publish.intervention.respond');

test('SMS has one primary action, a close button and optional help instead of four competing task controls',async()=>{
 const {v,calls}=await fixture();
 assert.deepEqual(footer(v).querySelectorAll('button').map(b=>b.textContent),['验证遇到问题？','关闭','确认验证码']);
 assert.equal(assistance(v).hidden,true);
 assert.equal(v.find(n=>n.tag==='button'&&n.dataset.action==='resend_code').disabled,true);
 assert.deepEqual(v.find(n=>(n.className||'').split(' ').includes('p011-verification-dialog')).querySelectorAll('button').filter(b=>b.className==='primary').map(b=>b.textContent),['确认验证码']);
 assert.throws(()=>v.find(n=>n.tag==='button'&&['取消本地目标','核对原提交结果','稍后处理'].includes(n.textContent)));
 assert.equal(responses(calls).length,0);v.dispose();
});

test('one SMS confirmation sends only the original code action and successful status closes without a second result button',async()=>{
 const {v,calls,request,setRequest}=await fixture();
 const input=v.find(n=>n.attrs['aria-label']==='验证码');input.value='123456';await v.click('确认验证码');
 assert.deepEqual(responses(calls).map(x=>x.p),[{id:'sms-simple',delivery_id:'original-target',action:'code',code:'123456'}]);
 request.code_state='submitted';request.view_state='ready';await v.tick();
 assert(v.find(n=>n.className==='p011-verification-overlay'));
 setRequest(null);await v.tick();assert.throws(()=>v.find(n=>n.className==='p011-verification-overlay'));
 assert.equal(responses(calls).length,1);v.dispose();
});

test('closing leaves the task for the task list and opening it again does not resume, cancel or submit',async()=>{
 const {v,calls}=await fixture();await v.click('关闭');
 assert.throws(()=>v.find(n=>n.className==='p011-verification-overlay'));
 for(let i=0;i<2;i++)await v.tick();
 assert.equal(responses(calls).length,0);
 assert.equal(calls.filter(x=>x.op==='multi_publish.intervention.defer').length,1);
 assert.equal(await v.window.p011OpenPublishVerification('original-target'),true);
 assert.equal(assistance(v).hidden,true);assert.equal(responses(calls).length,0);v.dispose();
});

test('optional help preserves the entered code and only explicitly opens the original verification page',async()=>{
 const {v,calls,request}=await fixture({final_started:false});
 const input=v.find(n=>n.attrs['aria-label']==='验证码');input.value='123456';
 const check=v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标');check.checked=true;
 await v.click('验证遇到问题？');assert.equal(assistance(v).hidden,false);assert.equal(responses(calls).length,0);
 await v.click('打开验证页面');
 assert.deepEqual(responses(calls).at(-1).p,{id:'sms-simple',delivery_id:'original-target',action:'visible'});
 request.view_mode='page';request.view_state='ready';request.image='synthetic-frame';await v.tick();
 await v.click('收起帮助');assert.equal(assistance(v).hidden,true);
 assert.equal(v.find(n=>n.attrs['aria-label']==='验证码'),input);assert.equal(input.value,'123456');assert.equal(check.checked,true);
 assert.equal(responses(calls).length,1);v.dispose();
});

for(const [kind,label] of [['login','已登录，继续'],['complex','验证完成']])test(kind+' presents a single clear continuation while preserving original post-submit checking',async()=>{
 const {v,calls}=await fixture({kind});
 assert.deepEqual(footer(v).querySelectorAll('button').map(b=>b.textContent),['验证遇到问题？','关闭',label]);
 await v.click(label);assert.deepEqual(responses(calls).at(-1).p,{id:'sms-simple',delivery_id:'original-target',action:'done'});
 assert.equal(calls.some(c=>/execution.start|reprepare|publish.confirm/.test(c.op)),false);v.dispose();
});
