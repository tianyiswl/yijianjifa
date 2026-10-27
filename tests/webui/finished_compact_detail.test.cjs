const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
test('finished preview keeps publishing edits without repeating the list summary',async()=>{
 const row={id:'video-1',name:'测试成品.mp4',display_name:'测试成品.mp4',format:'MP4',origin_type:'generated',duration_ms:2000,category_ids:[],title:'投稿标题',body:'文案',tags:'标签',missing:true};
 const writes=[];
 const v=await mount((op,p)=>{
  if(op==='finished.list')return {items:[row],total:1};
  if(op==='category.list')return {items:[]};
  if(op==='finished.save'){writes.push(p);Object.assign(row,p,{name:p.display_name});return {...row};}
  throw Error(op);
 },'finished',undefined,{structuredClone,innerWidth:1280,innerHeight:800,P011Router:{current:'finished',go(){}}});
 const query=v.runtime.document.querySelector;
 v.runtime.document.querySelector=s=>s==='#finished-display_name'?v.find(n=>n.id==='finished-display_name'):query(s);
 await v.click('预览 / 编辑');
 assert.throws(()=>v.find(n=>n.className==='media-caption'||n.className==='category-detail'||n.className==='format-note'));
 const rename=v.find(n=>n.id==='finished-display_name');
 assert.equal(rename.parentElement.hidden,true);
 const title=v.find(n=>n.id==='finished-title');title.value='修改后的投稿标题';title.oninput({isTrusted:true});
 await v.click('保存发布信息');
 assert.equal(writes[0].title,'修改后的投稿标题');assert.equal(writes[0].display_name,'测试成品.mp4');
 // Renaming remains available through the existing row menu, rather than
 // occupying the default publishing pane.
 const tr=v.find(n=>n.tag==='tr'&&typeof n.oncontextmenu==='function');
 assert(tr);tr.oncontextmenu({preventDefault(){},clientX:20,clientY:20});
 await v.click('重命名显示名称');assert.equal(rename.parentElement.hidden,false);
 rename.value='短名称.mp4';rename.oninput({isTrusted:true});await v.click('保存发布信息');
 assert.equal(writes[1].display_name,'短名称.mp4');assert.equal(rename.parentElement.hidden,true);v.dispose();
});
