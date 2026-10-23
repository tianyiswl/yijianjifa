const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');

async function setup(receipt){
 const calls=[],timers=[];
 class Node{
  constructor(){this.children=[];this.style={};}
  append(...children){this.children.push(...children);}
  setAttribute(){} focus(){} remove(){}
 }
 const body=new Node();
 const context={document:{body,createElement:()=>new Node()},crypto:{getRandomValues:a=>a},
  window:{webkit:{messageHandlers:{p011App:{postMessage:async raw=>{calls.push(JSON.parse(raw));return JSON.stringify({ok:true,data:receipt});}}}}},
  setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){}};
 vm.createContext(context);
 vm.runInContext(fs.readFileSync('src/yijianjifa/webui/assets/use_term.js','utf8'),context);
 const dispose=context.P011UseTerm();
 await new Promise(resolve=>setImmediate(resolve));
 return{body,calls,timers,dispose,context};
}

test('free client mounts without requesting a product term or starting a renewal timer',async()=>{
 const view=await setup({expired:false,valid:true});
 assert.equal(view.calls.length,0);
 assert.equal(view.timers.length,0);
 assert.equal(view.body.children.length,0);
 assert.equal(typeof view.dispose,'function');view.dispose();
});

test('expired or corrupt legacy state cannot create an expiry dialog after navigation',async()=>{
 for(const receipt of [{expired:true,valid:true},{expired:true,valid:false}]){
  const view=await setup(receipt);view.dispose();view.context.P011UseTerm()();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(view.body.children.length,0);
  assert.equal(view.calls.length,0);
  assert.equal(view.timers.length,0);
 }
});
