/* 宿主负责保存选择和发布规则；共用组件负责单选切换及原子批量选择。 */
(function(root){'use strict';
function create({el,trigger,panel,onToggle,onToggleMany,isLocked=()=>false,beforeOpen=()=>{},onError=()=>{}}){
 let alive=true,bulkBusy=false,accounts=[],selected=new Set(),platformLabels={};
 trigger.classList.add('p011-account-picker-button');panel.classList.add('p011-account-picker-menu');
 trigger.setAttribute('aria-haspopup','true');trigger.setAttribute('aria-expanded','false');panel.classList.add('hide');
 const label=platform=>platformLabels[platform]||platform;
 function close(){panel.classList.add('hide');trigger.setAttribute('aria-expanded','false');}
 trigger.onclick=e=>{if(!alive||!e.isTrusted||isLocked())return;const opening=panel.classList.contains('hide');close();if(opening){beforeOpen();panel.classList.remove('hide');trigger.setAttribute('aria-expanded','true');}};
 function outside(e){if(!trigger.contains(e.target)&&!panel.contains(e.target))close();}
 function escape(e){if(e.key==='Escape'&&!panel.classList.contains('hide')){close();trigger.focus();}}
 document.addEventListener('click',outside);document.addEventListener('keydown',escape);
 function choice(accountId,account){
  const row=el('label',undefined,'p011-account-picker-choice daily-account-choice'),check=el('input');check.type='checkbox';check.dataset.account=accountId;check.checked=selected.has(accountId);check.disabled=isLocked()||bulkBusy;
  const text=account?(account.remark||account.nickname||account.id):'保存的账号已不存在：'+accountId+'，请取消选择';
  check.setAttribute('aria-label',account?label(account.platform)+'：'+text:text);
  check.onchange=async e=>{if(!alive||!e?.isTrusted||isLocked()||bulkBusy){check.checked=selected.has(accountId);return;}try{await onToggle(accountId,check.checked,account);}catch(error){check.checked=selected.has(accountId);onError(error);}};
  row.append(check,el('span',text));if(account&&account.login_state!=='valid')row.append(el('small','需要登录'));return row;
 }
 function allState(){const ids=[...new Set([...accounts.map(a=>a.id),...selected])],count=ids.filter(id=>selected.has(id)).length;return {ids,all:ids.length>0&&count===ids.length,partial:count>0&&count<ids.length};}
 function allChoice(){
  const row=el('label',undefined,'p011-account-picker-choice p011-account-picker-all'),check=el('input'),state=allState();check.type='checkbox';check.dataset.accountAll='true';check.checked=state.all;check.indeterminate=state.partial;check.disabled=isLocked()||bulkBusy||!state.ids.length;check.setAttribute('aria-label','全选发布账号');check.setAttribute('aria-checked',state.partial?'mixed':String(state.all));
  check.onchange=async e=>{
   if(!alive||!e?.isTrusted||isLocked()||bulkBusy){const current=allState();check.checked=current.all;check.indeterminate=current.partial;return;}
   const checked=check.checked,ids=checked?[...new Set(accounts.map(a=>a.id))]:allState().ids;bulkBusy=true;update({accounts,selectedIds:selected,platformLabels});
   try{if(onToggleMany)await onToggleMany(ids,checked);else for(const accountId of ids){if(!alive||isLocked())break;if(selected.has(accountId)!==checked)await onToggle(accountId,checked,accounts.find(a=>a.id===accountId));}}
   catch(error){onError(error);}
   finally{bulkBusy=false;if(alive)update({accounts,selectedIds:selected,platformLabels});}
  };
  row.append(check,el('span','全选'),el('small',state.partial?'部分已选':state.all?'已全选':''));return row;
 }
 function update(value){
  if(!alive)return;accounts=value.accounts||[];selected=new Set(value.selectedIds||[]);platformLabels=value.platformLabels||{};
  const chosen=accounts.filter(a=>selected.has(a.id));trigger.textContent=selected.size===1&&chosen.length===1?label(chosen[0].platform)+' · '+(chosen[0].remark||chosen[0].nickname||chosen[0].id):selected.size?'已选 '+selected.size+' 个账号':'请选择发布账号';trigger.disabled=isLocked();
  const scroll=panel.scrollTop;panel.replaceChildren(allChoice());if(!accounts.length&&!selected.size)panel.append(el('p','还没有可用的发布账号，请先到账号管理添加账号。','p011-account-picker-empty'));
  for(const platform of new Set(accounts.map(a=>a.platform))){const group=el('fieldset',undefined,'p011-account-picker-group daily-account-group');group.append(el('legend',label(platform)));for(const account of accounts.filter(a=>a.platform===platform))group.append(choice(account.id,account));panel.append(group);}
  for(const accountId of selected)if(!accounts.some(a=>a.id===accountId))panel.append(choice(accountId));panel.scrollTop=scroll;
  if(isLocked())close();
 }
 return {update,close,dispose(){if(!alive)return;alive=false;close();trigger.onclick=null;document.removeEventListener('click',outside);document.removeEventListener('keydown',escape);}};
}
root.P011AccountPicker={create};
})(globalThis);
