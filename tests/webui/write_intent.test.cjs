const assert=require('node:assert/strict');
const fs=require('node:fs');
const file='src/yijianjifa/webui/assets/write_intent.js';
assert.ok(fs.existsSync(file),'write intent retry owner is missing');
const {WriteIntent}=require('../../'+file);
(async()=>{
const records=new Map();let calls=0,fail=true;
const transport=async(op,params,id)=>{calls++;await new Promise(r=>setTimeout(r,10));if(!records.has(id))records.set(id,{id:'copy'+records.size,title:params.title});if(fail){fail=false;throw new Error('lost reply');}return records.get(id);};
let next=0;const intent=new WriteIntent(transport,()=>String(++next));
const first=intent.run('copy.save',{title:'A'}),double=intent.run('copy.save',{title:'A'});
await assert.rejects(first,/lost reply/);await assert.rejects(double,/lost reply/);
assert.equal(calls,1);
assert.deepEqual(await intent.run('copy.save',{title:'A'}),{id:'copy0',title:'A'});
assert.equal(records.size,1);
await intent.run('copy.save',{title:'B'});assert.equal(records.size,2);
console.log('write intent: concurrent click and lost-reply retry passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
