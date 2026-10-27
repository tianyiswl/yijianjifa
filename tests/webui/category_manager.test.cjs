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
 assert.ok(v.find(n=>/门店实拍/.test(n.textContent)));
 async function clickMenu(label){const walk=n=>[n,...n.children.flatMap(walk)];walk(v.query('#overlay')).find(n=>n.tag==='button'&&n.textContent===label).onclick({isTrusted:true});await v.flush();}
 const input=v.find(n=>n.attrs['aria-label']==='新分类名称');input.value='原分类';await v.click('新增分类');assert.equal(writes.length,0);
 input.value='新分类';await v.click('新增分类');assert.equal(rows.length,2);assert.equal(input.value,'');assert.ok(v.find(n=>/新建.*列表.*设置分类/.test(n.textContent)));
 const rowInput=v.find(n=>n.attrs['aria-label']==='分类名称 原分类');rowInput.value='改名分类';await v.click('保存');assert.equal(rows[0].name,'改名分类');
 await clickMenu('删除');assert.equal(rows.length,2);await v.click('取消');assert.equal(rows.length,2);
 await clickMenu('删除');await v.click('确认删除');assert.equal(rows.length,1);assert.equal(rows[0].name,'新分类');
 await v.click('完成');assert.equal(v.query('#overlay').hidden,true);v.dispose();
});
test('manager marks snapshots unreliable after an initial read or a readback failure',async()=>{
 for(const failure of ['initial','readback','none']){
  const v=await mount(()=>{},'globals',[]);let calls=0;const rows=[{id:'a',name:'原分类',kind:'video'}];
  const result=v.runtime.P011CategoryManager({overlay:v.query('#overlay'),kind:'video',label:'视频素材',api:async()=>{calls++;if(failure==='initial'||failure==='readback'&&calls>1)throw Error('分类暂时无法读取');return {items:rows,total:rows.length};},write:async()=>({id:'a',name:'新名称',kind:'video'})});
  await v.flush();
  if(failure==='readback'){v.find(n=>n.attrs['aria-label']==='分类名称 原分类').value='新名称';await v.click('保存');}
  await v.click('完成');const closed=JSON.parse(JSON.stringify(await result));assert.equal(closed.loaded,failure==='none');assert.equal(closed.changed,failure==='readback');assert.equal(closed.rows.length,failure==='initial'?0:1);v.dispose();
 }
});
test('assignment dialog exposes a modal and focuses the next useful control',async()=>{
 for(const categories of [[],[{id:'a',name:'新品'}]]){
  const v=await mount(()=>{},'globals',[]),create=v.runtime.document.createElement;let focused;
  v.runtime.document.createElement=tag=>{const node=create(tag);node.focus=()=>{focused=node;};return node;};
  const result=v.runtime.P011BatchCategories({overlay:v.query('#overlay'),categories,count:1,single:true,createCategory:async name=>({id:'b',name}),assignCategory:async()=>{}});
  assert.equal(v.find(n=>n.tag==='section'&&n.attrs.role==='dialog').attrs['aria-modal'],'true');assert.equal(focused.attrs['aria-label'],categories.length?'分类 新品':'新分类名称');
  assert.ok(v.find(n=>/点击.*立即保存/.test(n.textContent)));await v.click('取消');assert.equal(await result,null);v.dispose();
 }
});
