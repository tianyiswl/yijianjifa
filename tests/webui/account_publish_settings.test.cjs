const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {Node}=require('./support/commerce_view.cjs');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function fixture({platform='channels',saved={},fail=false}={}){
 const host=new Node('div'),modal=new Node('div'),dialog=new Node('div'),calls=[],writes=[];modal.append(dialog);modal.style={display:'none'};
 const el=(tag,text,cls)=>{const n=new Node(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 const button=(text,fn)=>{const b=el('button',text);b.onclick=()=>Promise.resolve(fn());return b;};
 const api=async(op,p)=>{calls.push({op,p});if(op==='account.publish_settings.read')return {account_id:p.account_id,platform,fields:{content_declaration_choice:'none'},schema:platform==='douyin'?{}:{content_declaration_choice:{}},douyin:{declaration_id:null,location:null},...saved};if(op==='publish.declarations.query')return {available:true,items:[{id:'ai',label:'AI生成内容'}]};if(op==='publish.locations.search')return {available:true,token:'a'.repeat(48),items:[{id:'shop',name:'测试店',address:'测试地址'}]};throw Error(op);};
 const sandbox={globalThis:null,Option:function(label,value){const n=el('option',label);n.value=value;return n;},P011MultiPlatformFields:{supported:s=>!!s.content_declaration_choice,create:h=>({materialize(){h.values.content_declaration_choice='ai';},dispose(){}})}};sandbox.globalThis=sandbox;
 const file=path.join(__dirname,'../../src/yijianjifa/webui/assets/account_publish_settings.js');
 vm.runInNewContext(fs.readFileSync(file,'utf8'),sandbox);
 let active=true;const settings=sandbox.P011AccountPublishSettings.create({el,button,host,modal,dialog,account:{id:'account-a',platform,nickname:'测试号'},api,intent:{run:async(op,p)=>{writes.push({op,p});if(fail)throw Error('未能保存');return {saved:true};}},isAlive:()=>active,notice:()=>{}});
 const walk=n=>[n,...n.children.flatMap(walk)],find=fn=>{const n=[...walk(host),...walk(dialog)].find(fn);assert(n,'node missing');return n;},click=async text=>{await find(n=>n.tag==='button'&&n.textContent===text).onclick();await flush();};
 return {host,modal,dialog,calls,writes,settings,find,click,flush,dispose(){active=false;settings.dispose();}};
}
test('account settings save platform fields once for the exact account and close after success',async()=>{
 const f=fixture();await f.click('发布设置');assert.equal(f.modal.style.display,'grid');await f.click('保存');
 assert.equal(f.writes.length,1);assert.equal(f.writes[0].op,'account.publish_settings.save');assert.equal(f.writes[0].p.account_id,'account-a');assert.equal(f.writes[0].p.fields.content_declaration_choice,'ai');assert.equal(f.modal.style.display,'none');f.dispose();
});
test('failed save keeps the editor and draft open for retry',async()=>{
 const f=fixture({fail:true});await f.click('发布设置');await f.click('保存');assert.equal(f.modal.style.display,'grid');assert.match(f.dialog.children.map(n=>n.textContent).join(' '),/未能保存/);assert.equal(f.settings.isOpen,true);f.dispose();
});
test('douyin declaration and searched location are saved using trusted references only',async()=>{
 const f=fixture({platform:'douyin'});await f.click('发布设置');const choice=f.find(n=>n.attrs['aria-label']==='默认内容声明');choice.value='ai';choice.onchange();
 const query=f.find(n=>n.attrs['aria-label']==='默认发布地点搜索');query.value='测试店';await f.click('搜索地点');const result=f.find(n=>n.attrs['aria-label']==='默认发布地点');result.value='shop';result.onchange();await f.click('保存');
 assert.equal(f.writes[0].p.douyin.declaration_id,'ai');assert.equal(f.writes[0].p.douyin.location.id,'shop');assert.equal(f.writes[0].p.douyin.location.token,'a'.repeat(48));f.dispose();
});
test('closing or disposing a loading editor cannot display results for a different account',async()=>{
 const f=fixture();const opening=f.click('发布设置');f.dispose();await opening;assert.equal(f.modal.style.display,'none');assert.equal(f.writes.length,0);
});
test('invalid short title is corrected in account settings before any save can claim success',async()=>{
 const f=fixture({saved:{fields:{channels_short_title:'三字短'}}});await f.click('发布设置');await f.click('保存');assert.equal(f.writes.length,0);assert.match(f.dialog.children.map(n=>n.textContent).join(' '),/6.*16/);assert.equal(f.modal.style.display,'grid');f.dispose();
});
test('reprint defaults cannot save without a source or with a self-made authorization',async()=>{
 for(const fields of [{creation_statement_choice:'reprint',reprint_source:''},{creation_statement_id:-2,reprint_source:'来源',content_authorization:true}]){const f=fixture({platform:'bilibili',saved:{fields}});await f.click('发布设置');await f.click('保存');assert.equal(f.writes.length,0);assert.match(f.dialog.children.map(n=>n.textContent).join(' '),/转载/);f.dispose();}
});
