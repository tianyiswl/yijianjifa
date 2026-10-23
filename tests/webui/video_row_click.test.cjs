const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
for(const page of ['library','finished'])test(page+' single click shows paused info and double click plays without rebuilding',async()=>{
 const row={id:'v1',display_name:'视频',category_ids:[],duration_ms:1200,origin_type:'external'};
 const v=await mount(async op=>{
  if(op==='category.list')return {items:[],total:0};
  if(op==='media.preference')return {keep_copy:false};
  if(op===page+'.list')return {items:[row],total:1};
  if(op==='media.preview')return {url:'p011media://asset/test'};
  if(op==='media.thumbnail')return {image:'test',duration_ms:1200};
  throw Error(op);
 },page,undefined,{structuredClone,P011Router:{current:page,go(){}}});
 const tr=v.find(n=>n.tag==='tr'&&typeof n.ondblclick==='function');
 const event={isTrusted:true,target:{closest:()=>null}};
 await tr.onclick(event);await v.flush();
 const player=v.find(n=>n.tag==='video');assert.equal(player.autoplay,false);
 v.query('#detail-pane').querySelector=()=>player;
 let played=0;player.play=()=>{played++;return Promise.resolve();};
 await tr.onclick(event);await tr.ondblclick(event);await v.flush();
 assert.equal(v.find(n=>n.tag==='video'),player);assert.equal(played,1);
 await tr.onclick({isTrusted:true,target:{closest:()=>({})}});assert.equal(played,1);
 v.dispose();
});
for(const kind of ['copy','music'])test('library '+kind+' single click reveals content without selecting or playing',async()=>{
 const row={id:'item-'+kind,title:'示例标题',display_name:'示例音乐',voice:'口播内容',body:'正文内容',tags:'标签',full:'字幕',category_ids:[]};
 const v=await mount(async(op,p)=>{
  if(op==='category.list')return {items:[],total:0};
  if(op==='media.preference')return {keep_copy:false};
  if(op==='library.list')return {items:p.kind===kind?[row]:[],total:p.kind===kind?1:0};
  if(op==='media.preview')return {url:'p011media://asset/audio'};
  throw Error(op);
 },'library',undefined,{structuredClone,P011Router:{current:'library',go(){}}});
 await v.click(kind==='copy'?'文案':'音乐');
 const tr=v.find(n=>n.tag==='tr'&&n.dataset.rowId===row.id);
 await tr.onclick({isTrusted:true,target:{closest:()=>null}});await v.flush();
 if(kind==='copy'){
  const input=v.find(n=>n.value==='口播内容');
  await tr.onclick({isTrusted:true,target:{closest:()=>null}});await v.flush();
  assert.equal(v.find(n=>n.value==='口播内容'),input);
 }else assert.equal(v.find(n=>n.tag==='audio').autoplay,false);
 assert.equal(v.find(n=>n.tag==='input'&&n.type==='checkbox'&&n.attrs['aria-label']?.startsWith('选择 ')).checked,false);
 v.dispose();
});
