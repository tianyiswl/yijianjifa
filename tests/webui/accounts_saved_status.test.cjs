const test=require('node:test'),assert=require('node:assert/strict');
const {mount,Node}=require('./support/commerce_view.cjs');
const base=(id='a')=>({id,platform:'channels',nickname:'账号'+id,remark:'原备注',group_name:'门店',login_state:'valid',is_default:false,capabilities:{ordinary:'pending',commerce:'pending',location_scope:'pending'},login_check:null});
const text=node=>[node.textContent,...node.children.map(text)].join('');
async function fixture(rows=[base()]){
 const ids=new Map(),selectors=new Map(),listeners=new Map(),timers=new Map(),calls=[],delays=[];let serial=0,hold=null;
 const get=(map,key)=>{if(!map.has(key))map.set(key,new Node('container'));return map.get(key);};
 const document={body:new Node('body'),hidden:false,activeElement:null,createElement:tag=>new Node(tag),getElementById:id=>get(ids,id),querySelector:key=>get(selectors,key),querySelectorAll:()=>[],addEventListener:(name,fn)=>{if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn);},removeEventListener:(name,fn)=>listeners.get(name)?.delete(fn)};
 for(const id of ['platform','state'])document.getElementById(id).options=[new Node('option'),new Node('option')];
 const handler=async(op,p)=>{
  calls.push({op,p});if(op==='account.login.availability')return {available:false};
  if(op==='multi_publish.catalog')return {items:[]};
  if(op==='account.list'){
   const savedStates={ok:'valid',relogin:'expired',pending:'pending_verification'};
   const selected=rows.filter(r=>(!p.query||[r.nickname,r.remark].join(' ').includes(p.query))&&(!p.group||r.group_name===p.group)&&(!p.platform||r.platform===p.platform)&&(!p.state||r.login_state===savedStates[p.state]));
   const result={items:structuredClone(selected.slice(p.offset,p.offset+p.limit)),total:selected.length,groups:[...new Set(rows.map(r=>r.group_name))]};
   if(hold){const release=hold;hold=null;return release(result);}return result;
  }
  if(op==='account.get')return structuredClone(rows.find(r=>r.id===p.id));
  throw Error(op);
 };
 const view=await mount(handler,'accounts',null,{document,setTimeout:(fn,delay)=>{const id=++serial;timers.set(id,fn);delays.push(delay);return id;},clearTimeout:id=>timers.delete(id)});
 const walk=node=>[node,...node.children.flatMap(walk)];
 const find=predicate=>{const node=[document.body,...ids.values(),...selectors.values(),...[...ids.values()].flatMap(n=>n._closest?[n._closest]:[])].flatMap(walk).find(predicate);if(!node)throw Error('View node not found');return node;};
 const getId=id=>document.getElementById(id),click=async label=>{find(n=>n.tag==='button'&&n.textContent===label).onclick({isTrusted:true});await view.flush();};
 const tick=async()=>{const entry=timers.entries().next().value;if(entry){timers.delete(entry[0]);await entry[1]();}await view.flush();};
 const emit=async(name)=>{for(const fn of listeners.get(name)||[])fn({});await view.flush();};
 const open=async(index=0)=>{getId('rows').children[index].onclick({target:{}});await view.flush();};
 return {rows,calls,document,getId,find,click,tick,emit,open,delays,timers,flush:view.flush,dispose:view.dispose,holdNextRead:fn=>hold=fn};
}

test('the visible account page refreshes saved startup check results and timestamps without running an online checker',async()=>{
 const f=await fixture();await f.open();const remark=f.find(n=>n.id==='account-remark'),group=f.find(n=>n.id==='account-group_name');
 f.rows[0].login_check={state:'valid',checked_at:'2026-10-23T06:00:00Z'};await f.tick();
 assert.match(text(f.getId('rows')),/已登录.*最近检查.*2026/);assert.match(text(f.getId('detail')),/登录状态：已登录.*最近检查.*2026/);
 assert.equal(f.find(n=>n.id==='account-remark'),remark);assert.equal(f.find(n=>n.id==='account-group_name'),group);
 assert.equal(f.calls.some(c=>/session\.check|check_session|account\.check|capabilities/.test(c.op)),false);
 assert(f.delays.every(delay=>delay>=3000));f.dispose();
});

test('passive status updates keep selected accounts, filters and the current page',async()=>{
 const f=await fixture(Array.from({length:42},(_,i)=>base('a'+i)));
 f.getId('platform').value='channels';f.getId('group').value='门店';f.getId('platform').onchange();await f.flush();await f.click('下一页');
 const checkbox=f.find(n=>n.attrs['aria-label']==='选择账号 账号a20');checkbox.checked=true;checkbox.onchange();await f.open();
 f.rows[20].login_check={state:'login_required',checked_at:'2026-10-23T06:01:00Z'};f.rows[20].login_state='expired';await f.tick();
 assert.equal(f.find(n=>n.attrs['aria-label']==='选择账号 账号a20').checked,true);
 assert.equal(f.getId('platform').value,'channels');assert.equal(f.getId('group').value,'门店');assert.match(text(f.getId('rows').children[0]),/账号a20.*需要重新登录/);
 assert.equal(f.calls.filter(c=>c.op==='account.list').at(-1).p.offset,20);assert.match(text(f.getId('detail')),/账号a20.*需要重新登录/);f.dispose();
});

