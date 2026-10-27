const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
const account={id:'a',nickname:'测试账号',remark:'',group_name:'',login_state:'valid',is_default:false,capabilities:{ordinary:'pending',commerce:'pending',location_scope:'pending'}};

test('a transient check timeout does not replace a saved login with a login failure',async()=>{
 const row={...account,platform:'douyin',login_check:{state:'unavailable',reason:'timeout',checked_at:'2026-10-27T12:10:29Z'}};
 const v=await mount(op=>{if(op==='account.login.availability')return {available:false};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;throw Error(op);},'accounts');
 const text=n=>[n.textContent,...n.children.map(text)].join('');
 const cells=v.getId('rows').children[0].children;
 assert.match(text(cells[4]),/^已保存登录/);
 assert.equal(text(cells[6]),'发布前检查');
 assert.match(text(cells[4]),/最近检查：检查超时/);
 v.getId('rows').children[0].onclick({target:{}});await v.flush();
 assert(v.find(n=>n.textContent==='登录状态：已保存登录'));
 assert.throws(()=>v.find(n=>n.textContent==='登录状态：检查超时'));v.dispose();
});

function groupedAccounts(extraGroups=['门店A']){
 const rows=[{...account},{...account,id:'b',platform:'bilibili',nickname:'账号二'}],writes=[];
 const handler=(op,p)=>{
  if(op==='account.login.availability')return {available:false};
  if(op==='account.list'){const filtered=rows.filter(r=>!p.group||r.group_name===p.group);return {items:filtered.map(r=>({...r})),total:filtered.length,groups:[...new Set([...extraGroups,...rows.map(r=>r.group_name)].filter(Boolean))]};}
  if(op==='account.get')return {...rows.find(r=>r.id===p.id)};
  if(op==='account.save'){writes.push(p);Object.assign(rows.find(r=>r.id===p.id),{remark:p.remark,group_name:p.group_name.trim()});return {...rows.find(r=>r.id===p.id)};}
  throw Error(op);
 };
 return {handler,writes,rows};
}
test('another platform account can select an existing group returned outside the current rows',async()=>{
 const data=groupedAccounts(),v=await mount(data.handler,'accounts');
 v.getId('rows').children[1].onclick({target:{}});await v.flush();
 const group=v.find(n=>n.id==='account-group_name');assert.equal(group.tag,'select');
 assert.ok(group.children.some(n=>n.textContent==='门店A'));
 group.value='group:门店A';group.onchange();
 await v.click('保存备注与分组');
 assert.deepEqual(data.writes,[{id:'b',remark:'',group_name:'门店A'}]);
 assert.equal(data.rows[0].group_name,'');v.dispose();
});
test('a newly saved group can be reused by another account and is still offered after reopening',async()=>{
 const data=groupedAccounts();let v=await mount(data.handler,'accounts');
 v.getId('rows').children[0].onclick({target:{}});await v.flush();
 const group=v.find(n=>n.id==='account-group_name');assert.equal(group.tag,'select');
 group.value='new';group.onchange();const name=v.find(n=>n.id==='account-new-group');
 assert.equal(name.hidden,false);name.value='内容运营';name.oninput();await v.click('保存备注与分组');
 v.getId('rows').children[1].onclick({target:{}});await v.flush();
 assert.ok(v.find(n=>n.id==='account-group_name').children.some(n=>n.textContent==='内容运营'));
 v.dispose();v=await mount(data.handler,'accounts');v.getId('rows').children[1].onclick({target:{}});await v.flush();
 const reopened=v.find(n=>n.id==='account-group_name');assert.ok(reopened.children.some(n=>n.textContent==='内容运营'));
 reopened.value='group:内容运营';reopened.onchange();await v.click('保存备注与分组');
 assert.equal(data.rows[1].group_name,'内容运营');v.dispose();
});
test('empty new group is not silently saved as ungrouped and explicit ungrouped is allowed',async()=>{
 const data=groupedAccounts(),v=await mount(data.handler,'accounts');
 v.getId('rows').children[0].onclick({target:{}});await v.flush();
 const group=v.find(n=>n.id==='account-group_name');assert.equal(group.tag,'select');
 group.value='new';group.onchange();await v.click('保存备注与分组');
 assert.equal(data.writes.length,0);assert.match(v.getId('toast').textContent,/请输入新分组名称/);
 group.value='none';group.onchange();await v.click('保存备注与分组');
 assert.equal(data.writes[0].group_name,'');v.dispose();
});
test('moving the last account out of the filtered group refreshes the list after resetting the filter',async()=>{
 const data=groupedAccounts([]);data.rows[0].group_name='门店A';
 const v=await mount(data.handler,'accounts');v.getId('group').value='门店A';v.getId('group').onchange();await v.flush();
 assert.equal(v.getId('rows').children.length,1);v.getId('rows').children[0].onclick({target:{}});await v.flush();
 const group=v.find(n=>n.id==='account-group_name');group.value='none';group.onchange();await v.click('保存备注与分组');await v.flush();
 assert.equal(v.getId('group').value,'');assert.equal(v.getId('rows').children.length,2);
 assert.equal(data.rows[0].group_name,'');v.dispose();
});
test('account login stays disabled when native provider is unavailable',async()=>{
 const calls=[];const view=await mount(async(op)=>{calls.push(op);if(op==='account.list')return {items:[],total:0,groups:[]};if(op==='account.login.availability')return {available:false};throw Error(op);},'accounts',null,{commerceEnabled:true});
 assert.equal(view.getId('addAccount').disabled,true);
 view.getId('addAccount').onclick({isTrusted:true});await view.flush();assert.ok(!calls.includes('account.login.start'));view.dispose();
});
test('configured login saves credentials without claiming a current live session or rewriting observed capabilities',async()=>{
 let logged=false;const calls=[];
 const view=await mount(async(op,p)=>{calls.push([op,p]);if(op==='account.login.availability')return {available:true};if(op==='account.list')return {items:logged?[account]:[],total:logged?1:0,groups:[]};if(op==='account.login.start'){logged=true;return {id:'j',state:'completed',account_id:'a'};}if(op==='account.get')return account;throw Error(op);},'accounts',null,{commerceEnabled:true});
 assert.equal(view.getId('addAccount').disabled,false);
 view.getId('addAccount').onclick({isTrusted:true});await view.flush();
 assert.ok(!calls.some(([op])=>op==='account.login.start'));
 await view.click('抖音');await view.flush();
 assert.deepEqual(calls.find(([op])=>op==='account.login.start')[1],{account_id:null,platform:'douyin'});
 assert.match(view.getId('toast').textContent,/资料已保存.*发布前.*检查/);
 assert.equal(account.capabilities.ordinary,'pending');
 assert.equal(view.getId('detailSub').textContent,'抖音账号');assert.equal(view.getId('modal').style.display,'none');view.dispose();
});

