const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

async function fixture(viewName,kind='video',empty=false){
 const row={id:'asset-1',name:'示例视频',display_name:'示例视频',title:'示例文案',voice:'口播',body:'',tags:'',full:'',category_ids:empty?[]:['a'],duration_ms:1000,origin_type:'external',missing:true};
 const categories=empty?[]:[{id:'a',name:'产品'},{id:'b',name:'场景'}],writes=[];
 const v=await mount(async(op,p)=>{
  if(op==='media.preference')return {keep_copy:false};
  if(op==='category.list')return {items:categories,total:categories.length};
  if(op==='library.list'||op==='finished.list')return {items:p.query?[]:[row],total:p.query?0:1};
  if(op==='category.create'){const category={id:'new',kind:p.kind,name:p.name};categories.push(category);writes.push({op,p});return category;}
  if(op==='library.categories'||op==='finished.categories'){writes.push({op,p});row.category_ids=p.category_ids;return {updated:1};}
  throw Error(op);
 },viewName,undefined,{structuredClone,P011Router:{current:viewName,go(){}}});
 await v.flush();
 if(viewName==='library'&&kind!=='video'){await v.click(kind==='music'?'音乐':'文案');await v.flush();}
 return {v,row,categories,writes};
}

for(const viewName of ['library','finished']){
 test(`${viewName}: row category click moves content and saves immediately without opening preview`,async()=>{
  const {v,row,writes}=await fixture(viewName);
  await v.find(n=>n.tag==='button'&&n.attrs['aria-label']==='设置分类 '+(viewName==='library'?'示例文案':'示例视频')).onclick({isTrusted:true});await v.flush();
  const first=v.find(n=>n.attrs['aria-label']==='分类 产品'),second=v.find(n=>n.attrs['aria-label']==='分类 场景');
  assert.equal(first.attrs['aria-pressed'],'true');assert.equal(second.attrs['aria-pressed'],'false');
  second.onclick({isTrusted:true});await v.flush();await v.flush();
  assert.deepEqual(row.category_ids,['b']);
  assert.deepEqual(writes,[{op:viewName==='library'?'library.categories':'finished.categories',p:{...(viewName==='library'?{kind:'video'}:{}),ids:['asset-1'],category_ids:['b'],mode:'replace'}}]);
  assert.equal(v.query('#detail-pane').querySelector('video'),null);
  v.dispose();
 });
 test(`${viewName}: cancelling inline classification leaves item unchanged`,async()=>{
  const {v,row,writes}=await fixture(viewName);
  await v.find(n=>n.tag==='button'&&n.attrs['aria-label']?.startsWith('设置分类 ')).onclick({isTrusted:true});await v.flush();
  await v.click('取消');assert.deepEqual(row.category_ids,['a']);assert.equal(writes.length,0);v.dispose();
 });
 test(`${viewName}: classification from a retained detail pane still preselects categories after filtering`,async()=>{
  const {v,row}=await fixture(viewName);
  if(viewName==='library'){await v.find(n=>n.tag==='tr'&&n.dataset.rowId==='asset-1').ondblclick({isTrusted:true});await v.flush();}
  else {await v.click('预览 / 编辑');await v.flush();}
  const search=v.query('#search');search.value='没有匹配';search.oninput({isTrusted:true,target:search});await v.tick();await v.flush();
  const detail=v.query('#detail-pane');
  if(viewName==='finished'){
   assert.equal(detail.querySelector('.category-choice'),null);
   search.value='';search.oninput({isTrusted:true,target:search});await v.tick();await v.flush();
  }
  const action=(viewName==='finished'?v.query('#tablearea'):detail).querySelector('.category-choice');assert.ok(action);
  action.onclick({isTrusted:true});await v.flush();
  assert.equal(v.find(n=>n.attrs['aria-label']==='分类 产品').attrs['aria-pressed'],'true');
  assert.equal(v.find(n=>n.attrs['aria-label']==='分类 场景').attrs['aria-pressed'],'false');
  await v.click('取消');assert.deepEqual(row.category_ids,['a']);v.dispose();
 });
}

for(const kind of ['video','copy','music','finished'])test(`create and assign first ${kind} category inside one dialog`,async()=>{
 const {v,row,writes}=await fixture(kind==='finished'?'finished':'library',kind,true);
 await v.find(n=>n.tag==='button'&&n.attrs['aria-label']?.startsWith('设置分类 ')).onclick({isTrusted:true});await v.flush();
 const name=v.find(n=>n.attrs['aria-label']==='新分类名称');name.value='新品';
 await v.click('新建并使用');await v.flush();await v.flush();
 assert.equal(v.query('#overlay').hidden,true);
 assert.deepEqual(row.category_ids,['new']);
 assert.deepEqual(writes.map(x=>x.op),['category.create',kind==='finished'?'finished.categories':'library.categories']);
 assert.deepEqual(writes[0].p,{kind,name:'新品'});v.dispose();
});

