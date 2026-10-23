const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
for(const managed of [false,true])test(`media detail resolves original and copy by identity (managed=${managed})`,async()=>{
 const calls=[];
 const view=await mount(async(op,p)=>{
  calls.push({op,p});
  if(op==='media.preference')return {keep_copy:false};
  if(op==='category.list')return {items:[]};
  if(op==='library.list')return {items:[{id:'asset-1',display_name:'测试素材',managed,missing:false,category_ids:[]}],total:1};
  if(op==='media.preview')return {url:'p011media://asset/token'};
  if(op==='media.reveal')return {shown:true};
  throw Error(op);
 },'library',undefined,{structuredClone,P011Router:{current:'library',go(){}}});
 await view.find(n=>n.tag==='tr'&&n.dataset.rowId==='asset-1').ondblclick({isTrusted:true});await view.flush();
 view.find(n=>n.textContent===(managed?'保存方式：使用程序副本，原文件保留':'保存方式：使用电脑原文件'));
 await view.click('查看原文件位置');
 if(managed)await view.click('查看程序副本位置');
 else assert.throws(()=>view.find(n=>n.tag==='button'&&n.textContent==='查看程序副本位置'));
 assert.deepEqual(calls.filter(x=>x.op==='media.reveal'),[
  {op:'media.reveal',p:{kind:'video',id:'asset-1',original:true}},
  ...(managed?[{op:'media.reveal',p:{kind:'video',id:'asset-1',original:false}}]:[])
 ]);
});