test('ordinary readiness requires a publish-time check instead of treating saved login as a current live session',async()=>{
 const rows=[
  {...account,id:'d',platform:'douyin'},
  {...account,id:'k',platform:'kuaishou',capabilities:{...account.capabilities,ordinary:'not_found'}},
  {...account,id:'c',platform:'channels'},
  {...account,id:'b',platform:'bilibili'},
  {...account,id:'expired',platform:'douyin',login_state:'expired',capabilities:{...account.capabilities,ordinary:'available'}},
  {...account,id:'pending',platform:'douyin',login_state:'pending_verification',capabilities:{...account.capabilities,ordinary:'available'}},
 ];
 const before=JSON.stringify(rows),calls=[];
 const v=await mount((op,p)=>{calls.push(op);if(op==='account.login.availability')return {available:true,platforms:['douyin','kuaishou','channels','bilibili']};if(op==='multi_publish.catalog')return {items:[{platform:'douyin',ordinary_ready:true},{platform:'kuaishou',ordinary_ready:false},{platform:'channels',ordinary_ready:false},{platform:'bilibili',ordinary_ready:false}]};if(op==='account.list')return {items:rows,total:rows.length,groups:[]};if(op==='account.get')return rows.find(r=>r.id===p.id);throw Error(op);},'accounts');
 const text=n=>[n.textContent,...n.children.map(text)].join('');
 for(let i=0;i<rows.length;i++){
  const expected=i<4?'发布前检查':i===4?'需重新登录':'待检查';
  assert.equal(text(v.getId('rows').children[i].children[6]),expected);
  v.getId('rows').children[i].onclick({target:{}});await v.flush();
  assert.ok(v.find(n=>n.tag==='p'&&text(n)==='视频发布：'+expected));
 }
 assert.equal(JSON.stringify(rows),before);
 assert.equal(calls.some(op=>op==='account.save'||op.startsWith('account.capabilities.')),false);
 v.dispose();
});

