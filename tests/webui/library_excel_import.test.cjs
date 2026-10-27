const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

async function fixture(extra={}){
 const calls=[];
 const v=await mount(async(op,p)=>{
  calls.push({op,p});
  if(extra[op])return extra[op](p);
  if(op==='media.preference')return {keep_copy:false};
  if(op==='library.list')return {items:[],total:0};
  if(op==='category.list')return {items:[],total:0};
  if(op==='excel.preview')return {token:'preview',new_count:1,duplicate_count:0,update_count:0,updates:[],errors:[]};
  if(op==='excel.apply')return {new_count:1,updated_count:0,duplicate_count:0,error_count:0};
  throw Error(op);
 },'library',undefined,{structuredClone,P011Router:{current:'library',go(){}}});
 await v.click('文案');return {v,calls};
}

test('an existing spreadsheet can be imported directly without downloading a template',async()=>{
 const {v,calls}=await fixture();
 await v.click('导入表格');
 assert.equal(calls.filter(x=>x.op.startsWith('excel.')).length,0);
 await v.click('选择表格导入');
 assert.deepEqual(calls.filter(x=>x.op.startsWith('excel.')).map(x=>x.op),['excel.preview']);
 await v.click('确认导入');
 assert.equal(calls.filter(x=>x.op==='excel.apply').length,1);v.dispose();
});

test('downloading a template leaves the import dialog ready to choose a spreadsheet',async()=>{
 const {v,calls}=await fixture({'excel.template':()=>({saved:true,name:'文案模板.xlsx',token:'saved'})});
 await v.click('导入表格');await v.click('下载模板');
 assert.match(v.find(n=>n.attrs['aria-live']==='polite').textContent,/已保存：文案模板.xlsx/);
 assert.equal(v.query('#overlay').hidden,false);
 assert.equal(calls.some(x=>x.op==='excel.preview'),false);
 await v.click('选择表格导入');await v.click('取消');
 assert.equal(calls.some(x=>x.op==='excel.apply'),false);v.dispose();
});

test('template download blocks overlapping chooser actions until the save finishes',async()=>{
 let finish;
 const {v}=await fixture({'excel.template':()=>new Promise(resolve=>{finish=resolve;})});
 await v.click('导入表格');await v.click('下载模板');
 for(const label of ['下载模板','选择表格导入','取消'])assert.equal(v.find(n=>n.tag==='button'&&n.textContent===label).disabled,true);
 finish({saved:true,name:'模板.xlsx'});await v.flush();
 for(const label of ['下载模板','选择表格导入','取消'])assert.equal(v.find(n=>n.tag==='button'&&n.textContent===label).disabled,false);
 await v.click('取消');v.dispose();
});

for(const fail of [false,true])test(`a ${fail?'failed':'cancelled'} template save keeps direct import available`,async()=>{
 const {v,calls}=await fixture({'excel.template':()=>{if(fail)throw Error('保存位置不可写');return {cancelled:true,saved:false};}});
 await v.click('导入表格');await v.click('下载模板');
 const status=v.find(n=>n.attrs['aria-live']==='polite').textContent;
 assert.doesNotMatch(status,/已保存/);if(fail)assert.match(status,/保存位置不可写/);
 await v.click('选择表格导入');assert.equal(calls.some(x=>x.op==='excel.preview'),true);
 await v.click('取消');v.dispose();
});

test('unsaved copy edits are protected before opening the import chooser',async()=>{
 const {v,calls}=await fixture();
 await v.click('+ 手动填写');const input=v.find(n=>n.id==='detail-copy-title');input.value='还未保存';input.oninput({isTrusted:true});
 await v.click('导入表格');await v.click('继续编辑');
 assert.equal(v.find(n=>n.id==='detail-copy-title').value,'还未保存');
 assert.equal(calls.some(x=>x.op.startsWith('excel.')),false);v.dispose();
});

test('cancelling the native spreadsheet chooser allows a later import attempt',async()=>{
 const {v,calls}=await fixture({'excel.preview':()=>({cancelled:true})});
 await v.click('导入表格');await v.click('选择表格导入');
 await v.click('导入表格');await v.click('选择表格导入');
 assert.equal(calls.filter(x=>x.op==='excel.preview').length,2);v.dispose();
});
