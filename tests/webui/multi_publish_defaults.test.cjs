// Account-management UI over isolated file-backed defaults; no platform I/O.
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {mount}=require('./support/commerce_view.cjs');
const platforms=['bilibili','channels','kuaishou'],labels={bilibili:'B站',channels:'视频号',kuaishou:'快手'};
const choices=[{choice:'none',name:'不添加声明'},{choice:'ai_generated',name:'包含 AI 内容'}];
const schemas={bilibili:{category_choice:{choices_source:'builtin',default:'human:knowledge',choices:[{choice:'human:knowledge',name:'知识',mode:'human'},{choice:'human:tech',name:'科技数码',mode:'human'}]},creation_statement_choice:{choices_source:'builtin',default:'no_mark',choices:[{choice:'no_mark',name:'不添加声明'},{choice:'ai_generated',name:'包含 AI 内容'}]},content_authorization:{type:'boolean'},reprint_source:{max_length:200}},channels:{content_declaration_choice:{choices},channels_original:{type:'boolean'},channels_short_title:{max_length:16}},kuaishou:{content_declaration_choice:{choices}}};
function fixture(){
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'p011-account-defaults-ui-')),file=path.join(directory,'defaults.json'),calls=[];fs.writeFileSync(file,'{}');let failSave=false;
 const rows=[...platforms.map(platform=>({id:platform+'-account',platform,nickname:'离线'+labels[platform],remark:'',group_name:'',capabilities:{},login_state:'valid'})),{id:'channels-second',platform:'channels',nickname:'视频号二号',remark:'',group_name:'',capabilities:{},login_state:'valid'}];
 const read=()=>JSON.parse(fs.readFileSync(file,'utf8'));
 const handler=async(op,p)=>{
  calls.push({op,p:structuredClone(p)});
  if(op==='account.login.availability')return {available:false};
  if(op==='multi_publish.catalog')return {items:platforms.map(platform=>({platform,label:labels[platform],field_schema:schemas[platform]}))};
  if(op==='account.list')return {items:structuredClone(rows),total:rows.length,groups:[]};
  if(op==='account.get')return structuredClone(rows.find(r=>r.id===p.id));
  if(op==='account.publish_settings.read'){const account=rows.find(r=>r.id===p.account_id);return {account_id:account.id,platform:account.platform,fields:read()[account.id]||{},schema:schemas[account.platform],douyin:{declaration_id:null,location:null}};}
  if(op==='account.publish_settings.save'){if(failSave)throw Error('离线保存失败');const saved=read();saved[p.account_id]=p.fields;fs.writeFileSync(file,JSON.stringify(saved));return {saved:true};}
  throw Error('Unexpected operation '+op);
 };
 return {calls,read,handler,set failSave(v){failSave=v;},dispose(){fs.rmSync(directory,{recursive:true,force:true});}};
}
const find=(v,label)=>v.find(n=>n.attrs['aria-label']===label);
async function open(v,platform,index=platforms.indexOf(platform)){v.getId('rows').children[index].onclick({target:{}});await v.flush();await v.click('发布设置','发布设置 '+(index===3?'channels-second':platform+'-account'));}
function setValues(v,platform){
 if(platform==='bilibili'){const category=find(v,'默认分区');category.value='human:tech';category.onchange();const statement=find(v,'默认创作声明');statement.value='ai_generated';statement.onchange();return {category_choice:'human:tech',creation_statement_choice:'ai_generated',reprint_source:'',content_authorization:false};}
 const statement=find(v,'默认内容声明');statement.value='ai_generated';statement.onchange();
 if(platform==='channels'){const short=find(v,'默认短标题（选填）');short.value='离线默认短标题';short.oninput();return {content_declaration_choice:'ai_generated',channels_short_title:'离线默认短标题',channels_original:false};}
 return {content_declaration_choice:'ai_generated'};
}
function assertValues(v,platform){if(platform==='bilibili'){assert.equal(find(v,'默认分区').value,'human:tech');assert.equal(find(v,'默认创作声明').value,'ai_generated');assert.equal(find(v,'默认内容授权').checked,false);}else{assert.equal(find(v,'默认内容声明').value,'ai_generated');if(platform==='channels'){assert.equal(find(v,'默认短标题（选填）').value,'离线默认短标题');assert.equal(find(v,'默认声明原创').checked,false);}}}
function assertLocalOnly(calls){assert.equal(calls.some(c=>/platform_options|login.start|upload|prepare|confirm|execution|finished.save/.test(c.op)),false);}
for(const platform of platforms)test(labels[platform]+' defaults save in account management and survive reopening without creating a task',async()=>{
 const f=fixture();let v;try{v=await mount(f.handler,'accounts');await open(v,platform);const expected=setValues(v,platform);await v.click('保存');assert.deepEqual(f.read()[platform+'-account'],expected);assert.equal(v.getId('modal').style.display,'none');v.dispose();v=await mount(f.handler,'accounts');await open(v,platform);assertValues(v,platform);await v.click('取消');assertLocalOnly(f.calls);}finally{v?.dispose();f.dispose();}
});
test('two accounts on the same platform retain independent defaults',async()=>{
 const f=fixture();let v;try{v=await mount(f.handler,'accounts');await open(v,'channels');setValues(v,'channels');await v.click('保存');await open(v,'channels',3);assert.equal(find(v,'默认内容声明').value,'none');assert.equal(find(v,'默认声明原创').checked,false);await v.click('保存');assert.equal(f.read()['channels-account'].content_declaration_choice,'ai_generated');assert.equal(f.read()['channels-second'].content_declaration_choice,'none');assertLocalOnly(f.calls);}finally{v?.dispose();f.dispose();}
});
test('cancelling an edit does not write and failed save preserves the editor for retry',async()=>{
 const f=fixture();let v;try{v=await mount(f.handler,'accounts');await open(v,'channels');setValues(v,'channels');await v.click('取消');assert.deepEqual(f.read(),{});await open(v,'channels');setValues(v,'channels');f.failSave=true;await v.click('保存');assert.equal(v.getId('modal').style.display,'grid');assert.ok(v.find(n=>n.textContent==='保存失败：离线保存失败'));assert.deepEqual(f.read(),{});f.failSave=false;await v.click('保存');assert.equal(f.read()['channels-account'].content_declaration_choice,'ai_generated');assertLocalOnly(f.calls);}finally{v?.dispose();f.dispose();}
});
test('cancel after failed save cannot leak abandoned fields into another account',async()=>{
 const f=fixture();let v;try{v=await mount(f.handler,'accounts');await open(v,'channels');setValues(v,'channels');f.failSave=true;await v.click('保存');await v.click('取消');f.failSave=false;await open(v,'channels',3);await v.click('保存');assert.equal(f.read()['channels-account'],undefined);assert.equal(f.read()['channels-second'].content_declaration_choice,'none');assertLocalOnly(f.calls);}finally{v?.dispose();f.dispose();}
});
