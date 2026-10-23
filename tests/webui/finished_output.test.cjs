const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
test('finished output uses native chooser, rejects synthetic clicks and prevents duplicate dialogs',async()=>{
 let resolve,chosen=0;
 const view=await mount(op=>{
  if(op==='finished.list')return {items:[],total:0};
  if(op==='settings.output.read')return {directory:'/initial'};
  if(op==='settings.output.choose'){chosen++;return new Promise(r=>resolve=r);}
  throw Error(op);
 },'finished');
 const b=view.query('[data-action="path"]');
 assert.equal(b.disabled,false);
 await b.onclick({isTrusted:false});assert.equal(chosen,0);
 const pending=b.onclick({isTrusted:true});await view.flush();
 assert.equal(chosen,1);assert.equal(b.disabled,true);
 await b.onclick({isTrusted:true});assert.equal(chosen,1);
 resolve({directory:'/selected',cancelled:false});await pending;
 assert.equal(b.disabled,false);assert.match(view.query('#toast').textContent,/selected/);
 assert.match(view.query('#toast').textContent,/新/);
 view.dispose();
});
for(const outcome of ['cancel','error'])test('finished output recovers button after '+outcome,async()=>{
 const view=await mount(op=>{
  if(op==='finished.list')return {items:[],total:0};
  if(op==='settings.output.choose'){if(outcome==='error')throw Error('目录不可写');return {directory:'/old',cancelled:true};}
  throw Error(op);
 },'finished');
 const b=view.query('[data-action="path"]');await b.onclick({isTrusted:true});
 assert.equal(b.disabled,false);
 assert.match(view.query('#toast').textContent,outcome==='cancel'?/已取消更改.*old/:/目录不可写/);
 view.dispose();
});
