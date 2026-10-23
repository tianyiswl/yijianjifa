const P011CommerceForm={
 localIso(date){const offset=-date.getTimezoneOffset(),sign=offset>=0?'+':'-',pad=v=>String(v).padStart(2,'0');return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:00${sign}${pad(Math.floor(Math.abs(offset)/60))}:${pad(Math.abs(offset)%60)}`;},
 declarationPayload(token,batch,items,videoIds){return token?{declarations:{token,batch:batch||null,items:Object.fromEntries(videoIds.filter(id=>Object.hasOwn(items,id)).map(id=>[id,items[id]]))}}:{};},
 musicChoice(mode,ids,token,rows){if(mode==='none')return {mode:'none',ids:[],token:null};if(!['fixed','random'].includes(mode)||!token||!ids.length||ids.length>200||new Set(ids).size!==ids.length||(mode==='fixed'&&ids.length!==1)||ids.some(id=>!rows.some(row=>row.id===id)))throw Error('请刷新当前账号收藏，并按所选模式勾选音乐。');return {mode,ids:[...ids],token};},
 manualStores(videoIds,manual={}){return Object.fromEntries(videoIds.filter(id=>Object.hasOwn(manual,id)).map(id=>[id,manual[id]]));},
 async loadStores(api,params){
  const result=await api('commerce.stores.search',params);
  if(!result.available)return {available:false,reason:result.reason,items:[]};
  if(!Number.isInteger(result.total)||result.total<0||result.total>10000)throw Error('门店结果不完整，请重新查询。');
  const items=[],seen=new Set();
  while(items.length<result.total){const page=await api('commerce.stores.page',{account_id:params.account_id,token:result.token,offset:items.length,limit:100});
   if(page.total!==result.total||!page.items.length||items.length+page.items.length>result.total)throw Error('门店结果不完整，请重新查询。');
   for(const item of page.items){if(seen.has(item.id))throw Error('门店结果不完整，请重新查询。');seen.add(item.id);items.push(item);}
  }
  return {available:true,token:result.token,items};
 },
 textPatches(account,videoIds,batch,items){const base={};for(const key of ['title','body','tags'])if(batch[key]?.enabled)base[key]=batch[key].value;return Object.fromEntries(videoIds.map(id=>[account+':'+id,{...base,...items[id]}]).filter(([,patch])=>Object.keys(patch).length));},
 switchAccount(state,account,confirmed){if(!confirmed||state.account_id===account)return state;return {...state,account_id:account,batch:{},items:{},store_pool:[],store_assignments:{},manual_store_overrides:{},music:{},declarations:{}};}
};
if(typeof module!=='undefined')module.exports=P011CommerceForm;
