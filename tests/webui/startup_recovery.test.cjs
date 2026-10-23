const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=path.join(__dirname,'../../src/yijianjifa/webui/assets/startup_recovery.js');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.style={};this.textContent='';this.inert=false;}
 append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(k,fn){this['on'+k]=fn;}
 focus(){this.focused=true;}
}
function setup(response){
 assert.ok(fs.existsSync(source),'startup recovery prompt is not implemented');
 const document={body:new Element('body'),createElement:tag=>new Element(tag)};
 const background=new Element('main');document.body.append(background);
 const calls=[],routes=[];
 const context={document,crypto:require('node:crypto').webcrypto,P011Router:{go:name=>routes.push(name)},window:{}};
 context.window.p011Navigate=fn=>fn();
 context.window.webkit={messageHandlers:{p011App:{postMessage:async raw=>{
  const request=JSON.parse(raw);calls.push(request);return JSON.stringify(typeof response==='function'?await response():response);
 }}}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(source,'utf8'),context);
 const walk=node=>[node,...node.children.flatMap(walk)];
 const button=label=>walk(document.body).find(n=>n.tag==='button'&&n.textContent===label);
 return {context,document,background,calls,routes,button,walk,start:()=>vm.runInContext('P011StartupRecovery()',context)};
}
const data={ok:true,data:{tasks:[{id:'older',state:'interrupted',quantity:20,completed:10,succeeded:10,failed:0},{id:'waiting',state:'paused',quantity:2,completed:0,succeeded:0,failed:0}]}};
test('startup asks once and deferring leaves every batch held',async()=>{
 const view=setup(data);await view.start();
 assert.equal(view.background.inert,true);
 assert.ok(view.walk(view.document.body).some(n=>n.textContent.includes('2 个')&&n.textContent.includes('12 条')));
 assert.equal(view.button('暂不继续').focused,true);
 await view.button('暂不继续').onclick({isTrusted:true});
 assert.equal(view.background.inert,false);
 assert.equal(view.walk(view.document.body).filter(n=>n.attrs.role==='dialog').length,0);
 await view.start();
 assert.deepEqual(view.calls.map(r=>r.operation),['task.recovery']);
 assert.deepEqual(view.routes,[]);
});
test('reviewing recovery only opens the chosen making task and never resumes it',async()=>{
 const view=setup(data);await view.start();
 await view.button('查看并选择继续').onclick({isTrusted:false});
 assert.deepEqual(view.routes,[]);
 await view.button('查看并选择继续').onclick({isTrusted:true});
 assert.deepEqual(view.routes,['tasks']);
 assert.equal(view.context.P011Router.pendingTaskId,'older');
 assert.deepEqual(view.calls.map(r=>r.operation),['task.recovery']);
 assert.equal(view.background.inert,false);
});
test('a clean startup does not show a recovery dialog',async()=>{
 const view=setup({ok:true,data:{tasks:[]}});await view.start();
 assert.equal(view.walk(view.document.body).filter(n=>n.attrs.role==='dialog').length,0);
 assert.equal(view.background.inert,false);
});
test('failed recovery read stays safe and offers a task-record entry',async()=>{
 const view=setup({ok:false,error:{message:'private path should not be displayed'}});
 await view.start();
 assert.ok(view.walk(view.document.body).some(n=>n.textContent.includes('无法读取')));
 assert.equal(view.walk(view.document.body).some(n=>n.textContent.includes('private path')),false);
 await view.button('查看任务记录').onclick({isTrusted:true});
 assert.deepEqual(view.routes,['tasks']);
 assert.deepEqual(view.calls.map(r=>r.operation),['task.recovery']);
});
test('a delayed startup reply remains a usable modal after navigation removes library styles',async()=>{
 let deliver;const view=setup(()=>new Promise(resolve=>{deliver=resolve;}));
 const pending=view.start();
 assert.equal(view.background.inert,false,'navigation must remain available while checking');
 const otherPage=new Element('main');
 view.document.body.replaceChildren(otherPage);
 deliver(data);await pending;
 const dialog=view.walk(view.document.body).find(n=>n.attrs.role==='dialog'),overlay=dialog.parent;
 assert.equal(overlay.style.position,'fixed','late dialog must cover the viewport without library CSS');
 assert.equal(overlay.style.inset,'0');
 assert.equal(overlay.style.display,'flex');
 assert.equal(dialog.style.overflow,'hidden');
 assert.equal(dialog.style.background,'white');
 assert.equal(otherPage.inert,true);
 assert.equal(view.background.inert,false);
 await view.button('暂不继续').onclick({isTrusted:true});
 assert.equal(otherPage.inert,false);
 assert.deepEqual(view.document.body.children,[otherPage]);
 assert.deepEqual(view.calls.map(r=>r.operation),['task.recovery']);
});
