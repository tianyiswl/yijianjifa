const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

async function fixture({delay}={}){
 const tasks=['make','publish','download'].map((kind,index)=>({id:'task-'+index,kind,name:['制作任务甲','发布任务乙','下载任务丙'][index],state:'completed',quantity:1,completed:1,succeeded:1,failed:0,created_at:'2026-10-08T00:00:00Z'})),calls=[];
 const v=await mount(async(op,p)=>{calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:tasks,total:tasks.length};if(op==='task.center.detail'){const task=tasks.find(t=>t.id===p.id),data={task,controls:[],settings:{},items:[],issues:[],outputs:[]};return delay?.(task,data)||data;}throw Error(op);},'tasks');
 const row=index=>v.getId('rows').children[index],active=()=>v.getId('rows').children.filter(n=>(n.className||'').split(' ').includes('active'));
 const assertCurrent=index=>{assert.equal(active().length,1);assert.equal(active()[0],row(index));assert.equal(row(index).attrs['aria-selected'],'true');assert.equal(v.getId('detail').children.find(n=>n.tag==='h3')?.textContent,tasks[index].name);};
 return {v,tasks,calls,row,active,assertCurrent};
}

test('task row selection is explicit and a single click keeps the highlighted row and detail together',async()=>{
 const {v,calls,row,assertCurrent}=await fixture();assertCurrent(0);row(1).onclick({isTrusted:true,target:row(1).children[1]});await v.flush();assertCurrent(1);
 assert.equal(row(0).attrs['aria-selected'],'false');assert.equal(calls.some(c=>!['local.checks.status','task.center.list','task.center.detail'].includes(c.op)),false);v.dispose();
});

test('direct double click and right click immediately move the highlight with the current task detail',async()=>{
 const {v,calls,row,assertCurrent}=await fixture();row(1).ondblclick({isTrusted:true,target:row(1).children[1]});await v.flush();assertCurrent(1);
 row(2).oncontextmenu({isTrusted:true,target:row(2).children[1],clientX:200,clientY:150,preventDefault(){}});await v.flush();assertCurrent(2);assert.equal(v.getId('ctx').style.display,'block');
 assert.equal(v.getId('count').textContent,'已选 0 项');await v.click('查看详情');assertCurrent(2);assert.equal(v.getId('ctx').style.display,'none');assert.equal(calls.some(c=>!['local.checks.status','task.center.list','task.center.detail'].includes(c.op)),false);v.dispose();
});

test('clicking or double clicking a checkbox only changes bulk selection and does not switch the highlighted detail',async()=>{
 const {v,calls,row,assertCurrent}=await fixture(),before=calls.filter(c=>c.op==='task.center.detail').length,check=row(1).children[0].children[0];
 row(1).onclick({isTrusted:true,target:check});check.checked=true;check.onchange({isTrusted:true,target:check});await v.flush();assertCurrent(0);assert.equal(v.getId('count').textContent,'已选 1 项');
 const currentCheck=row(1).children[0].children[0];row(1).ondblclick({isTrusted:true,target:currentCheck});await v.flush();assertCurrent(0);assert.equal(calls.filter(c=>c.op==='task.center.detail').length,before);
 v.getId('clear').onclick({isTrusted:true});await v.flush();assertCurrent(0);assert.equal(v.getId('count').textContent,'已选 0 项');v.dispose();
});

test('a late detail response cannot change the current highlighted task or its details',async()=>{
 let release;const {v,row,assertCurrent}=await fixture({delay:(task,data)=>task.id==='task-1'?new Promise(resolve=>release=()=>resolve(data)):null});
 row(1).onclick({isTrusted:true,target:row(1).children[1]});row(2).onclick({isTrusted:true,target:row(2).children[1]});await v.flush();assertCurrent(2);release();await v.flush();assertCurrent(2);await v.tick();assertCurrent(2);v.dispose();
});
