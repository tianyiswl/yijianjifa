(function(root){'use strict';
class CoverBatch{
 constructor(api,progress,wait=()=>new Promise(resolve=>setTimeout(resolve,250))){this.api=api;this.progress=progress;this.wait=wait;this.id=null;this.cancelled=false;this.cancelSent=false;}
 async cancel(){this.cancelled=true;if(this.id&&!this.cancelSent){this.cancelSent=true;try{await this.api('publish.cover.batch.cancel',{id:this.id});}catch(e){this.cancelSent=false;throw e;}}}
 async run(params){let state=await this.api('publish.cover.batch.start',params);this.id=state.id;
  try{for(;;){if(this.cancelled&&!this.cancelSent)await this.cancel();this.progress(state);if(state.state==='completed'){if(this.cancelled)throw Error('已取消本次应用；原封面设置保留。');return state.result;}if(state.state==='cancelled')throw Error('批量封面已取消，本批新副本已撤回，原设置保留。');if(state.state==='failed')throw Error(state.message||'批量封面处理失败，原设置保留。');if(!['running','cancelling'].includes(state.state))throw Error('封面任务状态无效，请重新检查。');await this.wait();state=await this.api('publish.cover.batch.status',{id:this.id});}}
  catch(error){if(!['completed','cancelled','failed'].includes(state.state)){try{await this.cancel();}catch(e){}}throw error;}
 }
}
if(typeof module!=='undefined'&&module.exports)module.exports={CoverBatch};else root.P011CoverBatch=CoverBatch;
})(globalThis);
