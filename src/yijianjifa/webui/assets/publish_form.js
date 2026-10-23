const P011PublishForm={
 handoffSelection(ids,rows){if(!Array.isArray(ids)||ids.some(id=>typeof id!=='string'))throw Error('无效成品选择');const requested=[...new Set(ids)],available=new Map(rows.map(r=>[r.id,r]));return {selected:requested.filter(id=>available.has(id)),excluded:requested.filter(id=>!available.has(id)),includesPublished:requested.some(id=>available.get(id)?.published)};},
 leaveGuard(ask){let pending=false;return async(dirty,action)=>{if(pending)return;pending=true;try{if(!dirty||await ask())await action();}finally{pending=false;}};},
 scheduleCount(perDay,days){const p=Number(perDay),d=Number(days);return Number.isInteger(p)&&p>=1&&p<=100&&Number.isInteger(d)&&d>=1&&d<=14?p*d:null;},
 selectionAfterRefresh(selected,allowed){const set=new Set(allowed);return [...selected].filter(id=>set.has(id));}
};
if(typeof module!=='undefined')module.exports=P011PublishForm;