test('a later batch failure never lets a stale copy editor undo successful classification',async()=>{
 const rows=Array.from({length:101},(_,i)=>({id:'copy-'+i,title:'文案'+i,voice:'',body:'',tags:'',full:'',category_ids:['a']})),categories=[{id:'a',name:'原分类'},{id:'b',name:'新增分类'}];let savedCopy;
 const v=await mount(async(op,p)=>{
  if(op==='media.preference')return {keep_copy:false};
  if(op==='category.list')return {items:categories,total:2};
  if(op==='library.list')return {items:rows.slice(p.offset,p.offset+p.limit),total:101};
  if(op==='library.categories'){if(p.ids.includes('copy-100'))throw Error('第二批未完成');for(const row of rows.filter(r=>p.ids.includes(r.id)))row.category_ids=['b'];return {updated:p.ids.length};}
  if(op==='copy.save'){savedCopy=p;return {...rows[0],...p};}
  throw Error(op);
 },'library',undefined,{structuredClone,P011Router:{current:'library',go(){}}});
 await v.click('文案');await v.click('编辑');await v.click('全选');await v.click('设置分类');
 v.find(n=>n.attrs['aria-label']==='分类 新增分类').onclick({isTrusted:true});await v.flush();await v.flush();
 assert.deepEqual(rows[0].category_ids,['b']);assert.deepEqual(rows[100].category_ids,['a']);
 await v.click('取消');await v.click('保存文案');await v.flush();assert.deepEqual(savedCopy.category_ids,['b']);v.dispose();
});

for(const viewName of ['library','finished'])test(`${viewName}: saving metadata during classification never consumes its write result`,async()=>{
 let finish,metadataWrites=0;const row={id:'one',title:'需要保留的标题',name:'示例',display_name:'示例',voice:'',body:'',tags:'',full:'',missing:true,category_ids:[],duration_ms:1000};
 const v=await mount(async(op,p)=>{
  if(op==='media.preference')return {keep_copy:false};
  if(op==='category.list')return {items:[{id:'a',name:'产品'}],total:1};
  if(op==='library.list'||op==='finished.list')return {items:[row],total:1};
  if(op==='library.categories'||op==='finished.categories')return new Promise(resolve=>{finish=()=>{row.category_ids=['a'];resolve({updated:1});};});
  if(op==='copy.save'||op==='finished.save'){metadataWrites++;return {...row,...p};}
  throw Error(op);
 },viewName,undefined,{structuredClone,P011Router:{current:viewName,go(){}}});
 await v.flush();if(viewName==='library')await v.click('文案');
 await v.click(viewName==='library'?'编辑':'预览 / 编辑');
 await v.find(n=>n.tag==='button'&&n.attrs['aria-label']?.startsWith('设置分类 ')).onclick({isTrusted:true});await v.flush();
 v.find(n=>n.attrs['aria-label']==='分类 产品').onclick({isTrusted:true});await v.flush();assert.equal(typeof finish,'function');
 await v.click(viewName==='library'?'保存文案':'保存发布信息');assert.equal(metadataWrites,0);
 assert.doesNotMatch(v.query('#toast').textContent,/已保存到本机|发布信息已保存/);
 finish();await v.flush();await v.flush();
 assert.doesNotMatch(v.query('#toast').textContent,/已保存到本机|发布信息已保存/);
 await v.click(viewName==='library'?'保存文案':'保存发布信息');assert.equal(metadataWrites,1);v.dispose();
});

test('a failed category-manager read cannot clear the open copy classifications',async()=>{
 let listCalls=0,saved;const row={id:'copy-one',title:'保留原分类',voice:'',body:'',tags:'',full:'',category_ids:['a']};
 const v=await mount(async(op,p)=>{
  if(op==='media.preference')return {keep_copy:false};
  if(op==='library.list')return {items:[row],total:1};
  if(op==='category.list'){if(++listCalls===3)throw Error('读取暂时失败');return {items:[{id:'a',name:'产品'},{id:'b',name:'场景'}],total:2};}
  if(op==='copy.save'){saved=p;return {...row,...p};}
  throw Error(op);
 },'library',undefined,{structuredClone,P011Router:{current:'library',go(){}}});
 await v.click('文案');await v.click('编辑');
 v.query('[data-action="categories"]').onclick({isTrusted:true});await v.flush();
 v.find(n=>n.tag==='p'&&n.textContent==='读取暂时失败');await v.click('完成');
 await v.click('保存文案');assert.deepEqual(saved.category_ids,['a']);v.dispose();
});

