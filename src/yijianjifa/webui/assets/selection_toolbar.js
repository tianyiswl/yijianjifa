/* Shared list actions. Views supply data operations; labels, layout and busy state live here. */
const P011Selection={
 mount({host,all,clear,remove,count,extras=[],scope}){
  const root=document.createElement('div');root.className='p011-selection';root.setAttribute('role','group');root.setAttribute('aria-label',(scope||'列表')+'批量操作');
  for(const [node,label,action] of [[all,'全选','all'],[clear,'取消全选','clear'],[remove,'批量删除','delete']]){
   if(!node)continue;node.textContent=label;node.className='p011-selection-button'+(action==='delete'?' p011-selection-danger':'');node.dataset.selectionAction=action;if(scope)node.setAttribute('aria-label',scope+label);
   if(action==='all')node.title='选择当前筛选结果的所有分页';root.append(node);
  }
  if(count){count.classList.add('p011-selection-count');root.append(count);}root.append(...extras);host.classList.add('p011-list-toolbar');host.append(root);
  return {root,update(size,busy=false){if(count)count.textContent=`已选 ${size} 项`;all.disabled=busy;clear.disabled=busy||!size;if(remove)remove.disabled=busy||!size;}};
 },
 create({host,onAll,onClear,onDelete,onError=()=>{},extras=[],scope}){
  function control(fn){const node=document.createElement('button');node.type='button';node.onclick=e=>{if(e.isTrusted&&!node.disabled)Promise.resolve().then(fn).catch(onError);};return node;}
  return this.mount({host,all:control(onAll),clear:control(onClear),remove:onDelete?control(onDelete):null,count:document.createElement('span'),extras,scope});
 }
};

const P011Pager={controls:new WeakMap(),update(host,total,page,size,onPage){
 host.classList.add('p011-pager');let select=this.controls.get(host);if(!select){const label=document.createElement('label');label.dataset.pageJump='true';const text=document.createElement('span');text.textContent='跳至 ';label.append(text);select=document.createElement('select');select.setAttribute('aria-label','选择页码');label.append(select);host.append(label);this.controls.set(host,select);}
 const pages=Math.max(1,Math.ceil(total/size));select.replaceChildren(...Array.from({length:pages},(_,i)=>new Option(`第 ${i+1} / ${pages} 页`,String(i))));select.value=String(Math.min(page,pages-1));select.disabled=pages===1;select.onchange=()=>{const n=Number(select.value);if(Number.isInteger(n)&&n>=0&&n<pages)Promise.resolve(onPage(n)).catch(()=>{});};
}};

/* Shared controls are also consumed by separately mounted download editors. */
globalThis.P011Selection=P011Selection;globalThis.P011Pager=P011Pager;
