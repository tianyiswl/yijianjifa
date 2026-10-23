const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const asset=n=>fs.readFileSync(path.join(__dirname,'../../src/yijianjifa/webui/assets',n),'utf8');
test('shelved commerce route cannot dispose current view or mount commerce',()=>{
 const context={};vm.createContext(context);vm.runInContext(asset('features.js')+asset('router.js'),context);
 vm.runInContext("P011Router.current='publish';P011Router.dispose=()=>{throw Error('disposed')};P011Views.commerce=()=>{throw Error('mounted')};P011Router.go('commerce');",context);
 assert.equal(vm.runInContext('P011Router.current',context),'publish');
});
test('shared visibility removes commerce navigation and hides handoff and probe',()=>{
 let removed=false;const handoff={},probe={},header={};const context={document:{querySelectorAll:s=>s==='nav button'?[{textContent:'普通发布',remove(){throw Error('ordinary removed')}},{textContent:'团购带货',remove(){removed=true}}]:s==='[data-commerce-column]'?[header]:[],getElementById:id=>id==='commerce-btn'?handoff:probe}};
 vm.runInNewContext(asset('features.js')+';P011ApplyFeatures();',context);
 assert.equal(removed,true);assert.equal(handoff.hidden,true);assert.equal(probe.hidden,true);assert.equal(header.hidden,true);
});
test('navigation has exactly one download entry through repeated module switches',()=>{
 const {Node}=require('./support/commerce_view.cjs'),nav=new Node('nav');
 const context={P011Router:{current:'library'},window:{p011Navigate:fn=>fn()},document:{querySelector:()=>nav,querySelectorAll:()=>[],getElementById:()=>null,createElement:tag=>new Node(tag)}};vm.createContext(context);vm.runInContext(asset('features.js'),context);
 for(const name of ['library','downloads','voices','tasks','publish']){context.P011Router.current=name;vm.runInContext('P011ApplyFeatures()',context);assert.equal(nav.children.filter(b=>b.textContent==='视频下载').length,1);assert.deepEqual(nav.children.slice(0,2).map(b=>b.dataset.route),['mixing','library']);}
});