for(const [state,label] of [['valid','已登录'],['login_required','需要重新登录'],['identity_mismatch','账号不一致'],['unavailable','暂时无法确认']])test('account list and detail show '+state+' with its recorded check time without another network check',async()=>{
 const calls=[],row={...account,platform:'channels',login_check:{checked_at:'2026-10-23T06:00:00+00:00',state}};
 const v=await mount((op,p)=>{calls.push(op);if(op==='account.login.availability')return {available:false};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;throw Error(op);},'accounts');
 const text=n=>[n.textContent,...n.children.map(text)].join('');
 assert.match(text(v.getId('rows').children[0]),new RegExp('最近检查.*'+label+'.*2026'));
 v.getId('rows').children[0].onclick({target:{}});await v.flush();
 assert(v.find(n=>n.tag==='p'&&n.textContent.includes('最近检查')&&n.textContent.includes(label)&&n.textContent.includes('2026')));
 assert.equal(calls.some(op=>/session\.check|check_session|account\.check|capabilities/.test(op)),false);v.dispose();
});

test('an account with only saved credentials is labelled untested rather than currently available',async()=>{
 const row={...account,platform:'channels',login_check:null};
 const v=await mount((op)=>{if(op==='account.login.availability')return {available:false};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;throw Error(op);},'accounts');
 const text=n=>[n.textContent,...n.children.map(text)].join('');assert.match(text(v.getId('rows').children[0]),/已保存登录.*最近检查：尚未检查/);
 assert.doesNotMatch(text(v.getId('rows').children[0]),/当前可用|实时可用/);v.dispose();
});

test('cancelling platform choice never starts a login',async()=>{
 let starts=0;const view=await mount(op=>{if(op==='account.login.availability')return {available:true};if(op==='account.list')return {items:[],total:0,groups:[]};if(op==='account.login.start'){starts++;return {id:'j',state:'cancelled'};}throw Error(op);},'accounts',null,{commerceEnabled:true});
 view.getId('addAccount').onclick({isTrusted:true});await view.flush();
 assert.equal(starts,0);await view.click('取消添加');assert.equal(starts,0);
 assert.equal(view.getId('addAccount').disabled,false);assert.equal(view.getId('modal').style.display,'none');view.dispose();
});

