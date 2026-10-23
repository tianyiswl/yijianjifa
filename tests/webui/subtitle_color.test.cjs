const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup(){
 const nodes=[];function el(tag,text=''){const n={tag,textContent:text,style:{},attrs:{},children:[],events:{},append(...v){this.children.push(...v)},setAttribute(k,v){this.attrs[k]=v},addEventListener(k,v){this.events[k]=v}};nodes.push(n);return n;}
 function button(text,click){const b=el('button',text);b.events.click=click;return b;}
 const triggers={speech:el('button'),full:el('button')},form={speechColor:'#ffffff',fullColor:'#000000'};
 let dialog,hidden=false,rendered=0,error='';const ctx={form,el,button,dirty:false,document:{querySelector:q=>triggers[q.includes('speech')?'speech':'full']},modal:(title,body,actions)=>dialog={title,body,actions},hideModal:()=>hidden=true,render:()=>rendered++,notice:m=>error=m};
 const s=fs.readFileSync('src/yijianjifa/webui/assets/mixing_advanced.js','utf8');const start=s.indexOf(" for(const prefix of ['speech','full'])document.querySelector");const end=s.indexOf('\n });',start)+5;
 vm.runInNewContext(s.slice(start,end),ctx);
 return {form,nodes,triggers,get dialog(){return dialog},get hidden(){return hidden},get rendered(){return rendered},get error(){return error}};
}
test('subtitle swatches preview before commit and do not change the other subtitle',()=>{
 const v=setup();v.triggers.speech.events.click({isTrusted:true});
 const red=v.nodes.find(n=>n.attrs['aria-label']==='红色');red.events.click();
 assert.equal(v.form.speechColor,'#ffffff');assert.equal(v.nodes.find(n=>n.className==='subtitle-color-sample').style.color,'#ff4444');
 v.dialog.actions[1].events.click();assert.equal(v.form.speechColor,'#ff4444');assert.equal(v.form.fullColor,'#000000');assert.equal(v.rendered,1);
});
test('invalid hex cannot apply; cancel preserves original; native picker synchronizes hex',()=>{
 const v=setup();v.triggers.full.events.click({isTrusted:true});const hex=v.nodes.find(n=>n.attrs['aria-label']==='十六进制颜色'),native=v.nodes.find(n=>n.type==='color');
 native.value='#123456';native.events.input();assert.equal(hex.value,'#123456');
 hex.value='bad';v.dialog.actions[1].events.click();assert.ok(v.error);assert.equal(v.form.fullColor,'#000000');assert.equal(v.hidden,false);
 v.dialog.actions[0].events.click();assert.equal(v.hidden,true);assert.equal(v.form.fullColor,'#000000');
});
