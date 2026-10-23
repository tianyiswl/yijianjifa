const test=require('node:test'),assert=require('node:assert/strict');
const {scheduleCount,selectionAfterRefresh}=require('../../src/yijianjifa/webui/assets/publish_form.js');
test('schedule selection requires finite integer capacity and keeps no hidden stale ids',()=>{
 assert.equal(scheduleCount('3','5'),15);assert.equal(scheduleCount('3.5','2'),null);assert.equal(scheduleCount('','5'),null);assert.equal(scheduleCount('2','15'),null);
 assert.deepEqual(selectionAfterRefresh(['old','keep'],['new','keep']),['keep']);
});
test('showing published never selects it; hiding removes only published selections',()=>{
 const selected=['new','old'];
 assert.deepEqual(selectionAfterRefresh(selected,['new']),['new']);
 assert.deepEqual(selectionAfterRefresh(['new'],['new','old']),['new']);
});
test('leaving modified publish settings requires discard and blocks concurrent requests',async()=>{
 const {leaveGuard}=require('../../src/yijianjifa/webui/assets/publish_form.js');
 let action=0,resolve;const guard=leaveGuard(()=>new Promise(r=>resolve=r));
 const first=guard(true,()=>action++);await guard(true,()=>action++);assert.equal(action,0);resolve(false);await first;assert.equal(action,0);
 const second=guard(true,()=>action++);resolve(true);await second;assert.equal(action,1);
 await guard(false,()=>action++);assert.equal(action,2);
});
test('finished handoff keeps requested eligible IDs and explicitly reports exclusions',()=>{
 const {handoffSelection}=require('../../src/yijianjifa/webui/assets/publish_form.js');
 const r=handoffSelection(['b','a','b','blocked'],[{id:'a',published:true},{id:'b',published:false}]);
 assert.deepEqual(r.selected,['b','a']);assert.deepEqual(r.excluded,['blocked']);assert.equal(r.includesPublished,true);
 assert.throws(()=>handoffSelection('bad',[]));
});
