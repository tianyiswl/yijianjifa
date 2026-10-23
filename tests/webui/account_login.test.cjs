const test=require('node:test'),assert=require('node:assert/strict');
const {AccountLogin}=require('../../src/yijianjifa/webui/assets/account_login.js');
test('polls login and returns completed account without treating window close as success',async()=>{
 const operations=[];let count=0;
 const api=async(op,p)=>{operations.push(op);if(op.endsWith('start'))return {id:'j',state:'waiting'};return ++count===1?{id:'j',state:'waiting'}:{id:'j',state:'completed',account_id:'a'};};
 const controller=new AccountLogin(api,()=>{},async()=>{});
 assert.equal((await controller.run(null)).account_id,'a');assert.equal(operations.length,3);
});
test('cancel before start reply cancels backend and cannot report completed login',async()=>{
 let resolve;const operations=[];
 const api=async(op,p)=>{operations.push(op);if(op.endsWith('start'))return new Promise(r=>resolve=r);return {id:'j',state:'completed',account_id:'a'};};
 const controller=new AccountLogin(api,()=>{},async()=>{}),result=controller.run(null);
 await controller.cancel();resolve({id:'j',state:'completed',account_id:'a'});
 assert.equal((await result).state,'cancelled');assert.ok(operations.includes('account.login.cancel'));
});
test('legacy login keeps its old start payload while explicit platform is forwarded only at start',async()=>{
 const calls=[];const api=async(op,p)=>{calls.push({op,p});return op.endsWith('start')?{id:'j',state:'waiting',platform:p.platform}:{id:'j',state:'completed',account_id:'a',platform:'kuaishou'};};
 const legacy=new AccountLogin(api,()=>{},async()=>{});await legacy.run('a');assert.deepEqual(calls[0].p,{account_id:'a'});
 calls.length=0;const explicit=new AccountLogin(api,()=>{},async()=>{});await explicit.run('a','kuaishou');assert.deepEqual(calls[0].p,{account_id:'a',platform:'kuaishou'});assert.deepEqual(calls[1].p,{id:'j'});
});
