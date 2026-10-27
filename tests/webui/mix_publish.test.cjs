const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
const form=require('../../src/yijianjifa/webui/assets/mixing_form.js');
const capabilities={douyin:['publish_now','platform_schedule'],xiaohongshu:['publish_now','platform_schedule']};
function localDay(offset){return new Date(Date.now()+8*3600000+offset*86400000).toISOString().slice(0,10);}
const accounts=[{id:'a',platform:'douyin',nickname:'抖音主号',remark:'',login_state:'valid',is_default:true},{id:'b',platform:'xiaohongshu',nickname:'小红书主号',remark:'',login_state:'valid',is_default:false}];
function advanced(ctx){let current=form.defaults();return {params:()=>form.params({...current,quantity:ctx.state.quantity||10},ctx.state.materialScope==='all'?[]:ctx.state.material,ctx.state.copyScope==='categories'?ctx.state.copies:[],ctx.state.copyScope!=='none'),restore:async p=>{current=form.fromParams(p);ctx.state.quantity=p.quantity;ctx.state.material=[...p.material_categories];ctx.state.copies=[...p.copy_categories];ctx.state.materialScope=p.material_scope||((p.material_categories||[]).length?'categories':'all');ctx.state.copyScope=p.copy_scope||(p.use_copy===false?'none':(p.copy_categories||[]).length?'categories':'all');},refresh:async()=>{},applyPendingPreset:async()=>{},leave:async()=>true,submitted(){},dispose(){},markDirty(){},showPublish(){}};}
function backend(op,p){if(op==='mix.daily.read')return {settings:null,capabilities,accounts,has_saved_settings:false};if(op==='mix.daily.save')return {saved:true,settings:p};if(op==='mix.daily.status')return {phase:'making',publish_task_ids:[],accepted:0,pending:2,failed:0,total:2,reason:''};if(op==='category.list')return {items:[{id:p.kind==='video'?'vcat':'ccat',name:'常用'}],total:1};if(op==='library.list')return {items:[],total:6};if(op==='task.poll')return {epoch:'e',revision:1,changed:false};throw Error(op);}
async function create(handler=backend,incoming){return mount(handler,'mixing',incoming,{P011MixAdvanced:advanced});}
function change(node,value){node.value=value;node.onchange({isTrusted:true,target:node});}

