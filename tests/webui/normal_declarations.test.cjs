const test=require('node:test'),assert=require('node:assert/strict');
const {mount:mountBase,baseData}=require('./support/commerce_view.cjs');
// Explicit initial V1 compatibility intent for existing Douyin-only behaviors.
const mount=(handler,name='commerce',incoming,extra)=>mountBase(handler,name,name==='publish'?{...incoming,compatibility:true}:incoming,extra);
// Per-batch exceptions remain in advanced mode; saved defaults now live in account management.
async function openAccountDeclaration(view){if(view.find(n=>n.dataset.action==='enter-advanced'||n.dataset.action==='back-quick').dataset.action==='enter-advanced')await view.click('高级设置');await view.click('本账号内容声明');}
test('ordinary account declaration selection and explicit item clear remain scoped and refresh revokes choices',async()=>{
 const requests=[];let queryCount=0;
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};
  if(op==='publish.declarations.query'){if(p.cache_only)return {available:false,items:[]};queryCount++;return {available:true,required:false,token:'policy-'+queryCount,items:[{id:'d',label:'实际选项'}]};}
  if(op==='publish.prepare'){requests.push(p);const result=baseData('commerce.prepare');result.items[0].account_id='a';return result;}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');await view.click('本账号内容声明');
 await view.click('刷新平台声明');
 const select=view.find(n=>n.attrs['aria-label']==='本账号批量声明');assert.equal(select.value,'');select.value='d';
 await view.click('应用声明');// Account, schedule and videos now share the same page.
view.find(n=>n.attrs['aria-label']==='视频分配方式').value='split_across_accounts';
 const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');
 assert.deepEqual(requests.at(-1).declarations,{a:{token:'policy-1',batch:'d',items:{}}});
 await view.click('修改本账号这条声明');view.find(n=>n.attrs['aria-label']==='本账号单条声明').value='';await view.click('应用声明');await view.click('预览发布安排');
 assert.deepEqual(requests.at(-1).declarations.a.items,{v:null});
 await view.click('修改本账号这条声明');await view.click('恢复本账号批量声明');await view.click('预览发布安排');assert.deepEqual(requests.at(-1).declarations.a.items,{});
 // Account, schedule and videos now share the same page.
await view.click('本账号内容声明');await view.click('刷新平台声明');await view.click('取消');// Account, schedule and videos now share the same page.
await view.click('预览发布安排');
 assert.equal(requests.at(-1).declarations.a,undefined);view.dispose();
});
test('ordinary declaration refresh ignores duplicate clicks and a late result cannot reopen a cancelled editor',async()=>{
 let count=0,finish;
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};
  if(op==='publish.declarations.query'){if(p.cache_only)return {available:false,items:[]};count++;return new Promise(resolve=>finish=resolve);}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await openAccountDeclaration(view);await view.click('刷新平台声明');await view.click('刷新平台声明');assert.equal(count,1);
 await view.click('取消');finish({available:true,required:false,token:'late',items:[]});await view.flush();assert.equal(view.ids.get('overlay').hidden,true);view.dispose();
});
async function twoAccountDeclarations(options){const requests=[];const view=await mount((op,p)=>{
 if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
 if(op==='account.list')return {items:[{id:'a',nickname:'账号A'},{id:'b',nickname:'账号B'}],total:2};
 if(op==='publish.declarations.query'&&p.cache_only)return {available:false,items:[]};
 if(op==='publish.declarations.query')return {available:true,required:false,token:'policy-'+p.account_id,items:options[p.account_id]};
 if(op==='publish.prepare'){requests.push(p);const result=baseData('commerce.prepare');result.items[0].account_id='b';return result;}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
},'publish');await view.click('高级设置');await view.click('全选','发布账号全选');
async function openAccount(name){const search=view.find(n=>n.attrs['aria-label']==='搜索发布账号');search.value=name;search.oninput();await view.click('本账号内容声明');}
for(const name of ['账号A','账号B']){await openAccount(name);await view.click('刷新平台声明');await view.click('取消');}
async function common(id){await view.click('统一内容声明');view.find(n=>n.attrs['aria-label']==='统一内容声明选项').value=id;await view.click('应用统一声明');}
async function preview(){// Account, schedule and videos now share the same page.
const mode=view.find(n=>n.attrs['aria-label']==='视频分配方式');mode.value='whole_batch';mode.onchange();const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');}
return {view,requests,openAccount,common,preview};}
test('common declarations preserve account and item exceptions and restoring account follows the new common value',async()=>{
 const options=[{id:'d',label:'声明D'},{id:'e',label:'声明E'}];const {view,requests,openAccount,common,preview}=await twoAccountDeclarations({a:options,b:options});
 await common('d');await preview();assert.equal(requests.at(-1).declarations.a.batch,'d');assert.equal(requests.at(-1).declarations.b.batch,'d');
 // Account, schedule and videos now share the same page.
await openAccount('账号A');view.find(n=>n.attrs['aria-label']==='本账号批量声明').value='';await view.click('应用声明');await preview();
 await view.click('修改本账号这条声明');view.find(n=>n.attrs['aria-label']==='本账号单条声明').value='';await view.click('应用声明');
 // Account, schedule and videos now share the same page.
await common('e');// Account, schedule and videos now share the same page.
await view.click('预览发布安排');
 assert.equal(requests.at(-1).declarations.a.batch,null);assert.equal(requests.at(-1).declarations.b.batch,'e');assert.deepEqual(requests.at(-1).declarations.b.items,{v:null});
 // Account, schedule and videos now share the same page.
await openAccount('账号A');await view.click('恢复统一声明');// Account, schedule and videos now share the same page.
await view.click('预览发布安排');assert.equal(requests.at(-1).declarations.a.batch,'e');view.dispose();
});
test('common declaration unsupported by an inheriting account blocks preview until that account has an explicit valid exception',async()=>{
 const {view,requests,openAccount,common,preview}=await twoAccountDeclarations({a:[{id:'d',label:'声明D'}],b:[{id:'b-only',label:'账号B选项'}]});
 await common('d');await preview();assert.equal(requests.length,0);assert.match(view.ids.get('toast').textContent,/账号B/);
 // Account, schedule and videos now share the same page.
await openAccount('账号B');view.find(n=>n.attrs['aria-label']==='本账号批量声明').value='b-only';await view.click('应用声明');// Account, schedule and videos now share the same page.
await view.click('预览发布安排');
 assert.equal(requests.at(-1).declarations.a.batch,'d');assert.equal(requests.at(-1).declarations.b.batch,'b-only');view.dispose();
});

test('refreshing one account withdraws the common declaration while preserving another account explicit exception',async()=>{
 const options=[{id:'d',label:'声明D'},{id:'e',label:'声明E'}];const {view,requests,openAccount,common,preview}=await twoAccountDeclarations({a:options,b:options});
 await common('d');await openAccount('账号A');view.find(n=>n.attrs['aria-label']==='本账号批量声明').value='e';await view.click('应用声明');
 await openAccount('账号B');await view.click('刷新平台声明');await view.click('取消');await preview();
 assert.equal(requests.at(-1).declarations.a.batch,'e');assert.equal(requests.at(-1).declarations.b,undefined);view.dispose();
});

test('stale cached declarations can be selected locally, block preparation, and survive verification',async()=>{
 const requests=[];let live=0;
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'}],total:1};
  if(op==='publish.declarations.query'){if(p.cache_only)return {available:false,cached:true,token:null,items:[{id:'d',label:'缓存声明'}]};live++;return {available:true,token:'fresh',items:[{id:'d',label:'缓存声明'}]};}
  if(op==='publish.prepare'){requests.push(p);return baseData('commerce.prepare');}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 await view.click('高级设置');await view.click('本账号内容声明');const select=view.find(n=>n.attrs['aria-label']==='本账号批量声明');assert.equal(select.disabled,false);select.value='d';await view.click('应用声明');assert.equal(live,0);assert.equal(view.find(n=>n.attrs['aria-label']==='已选内容声明').textContent,'缓存声明');assert.equal(view.find(n=>n.attrs['aria-label']==='已选发布地点').textContent,'');
 await view.click('本账号内容声明');assert.equal(view.find(n=>n.attrs['aria-label']==='本账号批量声明').value,'d');await view.click('取消');
 // Account, schedule and videos now share the same page.
const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');assert.equal(requests.length,0);assert.match(view.ids.get('toast').textContent,/刷新本账号声明/);
 // Account, schedule and videos now share the same page.
await view.click('本账号内容声明');await view.click('刷新平台声明');assert.equal(view.find(n=>n.attrs['aria-label']==='本账号批量声明').value,'d');await view.click('应用声明');// Account, schedule and videos now share the same page.
await view.click('预览发布安排');assert.equal(requests.at(-1).declarations.a.batch,'d');assert.equal(requests.at(-1).declarations.a.token,'fresh');view.dispose();
});

const builtinLabels=['内容由AI生成','内容为个人观点或见解','内容为转载信息','内容含营销推广信息','虚构演绎，仅供娱乐','无需添加自主声明'];
const builtinRows=builtinLabels.map(label=>({id:'ui-v1:'+require('node:crypto').createHash('sha256').update(label).digest('hex'),label}));
async function builtinDeclarations({query,accountRows=[{id:'a',nickname:'账号A',login_state:'invalid'}]}={}){
 const calls=[],requests=[];const view=await mount(async(op,p)=>{
  calls.push({op,p});if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:accountRows,total:accountRows.length};
  if(op==='publish.declarations.query')return query?query(p):{available:true,required:false,token:'builtin-'+p.account_id,items:builtinRows,source:'builtin',catalog_version:'fixture-v1',observed_at:'2026-09-28T00:00:00+00:00'};
  if(op==='publish.prepare'){requests.push(p);const result=baseData('commerce.prepare');result.items[0].account_id=p.account_ids.at(-1);return result;}
  if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish');
 return {view,calls,requests,async preview(){for(const id of ['publish-per-day','publish-days']){const input=view.find(n=>n.id===id);input.value='1';input.oninput();}const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');}};
}
test('built-in declarations load without a valid login marker and have no read, refresh, steps or update-time controls',async()=>{
 const {view,calls}=await builtinDeclarations();await openAccountDeclaration(view);const overlay=view.ids.get('overlay'),select=view.find(n=>n.attrs['aria-label']==='本账号批量声明');
 assert.equal(select.disabled,false);assert.equal(select.value,'');assert.equal(select.children.length,7);assert.deepEqual(select.children.slice(1).map(n=>n.textContent),builtinLabels);
 assert.equal(overlay.querySelectorAll('button').some(n=>/读取平台声明|刷新平台声明/.test(n.textContent)),false);assert.equal(overlay.querySelectorAll('.metadata-steps,.metadata-tools').length,0);
 assert(overlay.querySelectorAll('p').some(n=>/不选择.*不添加.*发布/.test(n.textContent)));assert.equal(overlay.querySelectorAll('span').some(n=>n.textContent.includes('上次更新')),false);
 assert.deepEqual(calls.filter(c=>c.op==='publish.declarations.query').map(c=>c.p),[{account_id:'a',refresh:false,cache_only:true}]);assert.equal(calls.some(c=>/account\.(login|capability)|publish\.(execute|confirm)/.test(c.op)),false);await view.click('取消');view.dispose();
});
test('applying a built-in declaration keeps its account token and reopening makes no refresh request',async()=>{
 const {view,calls,requests,preview}=await builtinDeclarations();await openAccountDeclaration(view);view.find(n=>n.attrs['aria-label']==='本账号批量声明').value=builtinRows[0].id;await view.click('应用声明');
 assert.equal(view.find(n=>n.attrs['aria-label']==='已选内容声明').textContent,builtinRows[0].label);await preview();assert.deepEqual(requests.at(-1).declarations,{a:{token:'builtin-a',batch:builtinRows[0].id,items:{}}});
 await openAccountDeclaration(view);assert.equal(view.find(n=>n.attrs['aria-label']==='本账号批量声明').value,builtinRows[0].id);assert.equal(calls.filter(c=>c.op==='publish.declarations.query').length,1);await view.click('取消');view.dispose();
});
test('leaving built-in declaration unselected explicitly applies no declaration',async()=>{
 const {view,requests,preview}=await builtinDeclarations();await openAccountDeclaration(view);await view.click('应用声明');await preview();assert.deepEqual(requests.at(-1).declarations.a,{token:'builtin-a',batch:null,items:{}});assert.equal(view.find(n=>n.attrs['aria-label']==='已选内容声明').textContent,'');view.dispose();
});
test('common built-in declarations automatically load each account locally and preserve account and item exceptions',async()=>{
 const {view,calls,requests,preview}=await builtinDeclarations({accountRows:[{id:'a',nickname:'账号A'},{id:'b',nickname:'账号B',login_state:'invalid'}]});await view.click('高级设置');await view.click('全选','发布账号全选');await view.click('统一内容声明');
 assert.equal(view.ids.get('overlay').hidden,false);assert.equal(view.find(n=>n.attrs['aria-label']==='统一内容声明选项').children.length,7);assert.deepEqual(calls.filter(c=>c.op==='publish.declarations.query').map(c=>c.p),[{account_id:'a',refresh:false,cache_only:true},{account_id:'b',refresh:false,cache_only:true}]);
 view.find(n=>n.attrs['aria-label']==='统一内容声明选项').value=builtinRows[0].id;await view.click('应用统一声明');const mode=view.find(n=>n.attrs['aria-label']==='视频分配方式');mode.value='whole_batch';mode.onchange();await preview();assert.equal(requests.at(-1).declarations.a.batch,builtinRows[0].id);assert.equal(requests.at(-1).declarations.b.batch,builtinRows[0].id);
 const search=view.find(n=>n.attrs['aria-label']==='搜索发布账号');search.value='账号A';search.oninput();await view.click('本账号内容声明');view.find(n=>n.attrs['aria-label']==='本账号批量声明').value='';await view.click('应用声明');await view.click('预览发布安排');
 await view.click('修改本账号这条声明');view.find(n=>n.attrs['aria-label']==='本账号单条声明').value='';await view.click('应用声明');await view.click('统一内容声明');view.find(n=>n.attrs['aria-label']==='统一内容声明选项').value=builtinRows[1].id;await view.click('应用统一声明');await view.click('预览发布安排');
 assert.equal(requests.at(-1).declarations.a.batch,null);assert.equal(requests.at(-1).declarations.b.batch,builtinRows[1].id);assert.deepEqual(requests.at(-1).declarations.b.items,{v:null});assert.equal(calls.filter(c=>c.op==='publish.declarations.query').length,2);assert(calls.filter(c=>c.op==='publish.declarations.query').every(c=>c.p.refresh===false&&c.p.cache_only===true));view.dispose();
});
test('cancelling a pending common local lookup prevents a late result from reopening or applying declarations',async()=>{
 let finish;const {view,requests,preview}=await builtinDeclarations({query:()=>new Promise(resolve=>finish=resolve)});await view.click('高级设置');await view.click('统一内容声明');assert.equal(view.ids.get('overlay').hidden,false);await view.click('取消');finish({available:true,required:false,token:'late',items:builtinRows,source:'builtin'});await view.flush();assert.equal(view.ids.get('overlay').hidden,true);await preview();assert.deepEqual(requests.at(-1).declarations,{});view.dispose();
});
test('a local declaration result from a cancelled account editor cannot overwrite the next account editor',async()=>{
 let finish;const {view}=await builtinDeclarations({accountRows:[{id:'a',nickname:'账号A'},{id:'b',nickname:'账号B'}],query:p=>p.account_id==='a'?new Promise(resolve=>finish=resolve):{available:true,required:false,token:'builtin-b',items:[builtinRows[1]],source:'builtin'}});await view.click('高级设置');await view.click('本账号内容声明');await view.click('取消');const search=view.find(n=>n.attrs['aria-label']==='搜索发布账号');search.value='账号B';search.oninput();await view.click('本账号内容声明');finish({available:true,required:false,token:'late-a',items:[builtinRows[0]],source:'builtin'});await view.flush();const select=view.find(n=>n.attrs['aria-label']==='本账号批量声明');assert.deepEqual(select.children.slice(1).map(n=>n.textContent),[builtinRows[1].label]);assert.equal(view.ids.get('overlay').querySelectorAll('button').some(n=>/刷新平台声明/.test(n.textContent)),false);await view.click('取消');view.dispose();
});
test('a pending local declaration lookup presents no platform-read controls or upload instructions and a cancelled error stays silent',async()=>{
 let fail;const {view}=await builtinDeclarations({query:()=>new Promise((_,reject)=>fail=reject)});await openAccountDeclaration(view);const overlay=view.ids.get('overlay');
 assert.equal(view.find(n=>n.attrs['aria-label']==='本账号批量声明').disabled,true);assert.equal(overlay.querySelectorAll('button').some(n=>/读取平台声明|刷新平台声明/.test(n.textContent)),false);assert.equal(overlay.querySelectorAll('p').some(n=>/上传|扫码|登录|尚未设置/.test(n.textContent)),false);assert.equal(overlay.querySelectorAll('.metadata-steps,.metadata-tools').length,0);
 await view.click('取消');const toast=view.ids.get('toast'),previous=toast.textContent;fail(Error('cancelled late error'));await view.flush();assert.equal(overlay.hidden,true);assert.equal(toast.textContent,previous);view.dispose();
});
test('a detached declaration apply action cannot modify declarations after the editor has closed',async()=>{
 const {view,requests,preview}=await builtinDeclarations();await openAccountDeclaration(view);const select=view.find(n=>n.attrs['aria-label']==='本账号批量声明'),apply=view.find(n=>n.tag==='button'&&n.textContent==='应用声明');select.value=builtinRows[0].id;await view.click('取消');apply.onclick({isTrusted:true});await view.flush();await preview();assert.deepEqual(requests.at(-1).declarations,{});view.dispose();
});
test('an unavailable account in a common local lookup prevents partial account policies or a declaration apply action',async()=>{
 const {view,requests,preview}=await builtinDeclarations({accountRows:[{id:'a',nickname:'账号A'},{id:'b',nickname:'账号B'}],query:p=>p.account_id==='a'?{available:true,required:false,token:'builtin-a',items:builtinRows,source:'builtin'}:{available:false,items:[]}});await view.click('高级设置');await view.click('全选','发布账号全选');await view.click('统一内容声明');assert.equal(view.ids.get('overlay').querySelectorAll('button').some(n=>n.textContent==='应用统一声明'),false);assert.match(view.find(n=>n.attrs.role==='status').textContent,/暂不可用/);await view.click('取消');await preview();assert.deepEqual(requests.at(-1).declarations,{});view.dispose();
});
