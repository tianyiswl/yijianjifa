const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function mount(categories,options={}){
 class N{constructor(tag,text){this.tag=tag;this.children=[];this.attrs={};this.textContent=text;this.value='';}append(...n){this.children.push(...n);}replaceChildren(...n){this.children=n;}setAttribute(k,v){this.attrs[k]=v;}focus(){}}
 const overlay=new N('overlay'),listeners=new Map(),context={document:{createElement:t=>new N(t),addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('src/yijianjifa/webui/assets/category_manager.js','utf8'),context);
 const result=context.P011BatchCategories({overlay,categories,count:12,assignCategory:async()=>{},...options}),walk=n=>[n,...n.children.flatMap(walk)],find=f=>walk(overlay).find(f),button=text=>find(n=>n.tag==='button'&&n.textContent===text),category=name=>find(n=>n.tag==='button'&&n.attrs['aria-label']===(name?'分类 '+name:'设为未分类'));
 return {result,overlay,find,button,category,key:key=>listeners.get('keydown')?.({key,preventDefault(){}}),click:async text=>{await button(text).onclick({isTrusted:true});},choose:async name=>{await category(name).onclick({isTrusted:true});}};
}
const plain=value=>JSON.parse(JSON.stringify(value));
test('single category choices show one current category without modes or a second save step',async()=>{
 const v=mount([{id:'a',name:'分类A'},{id:'b',name:'分类B'}],{single:true,itemName:'测试视频',initialIds:['a','b']});
 assert.equal(v.find(n=>n.tag==='h2').textContent,'设置分类');assert.equal(v.category('分类A').attrs['aria-pressed'],'true');assert.equal(v.category('分类B').attrs['aria-pressed'],'false');assert.equal(v.category(null).attrs['aria-pressed'],'false');
 assert.equal(v.find(n=>n.tag==='input'&&n.type==='checkbox'),undefined);assert.equal(v.find(n=>n.tag==='select'||n.tag==='details'),undefined);assert.equal(v.button('保存分类'),undefined);await v.click('取消');assert.equal(await v.result,null);
});
test('clicking a category waits for assignment and closes with exactly that category',async()=>{
 let finish;const writes=[],v=mount([{id:'a',name:'分类A'}],{assignCategory:id=>{writes.push(id);return new Promise(resolve=>{finish=resolve;});}});
 const pending=v.choose('分类A');assert.deepEqual(writes,['a']);assert.equal(v.overlay.hidden,false);assert.equal(v.button('取消').disabled,true);assert.equal(v.category('分类A').disabled,true);v.key('Escape');await v.click('取消');assert.equal(v.overlay.hidden,false);
 finish();await pending;assert.equal(v.overlay.hidden,true);assert.deepEqual(plain(await v.result),{mode:'replace',category_ids:['a']});
});
test('unclassified applies null immediately, while cancel and Escape never assign',async()=>{
 const writes=[],v=mount([{id:'a',name:'分类A'}],{single:true,initialIds:['a'],assignCategory:async id=>writes.push(id)});await v.choose(null);assert.deepEqual(writes,[null]);assert.deepEqual(plain(await v.result),{mode:'replace',category_ids:[]});
 const c=mount([],{assignCategory:async id=>writes.push(id)});await c.click('取消');assert.equal(await c.result,null);const escape=mount([],{assignCategory:async id=>writes.push(id)});escape.key('Escape');assert.equal(await escape.result,null);assert.deepEqual(writes,[null]);
});
test('assignment failure leaves the selector open and can be retried',async()=>{
 let attempts=0;const v=mount([{id:'a',name:'分类A'}],{assignCategory:async()=>{if(++attempts===1)throw Error('暂时无法保存');}});await v.choose('分类A');assert.equal(v.overlay.hidden,false);assert.equal(v.button('取消').disabled,false);assert.ok(v.find(n=>/暂时无法保存/.test(n.textContent||'')));await v.choose('分类A');assert.equal(attempts,2);assert.deepEqual(plain(await v.result),{mode:'replace',category_ids:['a']});
});
test('new category is created and assigned in one action',async()=>{
 const writes=[],v=mount([],{createCategory:async name=>{writes.push(['create',name]);return {id:'new',name};},assignCategory:async id=>writes.push(['assign',id])}),input=v.find(n=>n.attrs['aria-label']==='新分类名称');input.value='  门店实拍  ';await v.click('新建并使用');assert.deepEqual(writes,[['create','门店实拍'],['assign','new']]);assert.deepEqual(plain(await v.result),{mode:'replace',category_ids:['new']});
});
test('new category remains available after assignment fails and duplicate input never recreates it',async()=>{
 let creates=0,assigns=0;const v=mount([],{createCategory:async name=>{creates++;return {id:'new',name};},assignCategory:async()=>{if(++assigns<3)throw Error('归类暂时失败');}}),input=v.find(n=>n.attrs['aria-label']==='新分类名称');input.value='新品';await v.click('新建并使用');assert.equal(v.overlay.hidden,false);assert.ok(v.category('新品'));await v.choose('新品');input.value='新品';await v.click('新建并使用');assert.equal(creates,1);assert.equal(assigns,3);assert.deepEqual(plain(await v.result),{mode:'replace',category_ids:['new']});
});
test('creation validation and failure preserve the name; in-flight creation locks close and Escape',async()=>{
 let rejectCreate,calls=0;const v=mount([],{createCategory:()=>{calls++;return new Promise((resolve,reject)=>{rejectCreate=reject;});}}),input=v.find(n=>n.attrs['aria-label']==='新分类名称');
 input.value=' ';await v.click('新建并使用');input.value='长'.repeat(129);await v.click('新建并使用');assert.equal(calls,0);assert.ok(v.find(n=>/128/.test(n.textContent||'')));input.value='新品';const pending=v.click('新建并使用');assert.equal(v.button('取消').disabled,true);assert.equal(input.disabled,true);v.key('Escape');await v.click('取消');assert.equal(v.overlay.hidden,false);rejectCreate(Error('连接中断'));await pending;assert.equal(input.value,'新品');assert.equal(input.disabled,false);assert.ok(v.find(n=>/连接中断/.test(n.textContent||'')));await v.click('取消');assert.equal(await v.result,null);
});
test('search appears only for a longer category list and narrows choices',async()=>{
 const small=mount([{id:'a',name:'新品'}]);assert.equal(small.find(n=>n.attrs['aria-label']==='搜索分类'),undefined);await small.click('取消');const v=mount(Array.from({length:9},(_,i)=>({id:String(i),name:'分类'+i}))),search=v.find(n=>n.attrs['aria-label']==='搜索分类');assert.ok(search);search.value='分类8';search.oninput({isTrusted:true});assert.ok(v.category('分类8'));assert.equal(v.category('分类1'),undefined);await v.choose('分类8');assert.deepEqual(plain(await v.result),{mode:'replace',category_ids:['8']});
});
