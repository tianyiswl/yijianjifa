const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
function response(op,p){
 if(op==='mix.daily.read')return {settings:null,capabilities:{douyin:['platform_schedule']},accounts:[{id:'account',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true}],has_saved_settings:false};
 if(op==='mix.daily.save')return {saved:true};
 if(op==='preset.mix.list')return {items:[],total:0};
 if(op==='settings.mix_preset.read')return {id:''};
 if(op==='settings.output.read')return {directory:'/tmp/output'};
 if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
 if(op==='category.list')return {items:[],total:0};
 if(op==='library.list')return {items:p.kind==='copy'?[{id:'copy',title:'示例',voice:'一句口播。'}]:[],total:p.kind==='music'?0:1};
 if(op==='task.poll')return {epoch:'e',revision:1,tasks:[]};
 if(op==='mix.daily.status')return {phase:'making',total:6,accepted:0,pending:6,failed:0};
 throw Error(op);
}

test('both start actions use the same quantity derived from the visible daily rule',async()=>{
 const sent=[];const view=await mountMixing((op,p)=>{if(op==='mix.submit'||op==='mix.daily.submit'){sent.push({op,p});return {id:'batch'};}return response(op,p);});
 await view.change(view.get('daily-publish-days'),'3');await view.change(view.get('daily-publish-per-day'),'2');
 assert.equal(Number(view.get('quick-quantity').value),6);
 await view.click(view.get('quick-only-generate'));await view.click(view.get('quick-generate'));
 assert.deepEqual(sent.map(x=>[x.op,x.p.mix?.quantity??x.p.quantity]),[['mix.submit',6],['mix.daily.submit',6]]);
 assert.equal(sent[1].p.publishing.days,3);assert.equal(sent[1].p.publishing.per_day,2);view.dispose();
});

test('video-only generation cannot silently use an old quantity after the daily rule becomes invalid',async()=>{
 let submits=0;const view=await mountMixing((op,p)=>{if(op==='mix.submit'){submits++;return {id:'bad'};}return response(op,p);});
 await view.change(view.get('daily-publish-days'),'0');await view.click(view.get('quick-only-generate'));
 assert.equal(submits,0);assert.match(view.get('toast').textContent,/发布天数|数量/);view.dispose();
});

test('the last daily slot may be exactly fourteen days after the current instant',async()=>{
 const sent=[];const view=await mountMixing((op,p)=>{if(op==='mix.daily.submit'){sent.push(p);return {id:'at-limit'};}return response(op,p);},null,{},{now:'2026-10-15T04:00:00Z'});
 await view.change(view.get('daily-publish-day'),'2026-10-28');await view.change(view.get('daily-publish-days'),'2');await view.change(view.get('daily-publish-per-day'),'1');
 await view.click(view.get('quick-generate'));assert.equal(sent.length,1);assert.equal(sent[0].publishing.start_date,'2026-10-28');assert.doesNotMatch(view.get('schedule-brief').textContent,/超过14天/);view.dispose();
});

test('a final time one minute beyond fourteen days blocks publication but keeps the draft and video-only action',async()=>{
 const sent=[];let saved;const view=await mountMixing((op,p)=>{if(op==='mix.daily.submit'||op==='mix.submit'){sent.push({op,p});return {id:'batch'};}if(op==='mix.daily.save'){saved=p;return {saved:true};}return response(op,p);},null,{},{now:'2026-10-15T04:00:00Z'});
 await view.change(view.get('daily-publish-day'),'2026-10-28');await view.change(view.get('daily-publish-days'),'2');await view.change(view.get('daily-publish-per-day'),'1');await view.change(view.get('daily-publish-time'),'12:01');
 assert.match(view.get('schedule-brief').textContent,/超过14天/);assert.equal(view.get('quick-generate').disabled,true);assert.match(view.query('#schedule-brief [role="alert"]').textContent,/超过14天/);assert.equal(view.get('quick-only-generate').disabled,false);await view.click(view.get('quick-generate'));assert.equal(sent.length,0);assert.match(view.get('toast').textContent,/14天/);
 await view.save();assert.equal(saved.publishing.time,'12:01');await view.click(view.get('quick-only-generate'));assert.deepEqual(sent.map(x=>[x.op,x.p.quantity]),[['mix.submit',2]]);view.dispose();
});

test('correcting an overlong schedule removes its error and re-enables publication without creating a task',async()=>{
 const view=await mountMixing(response,null,{},{now:'2026-10-15T04:00:00Z'});
 await view.change(view.get('daily-publish-day'),'2026-10-28');await view.change(view.get('daily-publish-days'),'3');
 assert.equal(view.get('quick-generate').disabled,true);assert.ok(view.query('#schedule-brief .daily-schedule-error'));
 await view.change(view.get('daily-publish-days'),'1');assert.equal(view.get('quick-generate').disabled,false);assert.equal(view.query('#schedule-brief [role="alert"]'),null);assert.equal(view.get('quick-only-generate').disabled,false);assert.equal(view.calls.some(x=>x.operation==='mix.daily.submit'),false);view.dispose();
});

test('the periodic clock refresh re-enables a batch once its last slot falls within fourteen days',async()=>{
 const view=await mountMixing(response,null,{},{now:'2026-10-15T04:00:00Z'});
 await view.change(view.get('daily-publish-day'),'2026-10-28');await view.change(view.get('daily-publish-days'),'2');await view.change(view.get('daily-publish-per-day'),'1');await view.change(view.get('daily-publish-time'),'12:01');assert.equal(view.get('quick-generate').disabled,true);
 view.setNow('2026-10-15T04:02:00Z');await view.tick();assert.equal(view.get('quick-generate').disabled,false);assert.equal(view.query('#schedule-brief [role="alert"]'),null);view.dispose();
});

test('the fourteen-day cutoff includes the final within-day interval rather than just the first time',async()=>{
 let submits=0;const view=await mountMixing((op,p)=>{if(op==='mix.daily.submit'){submits++;return {id:'too-late'};}return response(op,p);},null,{},{now:'2026-10-15T04:00:00Z'});
 await view.change(view.get('daily-publish-day'),'2026-10-28');await view.change(view.get('daily-publish-days'),'2');await view.change(view.get('daily-publish-per-day'),'2');await view.change(view.get('daily-publish-time'),'09:00');await view.change(view.get('daily-publish-interval'),'3.5');
 assert.match(view.get('schedule-brief').textContent,/最后一条：10月29日12:30/);await view.click(view.get('quick-generate'));assert.equal(submits,0);assert.match(view.get('toast').textContent,/14天/);view.dispose();
});