test('capability button explains upload and does not upload when native adapter is unavailable',async()=>{
 const calls=[];let logged=false;
 const view=await mount((op,p)=>{calls.push(op);if(op==='account.login.availability')return {available:true};if(op==='account.list')return {items:logged?[account]:[],total:logged?1:0,groups:[]};if(op==='account.login.start'){logged=true;return {id:'j',state:'completed',account_id:'a'};}if(op==='account.get')return account;if(op==='account.capabilities.prepare')return {nickname:'测试账号',size_bytes:12345,available:false};throw Error(op);},'accounts',null,{commerceEnabled:true});
 view.getId('addAccount').onclick({isTrusted:true});await view.flush();await view.click('抖音');await view.flush();
 await view.click('检查能力');await view.flush();
 assert.ok(calls.includes('account.capabilities.prepare'));assert.ok(!calls.includes('account.capabilities.start'));
 assert.equal(view.getId('modal').style.display,'none');view.dispose();
});
test('context menu acts on right-clicked account and clamps within window',async()=>{
 const calls=[];const b={...account,id:'b',nickname:'账号二'};
 const v=await mount((op,p)=>{calls.push([op,p]);if(op==='account.login.availability')return {available:false};if(op==='account.list')return {items:[account,b],total:2,groups:[]};if(op==='account.get')return b;throw Error(op);},'accounts',null,{commerceEnabled:true});
 v.window.innerWidth=800;v.window.innerHeight=600;v.getId('ctx').getBoundingClientRect=()=>({width:200,height:200});
 v.getId('rows').children[1].oncontextmenu({preventDefault(){},clientX:799,clientY:599});
 assert.equal(v.getId('ctx').style.left,'592px');assert.equal(v.getId('ctx').style.top,'392px');
 const buttons=v.getId('ctx').children;assert.equal(buttons.length,6);assert.equal(buttons[1].disabled,true);assert.equal(buttons[2].disabled,true);
 buttons[0].onclick({isTrusted:true});await v.flush();assert.equal(calls.find(([op])=>op==='account.get')[1].id,'b');assert.equal(v.getId('ctx').style.display,'none');v.dispose();
});
test('account removal confirms the targeted nickname and refreshes only after confirmation',async()=>{
 const calls=[];let removed=false;
 const v=await mount((op,p)=>{calls.push([op,p]);if(op==='account.login.availability')return {available:false};if(op==='account.list')return {items:removed?[]:[account],total:removed?0:1,groups:[]};if(op==='account.delete'){removed=true;return {removed:true};}throw Error(op);},'accounts',null,{commerceEnabled:true});
 v.window.innerWidth=800;v.window.innerHeight=600;v.getId('ctx').getBoundingClientRect=()=>({width:200,height:260});
 async function openRemoval(){v.getId('rows').children[0].oncontextmenu({preventDefault(){},clientX:400,clientY:300});v.getId('ctx').children[5].onclick({isTrusted:true});await v.flush();}
 await openRemoval();assert.equal(calls.filter(([op])=>op==='account.delete').length,0);
 await v.click('取消');assert.equal(removed,false);
 await openRemoval();v.getId('dialog').children[2].children[1].onclick({isTrusted:true});await v.flush();
 assert.equal(calls.find(([op])=>op==='account.delete')[1].id,account.id);assert.equal(removed,true);assert.equal(v.getId('modal').style.display,'none');v.dispose();
});
for(const [platform,label] of [['douyin','抖音'],['kuaishou','快手'],['channels','视频号'],['bilibili','B站']])test('visible delete entry confirms platform and clears only the removed '+platform+' account',async()=>{
 const target={...account,platform,is_default:true,nickname:'同名账号'},other={...account,id:'other',platform:'douyin',nickname:'同名账号'},calls=[];let removed=false;
 const v=await mount((op,p)=>{calls.push([op,p]);if(op==='account.login.availability')return {available:false};if(op==='account.list')return {items:removed?[other]:[target,other],total:removed?1:2,groups:[]};if(op==='account.get')return {...target};if(op==='account.delete'){removed=true;return {removed:true};}throw Error(op);},'accounts');
 const checkbox=v.getId('rows').children[0].children[0].children[0];checkbox.checked=true;checkbox.onchange();
 v.getId('rows').children[0].onclick({target:{}});await v.flush();
 const entry=v.find(n=>n.tag==='button'&&n.textContent==='删除账号');assert.match(entry.className,/danger/);
 await v.click('删除账号');assert.match(v.getId('dialog').children[1].textContent,new RegExp(label+' · 同名账号'));assert.equal(removed,false);
 await v.click('取消');assert.equal(calls.some(([op])=>op==='account.delete'),false);
 await v.click('删除账号');await v.click('确认删除');
 assert.deepEqual(calls.filter(([op])=>op==='account.delete'),[['account.delete',{id:target.id}]]);
 assert.equal(v.getId('rows').children.length,1);assert.equal(v.getId('selectedCount').textContent,'已选 0 项');
 assert.equal(v.getId('detailSub').textContent,'选择一个账号查看');assert.equal(v.getId('modal').style.display,'none');v.dispose();
});
test('blocked account deletion shows an explicit reason and keeps account details and selection',async()=>{
 const v=await mount((op)=>{if(op==='account.login.availability')return {available:false};if(op==='account.list')return {items:[account],total:1,groups:[]};if(op==='account.get')return {...account};if(op==='account.delete')throw Error('账号仍被未完成任务使用，请先处理相关任务。');throw Error(op);},'accounts');
 const checkbox=v.getId('rows').children[0].children[0].children[0];checkbox.checked=true;checkbox.onchange();
 v.getId('rows').children[0].onclick({target:{}});await v.flush();await v.click('删除账号');await v.click('确认删除');
 assert.equal(v.getId('modal').style.display,'grid');assert.equal(v.getId('dialog').children[0].textContent,'账号未删除');
 assert.match(v.getId('dialog').children[1].textContent,/未完成任务/);await v.click('知道了');
 assert.equal(v.getId('rows').children.length,1);assert.equal(v.getId('selectedCount').textContent,'已选 1 项');assert.equal(v.getId('detailSub').textContent,'抖音账号');
 await v.click('删除账号');await v.click('取消');v.dispose();
});
test('capability starts directly for selected account and closes progress after completion',async()=>{
 const calls=[];
 const v=await mount((op,p)=>{calls.push([op,p]);if(op==='account.login.availability')return {available:true};if(op==='account.list')return {items:[account],total:1,groups:[]};if(op==='account.get')return account;if(op==='account.capabilities.prepare')return {available:true,fixture_sha256:'fixture'};if(op==='account.capabilities.start')return {id:'probe',state:'completed',account_id:'a'};throw Error(op);},'accounts',null,{commerceEnabled:true});
 v.getId('rows').children[0].onclick({target:{}});await v.flush();await v.click('检查能力');await v.flush();
 assert.deepEqual(calls.find(([op])=>op==='account.capabilities.start')[1],{account_id:'a',upload_consent:true,fixture_sha256:'fixture'});
 assert.equal(v.getId('modal').style.display,'none');v.dispose();
});

