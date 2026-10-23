const test=require('node:test'),assert=require('node:assert/strict');
const {CoverBatch}=require('../../src/yijianjifa/webui/assets/cover_batch.js');
test('batch controller reports progress and waits for completion',async()=>{
 const seen=[],ops=[];const batch=new CoverBatch(async(op)=>{ops.push(op);return op.endsWith('start')?{id:'j',state:'running',done:1,total:2}:{id:'j',state:'completed',done:2,total:2,result:{covers:{v:{token:'t'}}}};},s=>seen.push(s.done),async()=>{});
 assert.deepEqual(await batch.run({}),{covers:{v:{token:'t'}}});assert.deepEqual(seen,[1,2]);assert.equal(ops.length,2);
});
test('cancellation before start reply is remembered and never applies a completed result',async()=>{
 let finish;const ops=[];const batch=new CoverBatch(async(op)=>{ops.push(op);if(op.endsWith('start'))return new Promise(r=>finish=r);return {id:'j',state:'completed',done:1,total:1,result:{covers:{v:{token:'t'}}}};},()=>{},async()=>{});
 const running=batch.run({});await batch.cancel();finish({id:'j',state:'running',done:0,total:1});await assert.rejects(running,/取消/);assert.ok(ops.includes('publish.cover.batch.cancel'));
});
