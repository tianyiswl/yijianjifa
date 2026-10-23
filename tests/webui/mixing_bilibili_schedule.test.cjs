const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
const accounts=[['douyin','抖音主号'],['douyin','抖音副号'],['kuaishou','快手'],['channels','视频号'],['bilibili','B站']].map(([platform,nickname],n)=>({id:'account-'+n,platform,nickname,login_state:'valid',is_default:n===0}));
const capabilities=Object.fromEntries(accounts.map(a=>[a.platform,['platform_schedule']]));
const targets=accounts.map(a=>({account_id:a.id,platform:a.platform}));
function handler(extra=()=>undefined){return async(op,p)=>{const result=await extra(op,p);if(result!==undefined)return result;
 if(op==='mix.daily.read')return {settings:{mix:{quantity:6,total_ms:15000,segment_ms:3000},publishing:{targets,mode:'platform_schedule',start_date:'2026-10-17',time:'11:03',days:2,per_day:3,interval_hours:2.5}},accounts,capabilities,has_saved_settings:true,active_batch:null};
 if(op==='mix.daily.save')return {saved:true};if(op==='mix.daily.submit')return {id:'batch',phase:'checking_login'};
 if(op==='mix.daily.status')return {phase:'checking_login',total:24,can_continue:false};if(op==='mix.submit')return {id:'only'};
 if(op==='task.poll')return {epoch:'e',revision:1,tasks:[]};if(op==='preset.mix.list')return {items:[],total:0};
 if(op==='settings.output.read')return {directory:'/tmp/output'};if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
 if(op==='category.list')return {items:[],total:0};if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};if(op==='settings.mix_preset.read')return {id:''};throw Error(op);
};}
const mount=extra=>mountMixing(handler(extra),null,{},{now:'2026-10-16T00:00:00Z'});
const calls=(view,op)=>view.calls.filter(c=>c.operation===op);
const values=view=>view.get('daily-publish-minute').options.map(o=>o.value);
const fiveMinutes=Array.from({length:12},(_,n)=>String(n*5).padStart(2,'0'));

test('a saved multi-platform Bilibili minute stays visible and blocks publication without rounding or blocking video-only generation',async()=>{
 const view=await mount();const minute=view.get('daily-publish-minute');assert.equal(view.get('daily-publish-time').value,'11:03');assert.equal(minute.value,'03');
 assert.deepEqual(values(view),['00','03',...fiveMinutes.slice(1)]);assert.match(minute.options.find(o=>o.value==='03').textContent,/03.*需调整/);
 assert.match(view.query('#schedule-brief [role="alert"]').textContent,/B站.*5分钟刻度/);assert.equal(view.get('quick-generate').disabled,true);assert.equal(view.get('quick-only-generate').disabled,false);
 await view.tick();assert.equal(view.get('daily-publish-time').value,'11:03');assert.equal(view.get('daily-publish-day').value,'2026-10-17');await view.click(view.get('quick-generate'));assert.equal(calls(view,'mix.daily.submit').length,0);assert.match(view.get('toast').textContent,/B站.*5分钟刻度/);
 await view.change(view.get('daily-publish-hour'),'12');await view.save();assert.equal(calls(view,'mix.daily.save').at(-1).params.publishing.time,'12:03');assert.equal(minute.value,'03');
 await view.click(view.get('quick-only-generate'));assert.equal(calls(view,'mix.submit')[0].params.quantity,6);assert.equal('publishing'in calls(view,'mix.submit')[0].params,false);view.dispose();
});

test('choosing a five-minute value clears the Bilibili error immediately and submits the unchanged multi-platform target set',async()=>{
 const view=await mount();await view.change(view.get('daily-publish-minute'),'05');assert.equal(view.get('daily-publish-time').value,'11:05');assert.deepEqual(values(view),fiveMinutes);assert.equal(view.query('#schedule-brief [role="alert"]'),null);assert.equal(view.get('quick-generate').disabled,false);
 await view.save();assert.equal(calls(view,'mix.daily.save').at(-1).params.publishing.time,'11:05');await view.click(view.get('quick-generate'));assert.equal(calls(view,'mix.daily.submit').length,1);assert.deepEqual(calls(view,'mix.daily.submit')[0].params.publishing.targets,targets);assert.equal(calls(view,'mix.daily.submit')[0].params.publishing.time,'11:05');assert.equal(view.get('quick-generate').disabled,true);assert.equal(view.get('quick-only-generate').disabled,false);view.dispose();
});

test('deselecting Bilibili restores all sixty minutes and preserves the existing clock, while selecting it again marks the same invalid minute',async()=>{
 const view=await mount();const bili=view.query('[data-account="account-4"]');await view.change(bili,false);assert.equal(values(view).length,60);assert.equal(view.get('daily-publish-minute').value,'03');assert.equal(view.get('daily-publish-time').value,'11:03');assert.equal(view.query('#schedule-brief [role="alert"]'),null);assert.equal(view.get('quick-generate').disabled,false);
 await view.change(bili,true);assert.equal(values(view).length,13);assert.equal(view.get('daily-publish-time').value,'11:03');assert.equal(view.get('quick-generate').disabled,true);await view.change(view.get('daily-publish-minute'),'55');assert.deepEqual(values(view),fiveMinutes);await view.change(bili,false);assert.equal(values(view).length,60);assert.equal(view.get('daily-publish-time').value,'11:55');view.dispose();
});

test('correcting Bilibili minutes does not bypass an in-flight submission or its login-check lock',async()=>{
 let finish;const pending=new Promise(resolve=>{finish=resolve;});const view=await mount(op=>op==='mix.daily.submit'?pending:undefined);await view.change(view.get('daily-publish-minute'),'05');await view.click(view.get('quick-generate'));assert.equal(view.get('quick-generate').disabled,true);assert.equal(view.get('quick-only-generate').disabled,true);
 await view.change(view.get('daily-publish-minute'),'10');assert.equal(view.get('quick-generate').disabled,true);assert.equal(view.get('quick-only-generate').disabled,true);await view.click(view.get('quick-generate'));await view.click(view.get('quick-only-generate'));assert.equal(calls(view,'mix.daily.submit').length,1);assert.equal(calls(view,'mix.submit').length,0);
 finish({id:'batch',phase:'checking_login'});await view.flush();await view.flush();assert.equal(view.get('quick-generate').disabled,true);assert.equal(view.get('quick-only-generate').disabled,false);view.dispose();
});