test('shelved commerce removes account probe and capability detail without changing account data',async()=>{
 const calls=[];const v=await mount((op)=>{calls.push(op);if(op==='account.login.availability')return {available:true};if(op==='account.list')return {items:[account],total:1,groups:[]};if(op==='account.get')return account;throw Error(op);},'accounts');
 v.getId('rows').children[0].onclick({target:{}});await v.flush();
 assert.equal(v.getId('rows').children[0].children.length,8);
 assert.throws(()=>v.find(n=>n.tag==='button'&&n.textContent==='检查能力'),/not found/);
 assert.ok(!calls.includes('account.capabilities.start'));assert.equal(account.capabilities.commerce,'pending');v.dispose();
});


test('account login time is visible in both list and detail',async()=>{
 const row={...account,last_login_at:'2026-10-03T12:00:00+00:00'};
 const v=await mount(op=>{if(op==='account.login.availability')return {available:true};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;throw Error(op);},'accounts');
 assert.match(v.getId('rows').children[0].children[5].textContent,/2026/);
 v.getId('rows').children[0].onclick({target:{}});await v.flush();
 assert.ok(v.find(n=>n.tag==='p'&&n.textContent.startsWith('最近登录时间：2026')));v.dispose();
});

test('new platform account entry shows guidance instead of invoking Douyin login',async()=>{const calls=[];const row={...account,platform:'bilibili',nickname:'B站乙'};const v=await mount((op,p)=>{calls.push([op,p]);if(op==='multi_publish.catalog')return {items:[{platform:'douyin',label:'抖音',login_ready:true},{platform:'bilibili',label:'B站',login_ready:false}]};if(op==='account.login.availability')return {available:true};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;throw Error(op);},'accounts');v.getId('addAccount').onclick({isTrusted:true});await v.flush();await v.click('B站');assert.ok(v.getId('toast').textContent.includes('待接入'));assert.equal(calls.some(([op])=>op==='account.login.start'),false);v.getId('rows').children[0].onclick({target:{}});await v.flush();assert.equal(v.getId('detailSub').textContent,'B站账号');await v.click('接入说明');assert.equal(calls.some(([op])=>op==='account.login.start'),false);v.dispose();});

