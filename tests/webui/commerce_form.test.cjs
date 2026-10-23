const test=require('node:test'),assert=require('node:assert/strict');
const form=require('../../src/yijianjifa/webui/assets/commerce_form.js');
test('commerce text patches apply only enabled batch values and selected item exceptions',()=>{
 const result=form.textPatches('account-a',['v1','v2'],{title:{enabled:true,value:'批量标题'},body:{enabled:false,value:'不可覆盖'}},{v1:{title:'单条标题',body:''},hidden:{title:'不可带入'}});
 assert.deepEqual(result,{'account-a:v1':{title:'单条标题',body:''},'account-a:v2':{title:'批量标题'}});
 assert.deepEqual(form.textPatches('account-b',['v2'],{},{}),{});
});
test('account change preserves public selection and timing but clears all account-specific settings',()=>{
 const state={account_id:'a',selected_video_ids:['v'],schedule:{days:3},batch:{title:'t'},items:{v:{title:'x'}},store_pool:['store'],music:{mode:'fixed'},declarations:{v:'d'}};
 assert.equal(form.switchAccount(state,'b',false),state);
 const next=form.switchAccount(state,'b',true);
 assert.deepEqual(next.selected_video_ids,['v']);assert.deepEqual(next.schedule,{days:3});
 for(const key of ['batch','items','music','declarations'])assert.deepEqual(next[key],{});
 assert.deepEqual(next.store_pool,[]);assert.equal(next.account_id,'b');assert.equal(state.account_id,'a');
});
test('store query collects every page under one token and distinguishes unavailable from empty',async()=>{
 const calls=[];const api=async(op,p)=>{calls.push([op,p]);return op.endsWith('search')?{available:true,token:'query',total:101}: {total:101,items:Array.from({length:p.offset===0?100:1},(_,i)=>({id:String(p.offset+i)}))};};
 const result=await form.loadStores(api,{account_id:'a'});
 assert.equal(result.items.length,101);assert.equal(calls.length,3);assert.equal(calls[2][1].offset,100);assert.equal(calls[2][1].token,'query');assert.equal(calls[2][1].account_id,'a');
 assert.deepEqual(await form.loadStores(async()=>({available:false,reason:'account_catalog_not_connected'}),{}),{available:false,reason:'account_catalog_not_connected',items:[]});
 assert.deepEqual(await form.loadStores(async()=>({available:true,token:'empty',total:0}),{}),{available:true,token:'empty',items:[]});
});
test('incomplete store pagination is rejected instead of applying a partial selection',async()=>{
 await assert.rejects(form.loadStores(async op=>op.endsWith('search')?{available:true,token:'t',total:2}:{items:[],total:2},{}),/不完整/);
});
test('manual stores retain removed choices but only selected videos enter preview',()=>{
 const state={v1:'removed',v2:'active',hidden:'old'};
 assert.deepEqual(form.manualStores(['v1','v2'],state),{v1:'removed',v2:'active'});
 assert.deepEqual(form.manualStores(['v2'],state),{v2:'active'});
 assert.equal(state.hidden,'old');
 const switched=form.switchAccount({account_id:'a',manual_store_overrides:state},'b',true);
 assert.deepEqual(form.manualStores(['v1'],switched.manual_store_overrides),{});
});
test('music payload defaults none and requires explicit current catalog choices',()=>{
 assert.deepEqual(form.musicChoice('none',['old'],'old',[]),{mode:'none',ids:[],token:null});
 const rows=[{id:'a'},{id:'b'}];
 assert.deepEqual(form.musicChoice('random',['b','a'],'query',rows),{mode:'random',ids:['b','a'],token:'query'});
 assert.deepEqual(form.musicChoice('fixed',['a'],'query',rows),{mode:'fixed',ids:['a'],token:'query'});
 for(const [mode,ids,token] of [['fixed',[],'q'],['fixed',['a','b'],'q'],['random',[],'q'],['random',['old'],'q'],['random',['a','a'],'q'],['fixed',['a'],null]])assert.throws(()=>form.musicChoice(mode,ids,token,rows));
});
test('declarations omit unknown policy, preserve explicit clearing and exclude unselected overrides',()=>{
 assert.deepEqual(form.declarationPayload(null,'old',{v:'old'},['v']),{});
 assert.deepEqual(form.declarationPayload('q','batch',{v:null,other:'hidden'},['v','v2']),{declarations:{token:'q',batch:'batch',items:{v:null}}});
 assert.deepEqual(form.declarationPayload('q','',{},['v']),{declarations:{token:'q',batch:null,items:{}}});
});
