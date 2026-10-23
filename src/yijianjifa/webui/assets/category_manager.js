/* Shared category editor for independent library kinds. No file operations. */
async function P011CategoryList(api,kind){const rows=[];let offset=0;for(;;){const data=await api('category.list',{kind,offset,limit:100});rows.push(...data.items);offset+=data.items.length;if(!data.items.length||offset>=data.total)return rows;}}
function P011CategoryManager({overlay,kind,label,api,write}){
 return new Promise(resolve=>{
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  let rows=[],busy=false,changed=false;
  const box=el('section',undefined,'dialog category-manager'),head=el('header',undefined,'dialog-head'),body=el('div',undefined,'dialog-body'),foot=el('footer',undefined,'dialog-foot'),status=el('p',undefined,'category-status'),list=el('div',undefined,'category-list'),newName=el('input');
  box.setAttribute('role','dialog');box.setAttribute('aria-label','管理'+label+'分类');
  const button=(text,fn,cls='small')=>{const b=el('button',text,cls);b.onclick=e=>{if(e.isTrusted&&!busy)fn();};return b;};
  function close(){if(busy)return;document.removeEventListener('keydown',escape);overlay.hidden=true;overlay.replaceChildren();resolve({rows,changed});}
  const escape=e=>{if(e.key==='Escape'){e.preventDefault();close();}};document.addEventListener('keydown',escape);
  head.append(el('h2','管理'+label+'分类'),button('关闭',close));
  body.append(el('p','分类仅整理当前资料；删除分类不会删除内容或改变已确认任务。','muted'));
  const add=el('div',undefined,'category-add');newName.placeholder='输入新分类名称';newName.maxLength=128;newName.setAttribute('aria-label','新分类名称');
  add.append(newName,button('新增分类',()=>mutate('category.create',{kind,name:newName.value},()=>{newName.value='';}),'small primary'));body.append(add,status,list);foot.append(el('span','修改名称后点击该行“保存”。','muted'),button('完成',close,'small primary'));box.append(head,body,foot);overlay.replaceChildren(box);overlay.hidden=false;
  function render(){list.replaceChildren();if(!rows.length)list.append(el('p','暂无分类，可在上方新增。','category-empty'));for(const row of rows){const line=el('div',undefined,'category-edit-row'),input=el('input');input.value=row.name;input.maxLength=128;input.setAttribute('aria-label','分类名称 '+row.name);line.append(input,button('保存',()=>mutate('category.rename',{id:row.id,name:input.value})),button('删除',()=>{const confirm=el('div',undefined,'category-delete-confirm');confirm.append(el('span','删除“'+row.name+'”分类？内容将保留。'),button('取消',render),button('确认删除',()=>mutate('category.delete',{id:row.id}),'small danger'));line.replaceChildren(confirm);},'small danger'));list.append(line);}}
  async function mutate(operation,params,done=()=>{}){if(busy)return;const name=params.name?.trim();if(params.name!==undefined){if(!name){status.textContent='请填写分类名称。';return;}if(rows.some(r=>r.id!==params.id&&r.name===name)){status.textContent='这个分类名称已经存在。';return;}params={...params,name};}busy=true;box.querySelectorAll('button,input').forEach(n=>n.disabled=true);status.textContent='正在保存…';try{await write(operation,params);changed=true;rows=await P011CategoryList(api,kind);done();render();status.textContent='已保存。';}catch(e){status.textContent=e.message;}finally{busy=false;box.querySelectorAll('button,input').forEach(n=>n.disabled=false);}}
  busy=true;status.textContent='正在读取分类…';P011CategoryList(api,kind).then(data=>{rows=data;render();status.textContent='共 '+rows.length+' 个分类';}).catch(e=>{status.textContent=e.message;}).finally(()=>{busy=false;newName.focus();});
 });
}

function P011BatchCategories({overlay,categories,count,initialMode='append',initialIds=[]}){
 return new Promise(resolve=>{
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const chosen=new Set(initialIds),box=el('section',undefined,'dialog category-manager'),head=el('header',undefined,'dialog-head'),body=el('div',undefined,'dialog-body'),foot=el('footer',undefined,'dialog-foot'),mode=el('select'),tip=el('p'),grid=el('div',undefined,'catgrid');
  box.setAttribute('role','dialog');box.setAttribute('aria-label','批量修改分类');head.append(el('h2','批量修改分类'));
  mode.setAttribute('aria-label','分类修改方式');for(const [value,label] of [['append','追加分类'],['replace','替换分类'],['clear','清空分类']]){const option=el('option',label);option.value=value;mode.append(option);}mode.value=initialMode;
  body.append(el('p',`本次修改已选 ${count} 条，包含其他分页中已选的内容。`),el('label','修改方式'),mode,tip,grid);
  const checks=[];for(const c of categories){const label=el('label',undefined,'catitem'),check=el('input');check.type='checkbox';check.checked=chosen.has(c.id);check.setAttribute('aria-label','分类 '+c.name);check.onchange=e=>{if(!e.isTrusted)return;check.checked?chosen.add(c.id):chosen.delete(c.id);render();};label.append(check,el('span',c.name));grid.append(label);checks.push(check);}
  if(!categories.length)grid.append(el('p','暂无分类。可先到“管理分类”新增，或选择清空分类。'));
  const cancel=el('button','取消','small'),save=el('button','确认修改','small primary');
  function close(value){document.removeEventListener('keydown',escape);overlay.hidden=true;overlay.replaceChildren();resolve(value);}
  const escape=e=>{if(e.key==='Escape')close(null);};document.addEventListener('keydown',escape);
  cancel.onclick=e=>{if(e.isTrusted)close(null);};save.onclick=e=>{if(e.isTrusted&&!save.disabled)close({mode:mode.value==='clear'?'replace':mode.value,category_ids:mode.value==='clear'?[]:[...chosen]});};mode.onchange=render;
  function render(){const clear=mode.value==='clear';grid.hidden=clear;checks.forEach(c=>c.disabled=clear);tip.textContent=clear?`将清空所选 ${count} 条的全部分类，内容和原文件保留。`:mode.value==='replace'?`将用下面勾选的分类替换所选 ${count} 条的全部原分类。`:'保留原分类，追加下面勾选的分类；不会重复添加。';save.disabled=!clear&&!chosen.size;save.textContent=clear?'确认清空分类':'确认修改';}
  foot.append(cancel,save);box.append(head,body,foot);overlay.replaceChildren(box);overlay.hidden=false;render();
 });
}