test('adding a configured Kuaishou account saves the right platform without claiming publication',async()=>{
 const calls=[],row={...account,id:'k',platform:'kuaishou',nickname:'快手测试账号'};let logged=false;
 const v=await mount((op,p)=>{calls.push({op,p});if(op==='account.login.availability')return {available:true,platforms:['douyin','kuaishou']};if(op==='multi_publish.catalog')return {items:[{platform:'douyin',login_ready:true,ordinary_ready:true},{platform:'kuaishou',login_ready:true,ordinary_ready:false}]};if(op==='account.list')return {items:logged?[row]:[],total:logged?1:0,groups:[]};if(op==='account.login.start'){logged=true;return {id:'k-login',state:'completed',account_id:'k',platform:'kuaishou'};}if(op==='account.get')return row;throw Error(op);},'accounts',null,{commerceEnabled:true});
 v.getId('addAccount').onclick({isTrusted:true});await v.flush();await v.click('快手');
 assert.deepEqual(calls.find(c=>c.op==='account.login.start').p,{account_id:null,platform:'kuaishou'});
 assert.equal(v.getId('detailSub').textContent,'快手账号');assert.equal(v.getId('modal').style.display,'none');
 assert.match(v.getId('toast').textContent,/登录资料已保存.*发布通道.*待接入/);
 assert.ok(v.find(n=>n.textContent==='登录已接入；发布通道仍待接入，现有登录记录不代表可投稿。'));
 assert.throws(()=>v.find(n=>n.tag==='button'&&n.textContent==='检查能力'),/not found/);v.dispose();
});
test('Kuaishou relogin passes account identity and leaves only capability probes unavailable',async()=>{
 const calls=[],row={...account,id:'k',platform:'kuaishou',nickname:'快手测试账号'};
 const v=await mount((op,p)=>{calls.push({op,p});if(op==='account.login.availability')return {available:true,platforms:['kuaishou']};if(op==='multi_publish.catalog')return {items:[{platform:'kuaishou',login_ready:true,ordinary_ready:false}]};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;if(op==='account.login.start')return {id:'k-login',state:'completed',account_id:'k',platform:'kuaishou'};throw Error(op);},'accounts',null,{commerceEnabled:true});
 v.getId('rows').children[0].onclick({target:{}});await v.flush();const checkbox=v.find(n=>n.attrs['aria-label']==='选择账号 '+row.nickname);checkbox.checked=true;checkbox.onchange();assert.equal(v.getId('checkSelected').disabled,false);
 const backend=v.find(n=>n.tag==='button'&&n.textContent==='打开后台');assert.equal(backend.disabled,false);
 v.getId('rows').children[0].oncontextmenu({preventDefault(){},clientX:20,clientY:20});assert.equal(v.getId('ctx').children[1].disabled,false);assert.equal(v.getId('ctx').children[2].disabled,false);
 await v.click('重新登录');assert.deepEqual(calls.find(c=>c.op==='account.login.start').p,{account_id:'k',platform:'kuaishou'});
 assert.equal(calls.some(c=>c.op==='account.backend.start'||c.op.startsWith('account.capabilities.')),false);v.dispose();
});
test('Kuaishou backend opens the saved account and uses platform-specific progress without uploading',async()=>{
 const calls=[],row={...account,id:'k',platform:'kuaishou',nickname:'快手测试账号'};let finish;
 const v=await mount((op,p)=>{calls.push({op,p});if(op==='account.login.availability')return {available:true,platforms:['kuaishou']};if(op==='multi_publish.catalog')return {items:[{platform:'kuaishou',login_ready:true,ordinary_ready:false}]};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;if(op==='account.backend.start')return new Promise(resolve=>{finish=resolve;});throw Error(op);},'accounts',null,{commerceEnabled:true});
 v.getId('rows').children[0].onclick({target:{}});await v.flush();await v.click('打开后台');
 assert.deepEqual(calls.find(c=>c.op==='account.backend.start').p,{account_id:'k'});
 assert.notEqual(v.getId('modal').style.display,'grid');
 let navigated=false;await v.window.p011Navigate(()=>{navigated=true;});assert.equal(navigated,true);
 assert.equal(calls.some(c=>c.op==='account.login.cancel'),false);
 finish({id:'k-backend',state:'completed',account_id:'k',platform:'kuaishou'});await v.flush();
 assert.notEqual(v.getId('modal').style.display,'grid');assert.match(v.getId('toast').textContent,/快手后台已关闭，账号资料已刷新/);
 assert.equal(calls.some(c=>c.op==='account.login.start'||c.op.startsWith('account.capabilities.')||c.op.startsWith('publish.')||/^multi_publish\.(?!catalog$)/.test(c.op)),false);v.dispose();
});
test('integrated Bili and Channels creator providers expose their own backend entry',async()=>{
 const rows=['channels','bilibili'].map((platform,i)=>({...account,id:String(i),platform}));
 const v=await mount((op,p)=>{if(op==='account.login.availability')return {available:true,platforms:['channels','bilibili']};if(op==='account.list')return {items:rows,total:rows.length,groups:[]};if(op==='account.get')return rows.find(r=>r.id===p.id);throw Error(op);},'accounts');
 for(const row of v.getId('rows').children){row.onclick({target:{}});await v.flush();assert.equal(v.find(n=>n.tag==='button'&&n.textContent==='打开后台').disabled,false);}
 v.dispose();
});
for(const [platform,label] of [['channels','视频号'],['bilibili','B站']])test(label+' backend uses the saved account and never starts publication',async()=>{
 const calls=[],row={...account,id:'creator',platform,nickname:'合成账号'};let finish;
 const v=await mount((op,p)=>{calls.push({op,p});if(op==='account.login.availability')return {available:true,platforms:[platform]};if(op==='multi_publish.catalog')return {items:[{platform,login_ready:true,ordinary_ready:true}]};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;if(op==='account.backend.start')return new Promise(resolve=>{finish=resolve;});throw Error(op);},'accounts');
 v.getId('rows').children[0].onclick({target:{}});await v.flush();await v.click('打开后台');
 assert.deepEqual(calls.find(c=>c.op==='account.backend.start').p,{account_id:'creator'});
 assert.notEqual(v.getId('modal').style.display,'grid');
 finish({id:'creator-backend',state:'completed',account_id:'creator',platform});await v.flush();
 assert.match(v.getId('toast').textContent,new RegExp(label+'后台已关闭，账号资料已刷新'));
 assert.equal(calls.some(c=>c.op==='account.login.start'||c.op.startsWith('publish.')||/^multi_publish\.(?!catalog$)/.test(c.op)),false);v.dispose();
});
test('provider platform inventory takes priority over a stale catalog login flag',async()=>{
 const calls=[];const v=await mount((op,p)=>{calls.push({op,p});if(op==='account.login.availability')return {available:true,platforms:['douyin']};if(op==='multi_publish.catalog')return {items:[{platform:'douyin',login_ready:true},{platform:'kuaishou',login_ready:true}]};if(op==='account.list')return {items:[],total:0,groups:[]};throw Error(op);},'accounts');
 v.getId('addAccount').onclick({isTrusted:true});await v.flush();await v.click('快手');assert.match(v.getId('toast').textContent,/待接入/);assert.equal(calls.some(c=>c.op==='account.login.start'),false);v.dispose();
});
test('Kuaishou provider remains usable when the optional platform catalog cannot be read',async()=>{
 const calls=[];const v=await mount((op,p)=>{calls.push({op,p});if(op==='account.login.availability')return {available:true,platforms:['kuaishou']};if(op==='account.list')return {items:[],total:0,groups:[]};if(op==='account.login.start')return {id:'k-login',state:'cancelled',platform:'kuaishou'};throw Error(op);},'accounts');
 v.getId('addAccount').onclick({isTrusted:true});await v.flush();await v.click('快手');assert.deepEqual(calls.find(c=>c.op==='account.login.start').p,{account_id:null,platform:'kuaishou'});v.dispose();
});
