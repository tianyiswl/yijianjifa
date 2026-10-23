const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
test('original player moves the same video into a large layer and restores on Escape',()=>{
 class Node{constructor(){this.children=[];}append(...nodes){for(const n of nodes){n.remove();this.children.push(n);n.parent=this;}}prepend(n){n.remove();this.children.unshift(n);n.parent=this;}remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}setAttribute(){}focus(){this.focused=true;}}
 const handlers={},body=new Node(),media=new Node(),player=new Node(),state={};player.src='original';player.currentTime=12;player.play=()=>{player.played=true;return Promise.resolve();};media.append(player);
 const ctx={state,document:{body,activeElement:new Node(),addEventListener:(k,v)=>handlers[k]=v,removeEventListener:k=>delete handlers[k]},el:()=>new Node(),button:()=>new Node(),notice:()=>{}};
 const source=fs.readFileSync('src/yijianjifa/webui/assets/finished.js','utf8');vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf(' function openOriginalPlayer'),source.indexOf(' function renderDetail')),ctx);
 ctx.openOriginalPlayer(player,media);assert.equal(body.children.length,1);assert.equal(player.parent,body.children[0]);assert.equal(player.played,true);assert.equal(player.src,'original');assert.equal(player.currentTime,12);
 handlers.keydown({key:'Escape',preventDefault(){}});assert.equal(player.parent,media);assert.equal(body.children.length,0);assert.equal(state.originalClose,null);assert.equal(handlers.keydown,undefined);
});
