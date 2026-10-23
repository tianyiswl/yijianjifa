const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
test('a pending old batch never turns the new start button into continue',async()=>{
 const v=await mountMixing(async(op,p)=>{
  if(op==='mix.daily.read')return {accounts:[{id:'a',platform:'douyin',nickname:'合成账号',login_state:'valid',is_default:true}],capabilities:{douyin:['platform_schedule']},active_batch:null,recent_batch:null};
  if(op==='mix.daily.submit')return {id:'new-batch',phase:'needs_attention',can_continue:true};
  if(op==='mix.daily.status')return {id:'new-batch',phase:'needs_attention',can_continue:true};
  if(op==='mix.daily.continue')throw Error('must not resume automatically');
  if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};
  if(op==='settings.output.read')return {directory:'/tmp/finished'};
  if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
  if(op==='preset.mix.list'||op==='category.list')return {items:[],total:0};
  if(op==='settings.mix_preset.read')return {id:''};
  if(op==='mix.daily.save')return {saved:true};throw Error(op);
 });
 await v.click(v.get('quick-generate'));
 assert.equal(v.get('quick-generate').disabled,false);
 assert.equal(v.get('daily-progress').querySelectorAll('button').some(b=>!b.classList.contains('hide')&&b.textContent==='重新检查并继续'),true);
 assert.equal(v.calls.filter(c=>c.operation==='mix.daily.continue').length,0);
 await v.click(v.get('quick-generate'));
 assert.equal(v.calls.filter(c=>c.operation==='mix.daily.submit').length,2);
 assert.equal(v.calls.filter(c=>c.operation==='mix.daily.continue').length,0);
 v.dispose();
});
