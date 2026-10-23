const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
test('commerce batch and item cover choices inherit, restore and clear on confirmed account switch',async()=>{
 const sent=[],ops=[];
 const view=await mount((op,p)=>{
  ops.push(op);
  if(op==='commerce.context'){const r=baseData(op);r.accounts.push({...r.accounts[0],id:'b'});return r;}
  if(op==='commerce.prepare'){sent.push(p);return baseData(op);}
  if(op==='finished.cover.frame')return {token:'draft',url:'p011media://asset/test',kind:'first_frame',frame_ms:0};
  if(op==='publish.cover.batch.start')return {id:'j',state:'completed',done:1,total:1,result:{covers:{v:{token:'batch'}}}};
  if(op==='publish.cover.stage')return {token:'item'};
  return baseData(op);
 });
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
 async function apply(label){await view.click('生成封面预览');const button=view.find(n=>n.tag==='button'&&n.textContent===label);assert.equal(button.disabled,true);view.find(n=>n.tag==='img'&&n.alt==='本次发布封面预览').onload();await view.click(label);await view.click('检查发布安排');}
 await view.click('批量发布封面');await apply('应用批量封面');assert.deepEqual(sent.at(-1).cover_overrides,{'a:v':'batch'});
 await view.click('修改本条封面');await apply('应用本条封面');assert.deepEqual(sent.at(-1).cover_overrides,{'a:v':'item'});
 await view.click('修改本条封面');await view.click('恢复成品封面');await view.click('检查发布安排');assert.deepEqual(sent.at(-1).cover_overrides,{});
 await view.click('修改本条封面');await view.click('恢复批量封面');await view.click('检查发布安排');assert.deepEqual(sent.at(-1).cover_overrides,{'a:v':'batch'});
 const account=view.find(n=>n.attrs['aria-label']==='团购发布账号');account.value='b';let switching=account.onchange();await view.flush();await view.click('保留当前账号');await switching;await view.click('检查发布安排');assert.deepEqual(sent.at(-1).cover_overrides,{'a:v':'batch'});
 account.value='b';switching=account.onchange();await view.flush();await view.click('确认切换并清除');await switching;await view.click('检查发布安排');assert.equal(sent.at(-1).account_id,'b');assert.deepEqual(sent.at(-1).cover_overrides,{});assert.equal(ops.includes('finished.cover.save'),false);view.dispose();
});
test('commerce cover batch failure cannot replace applied settings and newly selected videos require reapplication',async()=>{
 const sent=[];let fail,calls=0;
 const view=await mount((op,p)=>{
  if(op==='publish.candidates'){const r=baseData(op);return {...r,items:[...r.items,{...r.items[0],id:'v2',display_name:'第二条'}],total:2};}
  if(op==='commerce.prepare'){sent.push(p);return baseData(op);}
  if(op==='finished.cover.frame')return {token:'draft',url:'p011media://asset/test',kind:'first_frame',frame_ms:0};
  if(op==='publish.cover.batch.start'){if(++calls===1)return {id:'j',state:'completed',done:1,total:1,result:{covers:{v:{token:'saved'}}}};return new Promise((resolve,reject)=>fail=reject);}
  return baseData(op);
 });
 const check=name=>{const n=view.find(n=>n.attrs['aria-label']==='选择团购视频 '+name);n.checked=true;n.onchange();};check('测试视频');
 const prepare=async()=>{await view.click('批量发布封面');await view.click('生成封面预览');view.find(n=>n.tag==='img'&&n.alt==='本次发布封面预览').onload();};
 await prepare();await view.click('应用批量封面');await prepare();await view.click('应用批量封面');await view.click('应用批量封面');assert.equal(calls,2);fail(Error('模拟复制失败'));await view.flush();assert.equal(view.ids.get('overlay').hidden,false);await view.click('取消');await view.click('检查发布安排');assert.deepEqual(sent.at(-1).cover_overrides,{'a:v':'saved'});
 check('第二条');const count=sent.length;await view.click('检查发布安排');assert.equal(sent.length,count);assert.match(view.ids.get('toast').textContent,/重新应用/);view.dispose();
});
test('commerce batch cancel waits for rollback and keeps the prior page settings',async()=>{
 let cancelled=false;const sent=[];
 const view=await mount((op,p)=>{
  if(op==='commerce.prepare'){sent.push(p);return baseData(op);}
  if(op==='finished.cover.frame')return {token:'draft',url:'p011media://asset/test',kind:'first_frame',frame_ms:0};
  if(op==='publish.cover.batch.start')return {id:'job',state:'running',done:0,total:1};
  if(op==='publish.cover.batch.cancel'){cancelled=true;return {id:'job',state:'cancelling',done:0,total:1};}
  if(op==='publish.cover.batch.status')return {id:'job',state:cancelled?'cancelled':'running',done:0,total:1};
  return baseData(op);
 });
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();await view.click('批量发布封面');await view.click('生成封面预览');view.find(n=>n.tag==='img'&&n.alt==='本次发布封面预览').onload();await view.click('应用批量封面');const cancel=view.find(n=>n.tag==='button'&&n.textContent==='取消批量处理');assert.equal(cancel.disabled,false);await view.click('取消批量处理');assert.equal(view.ids.get('overlay').hidden,false);await new Promise(r=>setTimeout(r,300));await view.flush();await view.click('取消');await view.click('检查发布安排');assert.deepEqual(sent.at(-1).cover_overrides,{});view.dispose();
});
