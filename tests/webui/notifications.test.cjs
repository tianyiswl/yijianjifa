const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
test('notification portal stays open for its own clicks and is removed on navigation',()=>{
 class Node {constructor(){this.children=[];this.handlers={};} append(...nodes){this.children.push(...nodes);} setAttribute(){} addEventListener(k,v){this.handlers[k]=v;} contains(n){return this===n||this.children.some(c=>c.contains(n));} remove(){this.removed=true;}}
 const body=new Node(),side=new Node(),handlers={};
 const document={body,createElement:()=>new Node(),querySelector:()=>side,addEventListener:(k,v)=>handlers[k]=v,removeEventListener:k=>delete handlers[k]};
 const context={P011Version:"0.1.31",document,crypto:{getRandomValues:a=>a},window:{webkit:{messageHandlers:{p011App:{postMessage:()=>new Promise(()=>{})}}}},clearTimeout(){},setTimeout(){}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('src/yijianjifa/webui/assets/notifications.js','utf8'),context);
 const dispose=context.P011Notifications(),panel=body.children[0],root=side.children[0];
 assert.equal(panel.className,'p011-notify-panel');assert.equal(root.children.length,2);assert.equal(root.children[0].className,'p011-help');
 assert.deepEqual(root.children[0].children[0].children.map(n=>n.textContent),['使用教程','下载最新版']);
 assert.equal(root.children[1].className,'p011-notify-footer');assert.equal(root.children[1].children[0].textContent,'版本 0.1.31');assert.equal(root.children[1].children[1].textContent,'通知');
 panel.hidden=false;handlers.click({type:'click',target:panel});assert.equal(panel.hidden,false);
 handlers.click({type:'click',target:body});assert.equal(panel.hidden,true);
 dispose();assert.equal(panel.removed,true);assert.equal(root.removed,true);assert.deepEqual(Object.keys(handlers),[]);
});