test('a new copy keeps one chosen category with its unsaved text until the copy is saved',async()=>{
 let saved;
 const v=await mount(async(op,p)=>{
  if(op==='media.preference')return {keep_copy:false};
  if(op==='library.list')return {items:[],total:0};
  if(op==='category.list')return {items:[{id:'a',name:'产品'}],total:1};
  if(op==='copy.save'){saved=p;return {...p,id:'created'};}
  throw Error(op);
 },'library',undefined,{structuredClone,P011Router:{current:'library',go(){}}});
 await v.click('文案');await v.click('+ 手动填写');
 const title=v.find(n=>n.id==='detail-copy-title');title.value='新文案';title.oninput({isTrusted:true});
 v.query('#detail-pane').querySelector('.category-choice').onclick({isTrusted:true});await v.flush();
 v.find(n=>n.attrs['aria-label']==='分类 产品').onclick({isTrusted:true});await v.flush();await v.flush();
 assert.equal(saved,undefined);assert.equal(v.find(n=>n.id==='detail-copy-title').value,'新文案');
 await v.click('保存文案');assert.equal(saved.title,'新文案');assert.deepEqual(saved.category_ids,['a']);v.dispose();
});

test('Excel import chooses only one category even when the list filters several categories',async()=>{
 let imported;
 const v=await mount(async(op,p)=>{
  if(op==='media.preference')return {keep_copy:false};
  if(op==='library.list')return {items:[],total:0};
  if(op==='category.list')return {items:[{id:'a',name:'产品'},{id:'b',name:'场景'}],total:2};
  if(op==='excel.preview')return {token:'preview',new_count:1,duplicate_count:0,update_count:0,updates:[],errors:[]};
  if(op==='excel.apply'){imported=p;return {new_count:1,updated_count:0,duplicate_count:0,error_count:0};}
  throw Error(op);
 },'library',undefined,{structuredClone,P011Router:{current:'library',go(){}}});
 await v.click('文案');v.query('#filterbutton').onclick({isTrusted:true});await v.flush();
 for(const check of v.query('#overlay').querySelectorAll('input').slice(1)){check.checked=true;check.onchange({isTrusted:true});}
 await v.click('应用');await v.click('导入表格');await v.click('选择表格导入');
 const choice=v.find(n=>n.attrs['aria-label']==='导入文案分类');assert.equal(choice.value,'');choice.value='b';choice.onchange();
 await v.click('确认导入');assert.deepEqual(imported.category_ids,['b']);v.dispose();
});

for(const viewName of ['library','finished'])test(`${viewName}: metadata saving blocks classification until its own write completes`,async()=>{
 let finish,categoryWrites=0;const row={id:'one',title:'标题',name:'示例',display_name:'示例',voice:'',body:'',tags:'',full:'',missing:true,category_ids:['a'],duration_ms:1000};
 const v=await mount(async(op,p)=>{
  if(op==='media.preference')return {keep_copy:false};
  if(op==='category.list')return {items:[{id:'a',name:'产品'},{id:'b',name:'场景'}],total:2};
  if(op==='library.list'||op==='finished.list')return {items:[row],total:1};
  if(op==='copy.save'||op==='finished.save')return new Promise(resolve=>{finish=()=>resolve({...row,...p});});
  if(op==='library.categories'||op==='finished.categories'){categoryWrites++;row.category_ids=p.category_ids;return {updated:1};}
  throw Error(op);
 },viewName,undefined,{structuredClone,P011Router:{current:viewName,go(){}}});
 await v.flush();if(viewName==='library')await v.click('文案');await v.click(viewName==='library'?'编辑':'预览 / 编辑');
 await v.click(viewName==='library'?'保存文案':'保存发布信息');assert.equal(typeof finish,'function');
 v.find(n=>n.tag==='button'&&n.attrs['aria-label']?.startsWith('设置分类 ')).onclick({isTrusted:true});await v.flush();
 assert.equal(v.query('#overlay').children.length,0);assert.equal(categoryWrites,0);
 finish();await v.flush();await v.flush();
 v.find(n=>n.tag==='button'&&n.attrs['aria-label']?.startsWith('设置分类 ')).onclick({isTrusted:true});await v.flush();
 v.find(n=>n.attrs['aria-label']==='分类 场景').onclick({isTrusted:true});await v.flush();await v.flush();
 assert.equal(categoryWrites,1);assert.deepEqual(row.category_ids,['b']);v.dispose();
});
