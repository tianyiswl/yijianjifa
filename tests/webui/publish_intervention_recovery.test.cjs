const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

async function view(request){
 const calls=[];
 const v=await mount((op,p)=>{
  calls.push({op,p});
  if(op==='publish.intervention.status')return {request:null};
  if(op==='multi_publish.intervention.status')return {request,pending:1};
  if(op==='multi_publish.intervention.respond'){if(['visible','refresh','scroll','pointer'].includes(p.action)){request.view_state='queued';request.view_revision=(request.view_revision||0)+1;}return {accepted:true,view_revision:request.view_revision};}
  throw Error(op);
 },'globals',['publish_intervention']);
 await v.click('验证遇到问题？');return {v,calls,request};
}
const request=(kind,final_started=true)=>({id:'r1',delivery_id:'d1',account_id:'a1',account_name:'快手甲',
 platform:'kuaishou',kind,final_started,message:'请完成原账号验证',image:'initial-frame'});

test('post-final SMS is transient and responds to the original target without submit controls',async()=>{
 const {v,calls}=await view(request('sms'));
 const input=v.find(n=>n.attrs['aria-label']==='验证码');input.value='654321';
 await v.click('确认验证码');
 assert.equal(input.value,'');
 assert.deepEqual(calls.find(c=>c.op==='multi_publish.intervention.respond').p,
  {id:'r1',delivery_id:'d1',action:'code',code:'654321'});
 assert.ok(v.find(n=>/避免重复发布/.test(n.textContent)));
 assert.throws(()=>v.find(n=>n.textContent==='继续发布'||n.textContent==='显示浏览器处理'));
 await v.click('检查验证状态');
 assert.equal(calls.filter(c=>c.op==='multi_publish.intervention.respond').at(-1).p.action,'done');
 v.dispose();
});

test('post-final complex verification retains original screenshot and only sends human pointer',async()=>{
 const {v,calls}=await view(request('complex'));
 const img=v.find(n=>n.tag==='img');
 assert.equal(img.src,'data:image/png;base64,initial-frame');
 img.naturalWidth=1440;img.naturalHeight=1000;img.setPointerCapture=()=>{};
 img.onpointerdown({isTrusted:true,pointerId:1,clientX:10,clientY:20,preventDefault(){}});
 img.onpointerup({isTrusted:true,clientX:10,clientY:20});await v.flush();
 assert.deepEqual(calls.find(c=>c.op==='multi_publish.intervention.respond').p,
  {id:'r1',delivery_id:'d1',action:'pointer',code:[80,200]});
 assert.throws(()=>v.find(n=>n.textContent==='继续发布'));
 v.dispose();
});

test('login QR refreshes the same original-page image and consent still gates a pre-final response',async()=>{
 const initial=request('login',false),{v,calls}=await view(initial);
 const img=v.find(n=>n.tag==='img');
 assert.equal(img.src,'data:image/png;base64,initial-frame');
 assert.equal(typeof img.onpointerup,'function');
 initial.image='refreshed-frame';await v.tick();
 assert.equal(v.find(n=>n.tag==='img'),img);
 assert.equal(img.src,'data:image/png;base64,refreshed-frame');
 await v.click('已登录，继续');
 assert.equal(calls.some(c=>c.op==='multi_publish.intervention.respond'),false);
 v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标').checked=true;
 await v.click('已登录，继续');
 assert.deepEqual(calls.find(c=>c.op==='multi_publish.intervention.respond').p,
  {id:'r1',delivery_id:'d1',action:'done',consent_same_identity:true});
 v.dispose();
});
