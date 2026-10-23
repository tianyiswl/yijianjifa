const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
async function fixture(){
 const calls=[],state={total:3,fail:false};
 const v=await mount((op,p)=>{calls.push({op,p});if(op==='task.center.list'){if(state.fail)throw Error('离线');return {total:state.total,items:[]};}throw Error(op);},'globals',['publish_progress']);
 v.runtime.P011ApplyFeatures();await v.tick();return {v,calls,state};
}
const banner=n=>(n.className||'').includes('p011-publish-progress');
test('only the task sidebar shows the pending task count, with no content banners or buttons',async()=>{
 const {v,calls}=await fixture();const badge=v.find(n=>n.className==='p011-task-count');assert.equal(badge.textContent,'3');assert.equal(badge.hidden,false);
 assert.equal(badge.parentElement.dataset.route,'tasks');assert.throws(()=>v.find(banner));assert.throws(()=>v.find(n=>n.textContent==='收起进度'));
 assert(calls.length>0&&calls.every(c=>c.op==='task.center.list'));assert.equal(calls[0].p.tab,'action');assert.equal(calls[0].p.limit,1);v.dispose();
});
test('badge clears when no pending tasks remain and keeps the same element during polling',async()=>{
 const {v,state}=await fixture();const badge=v.find(n=>n.className==='p011-task-count');await v.tick();assert.equal(v.find(n=>n.className==='p011-task-count'),badge);
 state.total=0;await v.tick();assert.equal(badge.hidden,true);state.total=7;await v.tick();assert.equal(badge.textContent,'7');assert.equal(badge.hidden,false);v.dispose();
});
test('a failed count read does not add warnings, fabricate zero, or replace task navigation',async()=>{
 const {v,state}=await fixture();const badge=v.find(n=>n.className==='p011-task-count'),button=badge.parentElement,click=button.onclick;state.fail=true;await v.tick();
 assert.equal(badge.textContent,'3');assert.equal(button.onclick,click);assert.throws(()=>v.find(banner));assert.throws(()=>v.find(n=>n.textContent.includes('读取失败')));v.dispose();
});
test('a late count response cannot recreate sidebar UI after leaving the page',async()=>{
 let release;const v=await mount(()=>new Promise(resolve=>release=resolve),'globals',['publish_progress']);v.runtime.P011ApplyFeatures();v.dispose();release({total:9,items:[]});await v.flush();assert.throws(()=>v.find(n=>n.className==='p011-task-count'));
});
