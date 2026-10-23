const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
for(const kind of ['library','finished'])test(kind+' only accepts native drops within list and outside modal',async()=>{
 const calls=[];const v=await mount(async(op,p)=>{calls.push([op,p]);if(op==='media.preference')return {keep_copy:false};if(op==='category.list')return {items:[]};if(op.endsWith('.list'))return {items:[],total:0};if(op.endsWith('.drop'))return {imported:1,reused:0,errors:[]};return {};},kind,undefined,{structuredClone});
 v.query('#tablearea .tablewrap').getBoundingClientRect=()=>({left:200,right:800,top:300,bottom:700});
 v.query('#overlay').hidden=true;
 await v.window.p011Drop({x:250,y:250,token:'outside'});
 v.query('#overlay').hidden=false;await v.window.p011Drop({x:250,y:350,token:'modal'});
 assert.equal(calls.filter(([op])=>op.endsWith('.drop')).length,0);
 v.query('#overlay').hidden=true;await v.window.p011Drop({x:250,y:350,token:'inside'});
 const writes=calls.filter(([op])=>op.endsWith('.drop'));assert.equal(writes.length,1);assert.equal(writes[0][1].token,'inside');
 v.dispose();
});
