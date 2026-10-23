const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {mount,baseData}=require('./support/commerce_view.cjs');

function ordinary(){const calls=[];return {calls,handler(op,p){calls.push({op,p});
 if(op==='publish.execution.availability')return {available:false};
 if(op==='publish.browser_mode.read')return {mode:'background'};
 if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
 if(op==='account.list')return {items:[{id:'a',platform:'douyin',nickname:'原抖音账号',group_name:'',is_default:true}],total:1};
 if(op==='media.thumbnail')return {image:''};
 if(op==='publish.prepare')return {...baseData('commerce.prepare'),token:'legacy'};
 return baseData(op);
}};}

test('ordinary publish defaults directly to original account settings and V1 preparation',async()=>{
 const f=ordinary(),v=await mount(f.handler,'publish');
 assert(f.calls.some(c=>c.op==='settings.publish.read'&&c.p.view==='publish'));
 assert(f.calls.some(c=>c.op==='account.list'&&c.p.platform==='douyin'));
 assert(f.calls.some(c=>c.op==='preset.list'&&c.p.kind==='normal_publish'));
 assert.equal(f.calls.some(c=>c.op.startsWith('multi_publish.')),false);
 assert.equal(v.find(n=>n.id==='publish-account').value,'a');
 await v.click('指定时间');const check=v.find(n=>n.attrs['aria-label']==='选择 测试视频');check.checked=true;check.onchange();
 await v.click('预览发布安排');assert(f.calls.some(c=>c.op==='publish.prepare'));assert.equal(f.calls.some(c=>c.op.startsWith('multi_publish.')),false);v.dispose();
});

test('every sidebar exposes one independent distribution route and respects the current leave guard',()=>{
 const {Node}=require('./support/commerce_view.cjs');
 const nav=new Node('nav'),navigation=[],context={P011Router:{current:'library',go:name=>navigation.push(name)},window:{p011Navigate:()=>false},document:{querySelector:()=>nav,querySelectorAll:()=>[],getElementById:()=>null,createElement:tag=>new Node(tag)}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../../src/yijianjifa/webui/assets/features.js'),'utf8'),context);
 for(const route of ['library','mixing','finished','publish','multi_publish','downloads','accounts','tasks','voices','presets']){
  nav.replaceChildren(new Node('button'));context.P011Router.current=route;vm.runInContext('P011ApplyFeatures()',context);
  assert.deepEqual(nav.children.map(n=>n.textContent),['混剪制作','资料库','成品库','普通发布','多平台分发','账号管理','任务记录','配音管理','视频下载','常用方案与设置']);
  assert.equal(nav.children.filter(n=>n.disabled).length,1);assert.equal(nav.children.find(n=>n.disabled).dataset.route,route);
  const multi=nav.children.find(n=>n.dataset.route==='multi_publish');if(!multi.disabled)multi.onclick({isTrusted:true});
 }
 assert.deepEqual(navigation,[]);context.P011Router.current='publish';context.window.p011Navigate=fn=>fn();vm.runInContext('P011ApplyFeatures()',context);nav.children.find(n=>n.dataset.route==='multi_publish').onclick({isTrusted:true});assert.deepEqual(navigation,['multi_publish']);
});