test('unsaved remarks and groups pause passive refresh without losing the editors',async()=>{
 const f=await fixture();await f.open();const remark=f.find(n=>n.id==='account-remark'),group=f.find(n=>n.id==='account-group_name');
 remark.value='正在填写的备注';remark.oninput();group.value='new';group.onchange();const newGroup=f.find(n=>n.id==='account-new-group');newGroup.value='新分组草稿';newGroup.oninput();
 const before=f.calls.length;f.rows[0].login_check={state:'login_required',checked_at:'2026-10-23T06:01:00Z'};await f.tick();
 assert.equal(f.calls.length,before);assert.equal(f.find(n=>n.id==='account-remark'),remark);assert.equal(remark.value,'正在填写的备注');assert.equal(newGroup.value,'新分组草稿');f.dispose();
});

test('a focused input retains focus and its value while saved status polling waits',async()=>{
 const f=await fixture();await f.open();const remark=f.find(n=>n.id==='account-remark');f.document.activeElement=remark;const before=f.calls.length;
 await f.tick();assert.equal(f.calls.length,before);assert.equal(f.document.activeElement,remark);assert.equal(remark.value,'原备注');
 f.document.activeElement=null;await f.tick();assert.equal(f.calls.filter(c=>c.op==='account.list').length,2);f.dispose();
});

test('an in-flight saved-status read cannot overwrite editing that began while it was waiting',async()=>{
 const f=await fixture();await f.open();let release;f.holdNextRead(result=>new Promise(resolve=>release=()=>resolve(result)));
 f.rows[0].login_check={state:'login_required',checked_at:'2026-10-23T06:01:00Z'};const pending=f.tick();await f.flush();assert.equal(typeof release,'function');
 const remark=f.find(n=>n.id==='account-remark');remark.value='读取过程中输入的备注';remark.oninput();release();await pending;
 assert.equal(f.find(n=>n.id==='account-remark'),remark);assert.equal(remark.value,'读取过程中输入的备注');assert.doesNotMatch(text(f.getId('detail')),/最近检查：需要重新登录/);f.dispose();
});

test('a hidden account page stops saved-status reads, resumes when visible, and disposal stops them permanently',async()=>{
 const f=await fixture();f.document.hidden=true;await f.emit('visibilitychange');const before=f.calls.length;await f.tick();assert.equal(f.calls.length,before);assert.equal(f.timers.size,0);
 f.document.hidden=false;await f.emit('visibilitychange');await f.tick();assert.equal(f.calls.filter(c=>c.op==='account.list').length,2);
 f.dispose();const after=f.calls.length;await f.tick();await f.emit('visibilitychange');assert.equal(f.calls.length,after);assert.equal(f.timers.size,0);
});

test('the selected account outside the current page still receives saved status without rebuilding its details',async()=>{
 const f=await fixture(Array.from({length:22},(_,i)=>base('a'+i)));await f.open();const remark=f.find(n=>n.id==='account-remark');await f.click('下一页');
 f.rows[0].login_check={state:'unavailable',checked_at:'2026-10-23T06:02:00Z'};await f.tick();assert.match(text(f.getId('detail')),/暂时无法确认.*2026/);assert.equal(f.find(n=>n.id==='account-remark'),remark);
 assert.equal(f.calls.filter(c=>c.op==='account.get').at(-1).p.id,'a0');assert.equal(f.calls.filter(c=>c.op==='account.list').at(-1).p.offset,20);f.dispose();
});

test('replacing the selected account during a passive read prevents that old result from changing the new details',async()=>{
 const f=await fixture([base('a'),base('b')]);await f.open();let release;f.holdNextRead(result=>new Promise(resolve=>release=()=>resolve(result)));
 f.rows[0].login_check={state:'login_required',checked_at:'2026-10-23T06:03:00Z'};const pending=f.tick();await f.flush();assert.equal(typeof release,'function');await f.open(1);release();await pending;
 assert.match(text(f.getId('detail')),/账号b/);assert.doesNotMatch(text(f.getId('detail')),/最近检查：需要重新登录/);f.dispose();
});

test('a new session with no saved observation removes the older check instead of displaying it as permanently valid',async()=>{
 const f=await fixture([{...base(),login_check:{state:'valid',checked_at:'2026-10-23T06:00:00Z'}}]);await f.open();f.rows[0].login_check=null;await f.tick();
 assert.match(text(f.getId('detail')),/登录状态：已保存登录.*最近检查：尚未检查/);assert.doesNotMatch(text(f.getId('detail')),/06:00:00|检查时已登录/);f.dispose();
});

test('a saved check that removes the last filtered page immediately loads the remaining page',async()=>{
 const f=await fixture(Array.from({length:22},(_,i)=>base('a'+i)));f.getId('state').value='ok';f.getId('state').onchange();await f.flush();await f.click('下一页');
 for(const row of f.rows.slice(20)){row.login_state='expired';row.login_check={state:'login_required',checked_at:'2026-10-23T06:04:00Z'};}
 await f.tick();assert.equal(f.calls.filter(c=>c.op==='account.list').at(-1).p.offset,0);assert.match(text(f.getId('rows').children[0]),/账号a0/);assert.equal(f.getId('state').value,'ok');f.dispose();
});