// A regression routing the primary action through mix.submit or adding a second
// confirmation would fail: this executes the shipped view and native intent path.
test('one click submits the current batch for making and real publication without a modal',async()=>{
 const calls=[];const view=await create((op,p,r)=>{if(op==='mix.daily.submit'){calls.push({op,p,r});return {id:'new-batch'};}return backend(op,p);});
 change(view.getId('daily-publish-days'),'1');change(view.getId('daily-publish-per-day'),'2');await view.flush();
 view.getId('quick-generate').onclick({isTrusted:true});await view.flush();
 assert.equal(calls.length,1);assert.equal(calls[0].p.mix.quantity,2);assert.equal(calls[0].p.publishing.days,1);assert.equal(calls[0].p.publishing.per_day,2);assert.equal(calls[0].p.publishing.mode,'platform_schedule');assert.deepEqual(calls[0].p.publishing.targets,[{account_id:'a',platform:'douyin'}]);assert.equal(typeof calls[0].r.request_id,'string');assert.equal(view.getId('modal-root').classList.contains('hide'),true);assert.match(view.getId('daily-progress').querySelector('.mixing-execution-phase').textContent,/制作/);view.dispose();
});
test('only generate still submits mix.submit without any publish linkage',async()=>{
 const sent=[];const view=await create((op,p)=>{if(op==='mix.submit'){sent.push(p);return {id:'only-make'};}return backend(op,p);});
 change(view.getId('daily-publish-days'),'3');change(view.getId('daily-publish-per-day'),'1');view.getId('quick-only-generate').onclick({isTrusted:true});await view.flush();
 assert.equal(sent.length,1);assert.equal(sent[0].quantity,3);assert.equal('publish_after' in sent[0],false);assert.equal('publishing' in sent[0],false);view.dispose();
});
test('saved scopes, full subtitle text, targets and daily timing survive reopening',async()=>{
 const mix={...form.params({...form.defaults(),quantity:4,fullCaption:true,override:'每天都用这行字幕'},['vcat'],[],false),batch_full_text:'每天都用这行字幕'};const sent=[];
 const view=await create((op,p)=>{if(op==='mix.daily.read')return {settings:{mix,publishing:{targets:[{account_id:'a',platform:'douyin'},{account_id:'b',platform:'xiaohongshu'}],mode:'platform_schedule',day_offset:1,time:'12:30',interval_hours:3}},capabilities,accounts,has_saved_settings:true};if(op==='mix.daily.submit'){sent.push(p);return {id:'daily'};}return backend(op,p);});
 assert.equal(view.getId('quick-material').value,'categories');assert.equal(view.getId('quick-copy').value,'none');assert.equal(view.getId('quick-quantity').value,'4');assert.equal(view.getId('daily-save-state').textContent,'');assert.equal(view.getId('daily-save-feedback').classList.contains('hide'),true);
 view.getId('quick-generate').onclick({isTrusted:true});await view.flush();assert.equal(sent.length,1);assert.equal(sent[0].mix.use_copy,false);assert.deepEqual(sent[0].mix.material_categories,['vcat']);assert.equal(sent[0].publishing.targets.length,2);assert.equal(sent[0].publishing.time,'12:30');assert.equal(sent[0].publishing.start_date,localDay(1));assert.equal('day_offset'in sent[0].publishing,false);view.dispose();
});
test('account choices are grouped by platform and multiple selected accounts are saved automatically',async()=>{
 const saved=[];const view=await create((op,p)=>{if(op==='mix.daily.save'){saved.push(p);return {saved:true};}return backend(op,p);});
 const choice=view.find(n=>n.tag==='input'&&n.dataset.account==='b');choice.checked=true;choice.onchange({isTrusted:true,target:choice});await view.tick();await view.tick();await view.flush();
 assert.equal(saved.at(-1).publishing.targets.length,2);assert.deepEqual(saved.at(-1).publishing.targets,[{account_id:'a',platform:'douyin'},{account_id:'b',platform:'xiaohongshu'}]);assert.equal(view.getId('daily-save-state').textContent,'');assert.equal(view.getId('daily-save-feedback').classList.contains('hide'),true);assert.ok(view.find(n=>n.textContent==='小红书'));view.dispose();
});
test('an out-of-range selected start remains visible with a correction message',async()=>{
 const view=await create();change(view.getId('daily-publish-day'),localDay(-1));assert.equal(view.getId('daily-publish-day').value,localDay(-1));assert.match(view.getId('schedule-brief').children.find(n=>n.attrs.role==='alert').textContent,/已失效/);view.dispose();
});
test('loading and failed daily settings keep publication disabled',async()=>{
 let finish;const view=await create((op,p)=>op==='mix.daily.read'?new Promise(resolve=>finish=resolve):backend(op,p));assert.equal(view.getId('quick-generate').disabled,true);view.getId('quick-generate').onclick({isTrusted:true});await view.flush();finish({settings:null,accounts:[],has_saved_settings:false});await view.flush();assert.equal(view.getId('quick-generate').disabled,true);assert.match(view.getId('daily-accounts').querySelector('p').textContent,/账号/);view.dispose();
});
test('combined presets keep their original entry and migrate account and interval to daily settings',async()=>{
 let applied=0;const view=await mount((op,p)=>op==='preset.get'?{kind:'mix_then_publish',state:'ready',payload:{mix_preset_id:'mix-rule',account_id:'a',start:'2026-10-17T12:00:00+08:00',interval_hours:2.5,linkage:'whole_batch'}}:backend(op,p),'mixing',{presetId:'combined'},{P011MixAdvanced:ctx=>({...advanced(ctx),applyPendingPreset:async()=>{applied++;}})});
 await view.flush();assert.equal(applied,1);assert.equal(view.getId('daily-publish-mode').value,'platform_schedule');assert.equal(view.getId('daily-publish-interval').value,'2.5');assert.equal(view.find(n=>n.tag==='input'&&n.dataset.account==='a').checked,true);view.dispose();
});
test('late combined preset responses cannot apply after leaving mixing',async()=>{
 let finish,applied=0;const view=await mount((op,p)=>op==='preset.get'?new Promise(resolve=>finish=resolve):backend(op,p),'mixing',{presetId:'combined'},{P011MixAdvanced:ctx=>({...advanced(ctx),applyPendingPreset:async()=>{applied++;}})});view.dispose();finish({kind:'mix_then_publish',state:'ready',payload:{mix_preset_id:'mix-rule',account_id:'a',interval_hours:3}});await view.flush();assert.equal(applied,0);
});
test('the current batch status distinguishes accepted platform tasks from published results and links task details',async()=>{
 const routes=[];const view=await create((op,p)=>{if(op==='mix.daily.submit')return {id:'batch'};if(op==='mix.daily.status')return {phase:'accepted',publish_task_ids:['pub'],accepted:2,pending:0,failed:0,total:2,reason:''};return backend(op,p);});view.runtime.P011Router.go=(...args)=>routes.push(args);
 view.getId('quick-generate').onclick({isTrusted:true});await view.flush();assert.match(view.getId('daily-progress').querySelector('.mixing-execution-phase').textContent,/已提交平台/);assert.doesNotMatch(view.getId('daily-progress').querySelector('.mixing-execution-phase').textContent,/已发布/);await view.click('查看本批任务');assert.equal(routes[0][0],'tasks');view.dispose();
});
test('missing material provides a direct library route and prevents empty batches',async()=>{
 let submits=0;const routes=[];const view=await create((op,p)=>{if(op==='library.list')return {items:[],total:0};if(op==='mix.daily.submit'){submits++;return {id:'bad'};}return backend(op,p);});view.runtime.P011Router.go=(...args)=>routes.push(args);view.getId('quick-generate').onclick({isTrusted:true});await view.flush();assert.equal(submits,0);assert.equal(view.getId('quick-add-material').classList.contains('hide'),false);view.getId('quick-add-material').onclick({isTrusted:true});await view.flush();assert.equal(routes[0][0],'library');view.dispose();
});

