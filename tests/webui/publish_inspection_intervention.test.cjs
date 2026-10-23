const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
async function fixture(kind){const calls=[],request={id:'verify-1',inspection_id:'check-1',account_id:'a1',account_name:'视频号甲',platform:'channels',kind,final_started:false,message:'请完成原账号验证',image:'check-image'};
 const v=await mount((op,p)=>{calls.push({op,p});if(op==='publish.intervention.status')return {request:null};if(op==='multi_publish.intervention.status')return {request};if(op==='multi_publish.inspection.respond'){if(['visible','refresh','scroll','pointer'].includes(p.action)){request.view_state='queued';request.view_revision=(request.view_revision||0)+1;}return {accepted:true,view_revision:request.view_revision};}throw Error(op);},'globals',['publish_intervention']);return {v,calls,request};}
const responses=c=>c.filter(c=>c.op==='multi_publish.inspection.respond');
for(const kind of ['sms','login','complex','unknown'])test('inspection '+kind+' verification uses only the inspection handle and labels its boundary',async()=>{
 const {v,calls}=await fixture(kind);assert.ok(v.find(n=>n.textContent==='仅检查，不发表 · 视频号甲'));assert.ok(v.find(n=>n.textContent==='取消检查'));
 await v.click('已完成验证');assert.equal(responses(calls).length,0);
 v.find(n=>n.attrs['aria-label']==='确认同一账号继续检查').checked=true;await v.click('已完成验证');
 assert.deepEqual(responses(calls).at(-1).p,{id:'verify-1',inspection_id:'check-1',action:'done',consent_same_identity:true});
 await v.click('取消检查');assert.deepEqual(responses(calls).at(-1).p,{id:'verify-1',inspection_id:'check-1',action:'cancel'});
 assert.equal(calls.some(c=>c.op==='multi_publish.intervention.respond'||c.op==='publish.intervention.respond'||c.op==='multi_publish.intervention.defer'||c.op.includes('execution.start')),false);v.dispose();
});
test('inspection page viewer retains consent and never becomes a publishing verification',async()=>{
 const {v,calls,request}=await fixture('sms'),check=v.find(n=>n.attrs['aria-label']==='确认同一账号继续检查'),input=v.find(n=>n.attrs['aria-label']==='验证码');check.checked=true;input.value='456789';
 await v.click('在客户端展开页面');request.view_mode='page';request.view_state='ready';request.image='check-next-image';await v.tick();
 assert.equal(v.find(n=>n.attrs['aria-label']==='验证码'),input);assert.equal(input.value,'456789');assert.equal(v.find(n=>n.attrs['aria-label']==='确认同一账号继续检查'),check);
 await v.click('向下滚动');request.view_state='ready';await v.tick();await v.click('更新画面');request.view_state='ready';await v.tick();await v.click('确认验证码');
 assert.deepEqual(responses(calls).map(c=>c.p),[
  {id:'verify-1',inspection_id:'check-1',action:'visible'},
  {id:'verify-1',inspection_id:'check-1',action:'scroll',code:[0,450]},
  {id:'verify-1',inspection_id:'check-1',action:'refresh'},
  {id:'verify-1',inspection_id:'check-1',action:'code',code:'456789',consent_same_identity:true}
 ]);assert.equal(input.value,'');v.dispose();
});
