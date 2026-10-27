const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {Node}=require('./support/commerce_view.cjs');
function create(){const handlers=new Map(),document={addEventListener(kind,fn){const set=handlers.get(kind)||new Set();set.add(fn);handlers.set(kind,set);},removeEventListener(kind,fn){handlers.get(kind)?.delete(fn);},emit(kind,e){for(const fn of handlers.get(kind)||[])fn(e);}},context={document};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../src/yijianjifa/webui/assets/account_picker.js'),'utf8'),context);const el=(tag,text,cls)=>{const n=new Node(tag);if(text!==undefined)n.textContent=text;n.className=cls||'';return n;},trigger=el('button'),panel=el('div'),changes=[];let locked=false,selected=['a'],before=0;const accounts=[{id:'a',platform:'douyin',nickname:'<b>主号</b>',login_state:'valid'},{id:'b',platform:'douyin',nickname:'副号',login_state:'expired'}],update=()=>picker.update({accounts,selectedIds:selected,platformLabels:{douyin:'抖音'}}),picker=context.P011AccountPicker.create({el,trigger,panel,onToggle(id,checked){changes.push([id,checked]);selected=selected.filter(x=>x!==id);if(checked)selected.push(id);update();},beforeOpen(){before++;},isLocked:()=>locked});update();return {trigger,panel,picker,document,handlers,changes,update,get before(){return before;},lock(value){locked=value;update();},select(ids){selected=ids;update();},row(id){return panel.querySelectorAll('input').find(n=>n.dataset.account===id);}};}
test('account picker preserves multi-selection, open state and scroll while only toggling the chosen ID',async()=>{const v=create();v.trigger.onclick({isTrusted:true});v.panel.scrollTop=140;const b=v.row('b');b.checked=true;await b.onchange({isTrusted:true});assert.deepEqual(v.changes,[['b',true]]);assert.equal(v.row('a').checked,true);assert.equal(v.panel.classList.contains('hide'),false);assert.equal(v.panel.scrollTop,140);assert.equal(v.trigger.textContent,'已选 2 个账号');assert.equal(v.before,1);v.picker.dispose();});
test('Escape and outside clicks close the picker while row clicks leave it open',()=>{const v=create();v.trigger.onclick({isTrusted:true});v.document.emit('click',{target:v.row('b')});assert.equal(v.panel.classList.contains('hide'),false);let focused=false;v.trigger.focus=()=>focused=true;v.document.emit('keydown',{key:'Escape'});assert.equal(v.trigger.attrs['aria-expanded'],'false');assert.equal(focused,true);v.trigger.onclick({isTrusted:true});v.document.emit('click',{target:new Node('div')});assert.equal(v.panel.classList.contains('hide'),true);v.picker.dispose();});
test('locked and synthetic account changes cannot mutate selection or authorize an operation',async()=>{const v=create();v.trigger.onclick({isTrusted:false});assert.equal(v.panel.classList.contains('hide'),true);const b=v.row('b');b.checked=true;await b.onchange({isTrusted:false});assert.equal(b.checked,false);v.lock(true);v.trigger.onclick({isTrusted:true});assert.equal(v.trigger.disabled,true);assert.equal(v.panel.classList.contains('hide'),true);const a=v.row('a');a.checked=false;await a.onchange({isTrusted:true});assert.equal(a.checked,true);assert.deepEqual(v.changes,[]);v.picker.dispose();});
test('selected missing accounts remain actionable and account names are text rather than markup',async()=>{const v=create();assert.equal(v.panel.querySelectorAll('b').length,0);assert.equal(v.trigger.textContent,'抖音 · <b>主号</b>');v.select(['gone']);const removed=v.row('gone');assert.equal(removed.checked,true);removed.checked=false;await removed.onchange({isTrusted:true});assert.deepEqual(v.changes,[['gone',false]]);assert.equal(v.row('a').checked,false);v.picker.dispose();});
test('disposing the picker removes both document handlers and ignores detached rows',async()=>{const v=create(),row=v.row('b');assert.equal(v.handlers.get('click').size,1);assert.equal(v.handlers.get('keydown').size,1);v.picker.dispose();v.picker.dispose();assert.equal(v.handlers.get('click').size,0);assert.equal(v.handlers.get('keydown').size,0);assert.equal(v.trigger.onclick,null);row.checked=true;await row.onchange({isTrusted:true});assert.deepEqual(v.changes,[]);});


test('全选支持半选、全选和取消全选，包含需要登录的现有账号',async()=>{
 const v=create(),all=()=>v.panel.querySelectorAll('input').find(n=>n.dataset.accountAll==='true');
 assert.ok(all());assert.equal(all().indeterminate,true);
 all().checked=true;await all().onchange({isTrusted:true});
 assert.equal(v.row('a').checked,true);assert.equal(v.row('b').checked,true);assert.equal(all().checked,true);
 all().checked=false;await all().onchange({isTrusted:true});
 assert.equal(v.row('a').checked,false);assert.equal(v.row('b').checked,false);assert.equal(all().indeterminate,false);v.picker.dispose();
});

test('全选不会接受合成操作，锁定时也不能修改账号',async()=>{
 const v=create(),all=()=>v.panel.querySelectorAll('input').find(n=>n.dataset.accountAll==='true');
 assert.ok(all());all().checked=true;await all().onchange({isTrusted:false});assert.deepEqual(v.changes,[]);
 v.lock(true);assert.equal(all().disabled,true);all().checked=true;await all().onchange({isTrusted:true});assert.deepEqual(v.changes,[]);v.picker.dispose();
});


test('取消全选能移除保留的失效账号，空账号列表不会凭空新增选择',async()=>{
 const v=create();v.select(['gone']);const all=()=>v.panel.querySelectorAll('input').find(n=>n.dataset.accountAll==='true');
 all().checked=false;await all().onchange({isTrusted:true});assert.equal(v.row('gone'),undefined);assert.equal(v.row('a').checked,false);
 v.picker.update({accounts:[],selectedIds:[],platformLabels:{}});assert.equal(all().disabled,true);v.picker.dispose();
});