test('the existing incoming task-detail entry distinguishes cancelled items from failures',async()=>{
 const view=await mount((op,p)=>{if(op==='task.detail')return {id:'m',state:'cancelled',succeeded:0,failed:0,completed:0,quantity:2,plan:{},copies:[],items:[{index:0,state:'cancelled'},{index:1,state:'failed'}]};return backend(op,p);},'mixing',null,{P011MixAdvanced:advanced,P011Router:{pendingTaskId:'m',go(){}}});await view.flush();assert.equal(view.getId('modal-root').classList.contains('hide'),false);const lines=[];function walk(n){lines.push(n.textContent);for(const c of n.children||[])walk(c);}walk(view.getId('modal-body'));assert.ok(lines.includes('已取消'));assert.ok(lines.includes('失败，请检查素材与制作环境'));view.dispose();
});
test('the existing no-copy detail keeps five compact items per page without a repeated missing-title list',async()=>{
 const view=await mount((op,p)=>{if(op==='task.detail')return {state:'completed',quantity:10,succeeded:10,failed:0,completed:10,created_at:'2026-09-28T01:00:00Z',plan:{use_copy:false,resolution:'720p',aspect_ratio:'9:16'},total_ms:15000,copies:Array.from({length:10},()=>({title:''})),items:Array.from({length:10},(_,index)=>({index,state:'completed'}))};return backend(op,p);},'mixing',null,{P011MixAdvanced:advanced,P011Router:{pendingTaskId:'m',go(){}}});await view.flush();const table=()=>view.getId('modal-body').children[2].children[0];assert.equal(table().children.length,6);assert.equal(table().children[1].children[1].textContent,'纯画面（不使用文案）');await view.click('下一页');assert.equal(table().children[1].children[0].textContent,'6');view.dispose();
});
