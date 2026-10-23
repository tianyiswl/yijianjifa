const test=require('node:test'),assert=require('node:assert/strict');
const {mount:mountBase,baseData}=require('./support/commerce_view.cjs');
// Explicit initial V1 compatibility intent for existing Douyin-only behaviors.
const mount=(handler,name='commerce',incoming,extra)=>mountBase(handler,name,name==='publish'?{...incoming,compatibility:true}:incoming,extra);
test('opening shows cached places without search and explicit search refreshes backend',async()=>{
 const requests=[];
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};
  if(op==='publish.locations.recent')return {items:[{scope:'national',query:'北海',snapshot:{available:true,token:'cached',items:[{id:'s',name:'地点',address:'地址'}]}}]};
  if(op==='publish.locations.search'){requests.push(p);return {available:true,cached:!p.refresh,needs_verification:!p.refresh,token:p.refresh?'fresh':'cached',items:[{id:'s',name:'地点',address:'地址'}]};}
  if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('当前账号发布地点');assert.equal(requests.length,0);await view.click('选择地点：地点 · 地址');assert.ok(view.find(n=>n.textContent==='本次选择：地点 · 地址'));await view.click('国内 · 北海');await view.click('搜索当前账号地点');
 assert.equal(requests[0].scope,'national');assert.equal(requests[0].refresh,true);
 await view.click('选择地点：地点 · 地址');assert.ok(view.find(n=>n.textContent==='本次选择：地点 · 地址'));assert.equal(requests.length,1);
 await view.click('从后台刷新地点');assert.equal(requests[1].refresh,true);
 await view.click('选择地点：地点 · 地址');assert.ok(view.find(n=>n.textContent==='本次选择：地点 · 地址'));view.dispose();
});
test('ordinary locations require explicit apply, support result pages and single-item clear/restore',async()=>{
 const sent=[];
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};
  if(op==='publish.locations.search')return {available:true,token:'locations-token',items:Array.from({length:21},(_,i)=>({id:'s'+i,name:'地点'+i,address:'地址'+i}))};
  if(op==='publish.prepare'){sent.push(p);const r=baseData('commerce.prepare');r.items[0].account_id='a';return r;}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');await view.click('本账号发布地点');
 view.find(n=>n.attrs['aria-label']==='地点名称或地址').value='地点';await view.click('搜索当前账号地点');await view.click('地点下一页');await view.click('选择地点：地点20 · 地址20');await view.click('取消');
 // Account, schedule and videos now share the same page.
view.find(n=>n.attrs['aria-label']==='视频分配方式').value='split_across_accounts';const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');assert.deepEqual(sent.at(-1).locations,{});
 // Account, schedule and videos now share the same page.
await view.click('本账号发布地点');view.find(n=>n.attrs['aria-label']==='地点名称或地址').value='地点';await view.click('搜索当前账号地点');await view.click('选择地点：地点0 · 地址0');await view.click('应用地点');// Account, schedule and videos now share the same page.
await view.click('预览发布安排');
 assert.deepEqual(sent.at(-1).locations,{a:{batch:{token:'locations-token',id:'s0'},items:{}}});
 await view.click('修改本账号这条地点');await view.click('本次不添加地点');await view.click('应用地点');await view.click('预览发布安排');assert.deepEqual(sent.at(-1).locations.a.items,{v:null});
 await view.click('修改本账号这条地点');await view.click('恢复本账号地点');await view.click('预览发布安排');assert.deepEqual(sent.at(-1).locations.a.items,{});view.dispose();
});
async function commonFixture(results){const sent=[],queries=[];
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'},{id:'b',nickname:'账号B'}],total:2};
  if(op==='publish.locations.search'){queries.push(p.account_id);return {available:true,token:'token-'+p.account_id,items:results[p.account_id]};}
  if(op==='publish.prepare'){sent.push(p);const r=baseData('commerce.prepare');r.items[0].account_id='b';return r;}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');await view.click('高级设置');await view.click('全选','发布账号全选');
 async function common(){await view.click('统一发布地点');view.find(n=>n.attrs['aria-label']==='地点名称或地址').value='地点';await view.click('搜索所选账号地点');await view.click('选择地点：共同地点 · 地址');await view.click('应用地点');}
 async function preview(){// Account, schedule and videos now share the same page.
view.find(n=>n.attrs['aria-label']==='视频分配方式').value='whole_batch';const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');}
 async function account(name){// Account, schedule and videos now share the same page.
const search=view.find(n=>n.attrs['aria-label']==='搜索发布账号');search.value=name;search.oninput();await view.click('本账号发布地点');}
 return {view,sent,queries,common,preview,account};
}
test('common location uses separate account references and preserves account and item exceptions',async()=>{
 const row={id:'s',name:'共同地点',address:'地址'};const {view,sent,queries,common,preview,account}=await commonFixture({a:[row],b:[row]});
 await common();await preview();assert.deepEqual(queries,['a','b']);assert.equal(sent.at(-1).locations.a.batch.token,'token-a');assert.equal(sent.at(-1).locations.b.batch.token,'token-b');
 await account('账号A');await view.click('本次不添加地点');await view.click('应用地点');await preview();
 await view.click('修改本账号这条地点');await view.click('本次不添加地点');await view.click('应用地点');// Account, schedule and videos now share the same page.
await common();await preview();
 assert.equal(sent.at(-1).locations.a.batch,null);assert.deepEqual(sent.at(-1).locations.b.items,{v:null});
 await account('账号A');await view.click('恢复统一地点');await preview();assert.equal(sent.at(-1).locations.a.batch.token,'token-a');view.dispose();
});
test('same location id with a different address in another account cannot be inherited silently',async()=>{
 const {view,sent,common,preview,account}=await commonFixture({a:[{id:'s',name:'共同地点',address:'地址'}],b:[{id:'s',name:'共同地点',address:'其他地址'}]});
 await common();await preview();assert.equal(sent.length,0);assert.match(view.ids.get('toast').textContent,/账号B/);
 await account('账号B');await view.click('本次不添加地点');await view.click('应用地点');await preview();assert.equal(sent.at(-1).locations.b.batch,null);assert.equal(sent.at(-1).locations.a.batch.token,'token-a');view.dispose();
});
test('cancelling a common location search stops remaining account queries and ignores late results',async()=>{
 const queries=[];let finish;
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'},{id:'b',nickname:'账号B'}],total:2};
  if(op==='publish.locations.search'){queries.push(p.account_id);return new Promise(resolve=>finish=resolve);}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');await view.click('全选','发布账号全选');await view.click('统一发布地点');view.find(n=>n.attrs['aria-label']==='地点名称或地址').value='地点';await view.click('搜索所选账号地点');await view.click('搜索所选账号地点');assert.deepEqual(queries,['a']);
 await view.click('取消');finish({available:true,token:'old-token',items:[{id:'s',name:'旧结果',address:'地址'}]});await view.flush();assert.deepEqual(queries,['a']);assert.equal(view.ids.get('overlay').hidden,true);view.dispose();
});
