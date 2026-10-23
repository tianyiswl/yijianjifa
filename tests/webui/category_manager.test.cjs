const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
test('category manager separates creation and row edits, blocks duplicates, and confirms deletion',async()=>{
 let rows=[{id:'a',name:'原分类',kind:'video'}],writes=[];
 const v=await mount((op,p)=>{
  if(op==='media.preference')return {keep_copy:false};if(op==='library.list')return {items:[],total:0};
  if(op==='category.list')return {items:rows,total:rows.length};
  writes.push([op,p]);if(op==='category.create'){rows.push({id:'b',name:p.name,kind:'video'});return rows.at(-1);}
  if(op==='category.rename'){rows=rows.map(r=>r.id===p.id?{...r,name:p.name}:r);return rows.find(r=>r.id===p.id);}
  if(op==='category.delete'){rows=rows.filter(r=>r.id!==p.id);return {id:p.id};}throw Error(op);
 },'library',undefined,{structuredClone,P011Router:{current:'library',go(){}}});
 v.query('[data-action="categories"]').onclick({isTrusted:true});await v.flush();
 async function clickMenu(label){const walk=n=>[n,...n.children.flatMap(walk)];walk(v.query('#overlay')).find(n=>n.tag==='button'&&n.textContent===label).onclick({isTrusted:true});await v.flush();}
 const input=v.find(n=>n.attrs['aria-label']==='新分类名称');input.value='原分类';await v.click('新增分类');assert.equal(writes.length,0);
 input.value='新分类';await v.click('新增分类');assert.equal(rows.length,2);assert.equal(input.value,'');
 const rowInput=v.find(n=>n.attrs['aria-label']==='分类名称 原分类');rowInput.value='改名分类';await v.click('保存');assert.equal(rows[0].name,'改名分类');
 await clickMenu('删除');assert.equal(rows.length,2);await v.click('取消');assert.equal(rows.length,2);
 await clickMenu('删除');await v.click('确认删除');assert.equal(rows.length,1);assert.equal(rows[0].name,'新分类');
 await v.click('完成');assert.equal(v.query('#overlay').hidden,true);v.dispose();
});
