const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
test('publish cover must load a preview before applying and can restore without writing the finished cover',async()=>{
 const requests=[],ops=[];
 const view=await mount((op,p)=>{
  ops.push(op);
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};
  if(op==='publish.prepare'){requests.push(p);const r=baseData('commerce.prepare');r.items[0].account_id='a';return r;}
  if(op==='publish.cover.stage')return {token:'retained-cover',kind:'first_frame',frame_ms:0};
  if(op==='finished.cover.frame')return {token:'cover-token',kind:p.kind,frame_ms:p.frame_ms,url:'p011media://asset/fixture'};
  if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');// Account, schedule and videos now share the same page.
view.find(n=>n.attrs['aria-label']==='视频分配方式').value='split_across_accounts';const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');
 await view.click('修改本账号这条封面');await view.click('生成封面预览');
 const apply=view.find(n=>n.tag==='button'&&n.textContent==='应用本条封面');assert.equal(apply.disabled,true);await view.click('应用本条封面');assert.equal(view.ids.get('overlay').hidden,false);
 view.find(n=>n.tag==='img'&&n.alt==='本次发布封面预览').onload();assert.equal(apply.disabled,false);await view.click('应用本条封面');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{'a:v':'retained-cover'});
 await view.click('修改本账号这条封面');await view.click('恢复成品封面');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{});assert.equal(ops.includes('finished.cover.save'),false);view.dispose();
});
test('retaining a publish cover suppresses duplicate clicks and a failed copy cannot apply a preview token',async()=>{
 let fail,writes=0;const requests=[];
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};
  if(op==='publish.prepare'){requests.push(p);const r=baseData('commerce.prepare');r.items[0].account_id='a';return r;}
  if(op==='finished.cover.frame')return {token:'temporary',kind:'first_frame',frame_ms:0,url:'p011media://asset/fixture'};
  if(op==='publish.cover.stage'){writes++;return new Promise((resolve,reject)=>fail=reject);}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');// Account, schedule and videos now share the same page.
view.find(n=>n.attrs['aria-label']==='视频分配方式').value='split_across_accounts';const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');
 await view.click('修改本账号这条封面');await view.click('生成封面预览');view.find(n=>n.tag==='img'&&n.alt==='本次发布封面预览').onload();
 await view.click('应用本条封面');await view.click('应用本条封面');assert.equal(writes,1);fail(Error('模拟磁盘写入失败'));await view.flush();assert.equal(view.ids.get('overlay').hidden,false);
 await view.click('取消');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{});view.dispose();
});
test('common and account covers preserve item exceptions and block newly selected uncovered videos',async()=>{
 const requests=[],batches=[];let seq=0;
 const view=await mount((op,p)=>{
  if(op==='publish.candidates'){const r=baseData(op);return {...r,default_account_id:'a',items:[...r.items,{...r.items[0],id:'v2',display_name:'第二视频'}]};}
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};
  if(op==='publish.prepare'){requests.push(p);const r=baseData('commerce.prepare');r.items[0].account_id='a';return r;}
  if(op==='finished.cover.frame')return {token:'preview',kind:p.kind,frame_ms:p.frame_ms,url:'p011media://asset/fixture'};
  if(op==='publish.cover.batch.start'){batches.push(p);const n=++seq;return {id:'j',state:'completed',done:p.video_ids.length,total:p.video_ids.length,result:{covers:Object.fromEntries(p.video_ids.map(id=>[id,{token:'batch'+n+id}]))}};}
  if(op==='publish.cover.stage')return {token:'item'};
  if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');// Account, schedule and videos now share the same page.
view.find(n=>n.attrs['aria-label']==='视频分配方式').value='whole_batch';
 const check=name=>{const n=view.find(n=>n.attrs['aria-label']==='选择 '+name);n.checked=true;n.onchange();};check('测试视频');
 const apply=async label=>{await view.click('生成封面预览');view.find(n=>n.tag==='img'&&n.alt==='本次发布封面预览').onload();await view.click(label);};
 await view.click('统一发布封面');await apply('应用批量封面');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{'a:v':'batch1v'});
 await view.click('本账号批量封面');await apply('应用批量封面');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{'a:v':'batch2v'});
 await view.click('修改本账号这条封面');await view.click('恢复成品封面');await view.click('统一发布封面');await apply('应用批量封面');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{});
 await view.click('修改本账号这条封面');await view.click('恢复上级封面');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{'a:v':'batch2v'});
 await view.click('本账号批量封面');await view.click('恢复上级封面');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{'a:v':'batch3v'});
 check('第二视频');const count=requests.length;await view.click('预览发布安排');assert.equal(requests.length,count);assert.match(view.ids.get('toast').textContent,/重新应用/);
 await view.click('统一发布封面');await apply('应用批量封面');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{'a:v':'batch4v','a:v2':'batch4v2'});assert.deepEqual(batches.at(-1).video_ids,['v','v2']);view.dispose();
});
test('failed batch replacement preserves the prior common covers for both accounts',async()=>{
 let batches=0;const requests=[];
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'},{id:'b',nickname:'账号B'}],total:2};
  if(op==='publish.prepare'){requests.push(p);return baseData('commerce.prepare');}
  if(op==='finished.cover.frame')return {token:'preview',kind:p.kind,frame_ms:p.frame_ms,url:'p011media://asset/fixture'};
  if(op==='publish.cover.batch.start'){if(++batches>1)throw Error('本批复制失败');return {id:'j',state:'completed',done:1,total:1,result:{covers:{v:{token:'saved'}}}};}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');const account=view.find(n=>n.attrs['aria-label']==='发布账号 账号B');account.checked=true;account.onchange();// Account, schedule and videos now share the same page.
view.find(n=>n.attrs['aria-label']==='视频分配方式').value='whole_batch';const video=view.find(n=>n.attrs['aria-label']==='选择 测试视频');video.checked=true;video.onchange();
 const apply=async()=>{await view.click('统一发布封面');await view.click('生成封面预览');view.find(n=>n.tag==='img'&&n.alt==='本次发布封面预览').onload();await view.click('应用批量封面');};
 await apply();await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{'a:v':'saved','b:v':'saved'});
 await apply();assert.equal(view.ids.get('overlay').hidden,false);await view.click('取消');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).cover_overrides,{'a:v':'saved','b:v':'saved'});view.dispose();
});
test('batch cover editor requires an assigned account',async()=>{
 const view=await mount((op)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');// Account, schedule and videos now share the same page.
const video=view.find(n=>n.attrs['aria-label']==='选择 测试视频');video.checked=true;video.onchange();await view.click('取消全选','发布账号取消全选');await view.click('统一发布封面');assert.match(view.ids.get('toast').textContent,/先选择账号/);view.dispose();
});
