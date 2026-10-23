// Minimal DOM contract for view-handler tests; does not claim browser/layout coverage.
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
class Node {
 constructor(tag){const classes=new Set();this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,on)=>{const yes=on===undefined?!classes.has(x):on;yes?classes.add(x):classes.delete(x);return yes;}};this.tag=tag;this.dataset={};this.children=[];this.attrs={};this.value='';this.textContent='';this.hidden=false;this.style={};}
 closest(selector){for(let n=this;n;n=n._parent)if(n.matches(selector))return n;return this._closest||(this._closest=new Node('section'));}
 matches(selector){return selector.split(',').some(part=>{part=part.trim();if(part.startsWith('#'))return this.id===part.slice(1);if(part.startsWith('.'))return (this.className||'').split(/\s+/).includes(part.slice(1));if(part.startsWith('[')){const m=part.match(/^\[([^=]+)(?:="?([^"\]]+)"?)?\]$/);return m&&(this.attrs[m[1]]!==undefined||this.dataset[m[1].replace(/^data-/,'').replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]!==undefined);}return this.tag===part;});}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 querySelectorAll(s){return this.children.flatMap(n=>[...(n.matches(s)?[n]:[]),...n.querySelectorAll(s)]);}
 contains(node){return node===this||this.children.some(n=>n.contains(node));}
 getBoundingClientRect(){return {left:0,top:0,width:180,height:100};}
 get parentElement(){return this._parent||(this._parent=new Node('container'));}
 get lastElementChild(){return this.children.at(-1);}
 addEventListener(event,fn){this['on'+event]=fn;}
 remove(){if(this._parent){this._parent.children=this._parent.children.filter(n=>n!==this);this._parent=null;}}
 after(node){const parent=this.parentElement;parent.insertBefore(node,parent.children[parent.children.indexOf(this)+1]);}
 before(node){this.parentElement.insertBefore(node,this);}
 focus(){}
 play(){return Promise.resolve();}
 pause(){}
 append(...nodes){for(const n of nodes){n.remove();n._parent=this;this.children.push(n);}}
 prepend(...nodes){for(const n of nodes)n.remove();for(const n of nodes)n._parent=this;this.children.unshift(...nodes);}
 replaceChildren(...nodes){for(const n of this.children)n._parent=null;this.children=[];this.append(...nodes);}
 insertBefore(node,before){node.remove();node._parent=this;const at=this.children.indexOf(before);this.children.splice(at<0?this.children.length:at,0,node);}
 setAttribute(key,value){this.attrs[key]=value;}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function mount(handler,viewName='commerce',incoming,extra={}){
 const ids=new Map(),selectors=new Map();
 const get=(map,key)=>{if(!map.has(key))map.set(key,new Node('container'));return map.get(key);};
 const document={addEventListener(){},removeEventListener(){},body:new Node('body'),createElement:tag=>new Node(tag),getElementById:id=>get(ids,id),querySelector:s=>get(selectors,s),querySelectorAll:()=>[]};
 document.getElementById('overlay').hidden=true;
 if(viewName==='accounts'){for(const id of ['platform','state'])document.getElementById(id).options=[new Node('option'),new Node('option')];document.addEventListener=()=>{};document.removeEventListener=()=>{};}
 const timers=[];
 const context={P011AccountLogin:require('../../../src/yijianjifa/webui/assets/account_login.js').AccountLogin,P011CoverBatch:require('../../../src/yijianjifa/webui/assets/cover_batch.js').CoverBatch,setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{},P011WriteIntent:require('../../../src/yijianjifa/webui/assets/write_intent.js').WriteIntent,document,Option:function(label,value){const n=new Node('option');n.textContent=label;n.value=value;return n;},crypto:require('node:crypto').webcrypto,P011Views:{},P011Router:{go(){}},P011CommerceForm:require('../../../src/yijianjifa/webui/assets/commerce_form.js'),P011PublishForm:require('../../../src/yijianjifa/webui/assets/publish_form.js')};
 context.window={webkit:{messageHandlers:{p011App:{postMessage:async raw=>{
  const request=JSON.parse(raw);try{return JSON.stringify({ok:true,data:await handler(request.operation,request.params,request)});}catch(e){return JSON.stringify({ok:false,error:{message:e.message}});}
 }}}}};
 Object.assign(context,extra);
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/features.js'),'utf8').replace('commerce:false',extra.commerceEnabled?'commerce:true':'commerce:false'),context);
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/selection_toolbar.js'),'utf8'),context);
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/category_manager.js'),'utf8'),context);
 for(const script of ['image_posts.js','image_publish.js']){const file=path.join(__dirname,'../../../src/yijianjifa/webui/assets/'+script);if(fs.existsSync(file))vm.runInNewContext(fs.readFileSync(file,'utf8'),context);}
 if(viewName==='voices')vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/cloud_voice.js'),'utf8'),context);
 if(viewName==='downloads')vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/download_subtitles.js'),'utf8'),context);
 if(viewName==='mixing')for(const script of ['mixing_publish.js','mixing_execution.js'])vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/'+script),'utf8'),context);
 for(const script of ['multi_publish_client.js','multi_publish_platform_fields.js','multi_publish_ui.js','multi_publish_targets.js']){const file=path.join(__dirname,'../../../src/yijianjifa/webui/assets/'+script);if(fs.existsSync(file))vm.runInNewContext(fs.readFileSync(file,'utf8'),context);}
 if(viewName==='multi_publish')vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/publish.js'),'utf8'),context);
 if(viewName!=='globals')vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/'+viewName+'.js'),'utf8'),context);
 let dispose;if(viewName==='globals'){const stops=[];for(const script of incoming){vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../../src/yijianjifa/webui/assets/'+script+'.js'),'utf8'),context);stops.push(context[script==='publish_progress'?'P011PublishProgress':'P011PublishIntervention']());}dispose=()=>stops.forEach(fn=>fn());}else dispose=context.P011Views[viewName](incoming);await flush();
 function walk(node){return [node,...node.children.flatMap(walk)];}
 function find(predicate){const n=[document.body,...ids.values(),...selectors.values(),...[...ids.values()].flatMap(n=>n._closest?[n._closest]:[])].flatMap(walk).find(predicate);if(!n)throw Error('View node not found');return n;}
 return {runtime:context,window:context.window,ids,query:s=>get(selectors,s),getId:id=>get(ids,id),find,dispose,flush,tick:async()=>{const fn=timers.shift();if(fn)await fn();await flush();},click:async (text,label)=>{find(n=>n.tag==='button'&&n.textContent===text&&(!label||n.attrs['aria-label']===label)).onclick({isTrusted:true});await flush();}};
}
function baseData(operation){
 if(operation==='settings.publish.read')return {settings:null};
 if(operation==='settings.publish.save')return {saved:true};
 if(operation==='preset.list')return {items:[],total:0};
 if(operation==='commerce.context')return {accounts:[{id:'a',nickname:'测试账号',capabilities:{commerce:'available'},store_scopes:['local']}],default_account_id:'a'};
 if(operation==='publish.candidates')return {items:[{id:'v',display_name:'测试视频',title:'标题',body:'正文',tags:'',duration_ms:1000}],total:1};
 if(operation==='commerce.preview'||operation==='commerce.prepare')return {locally_valid:true,music_choice:{mode:'none',candidates:[]},issues:[],items:[{video_id:'v',title:'标题',body:'正文',tags:'',time:null,blockers:[],required_choice:[],store:null,declaration:null}]};
 throw Error('unexpected operation '+operation);
}
module.exports={mount,baseData,Node};
