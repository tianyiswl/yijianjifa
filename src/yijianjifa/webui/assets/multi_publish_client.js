/* Native-source V2 commands. Persist only receipt identities, never field values or verification data. */
(function(root){'use strict';
 const KEY='p011.multi_publish.intent.v2';
 class Client{
  constructor(api,rid,onStorage){this.api=api;this.rid=rid;this.onStorage=onStorage;this.pending=null;this.inflight=null;this.durable=true;
   try{this.storage=root.localStorage;if(!this.storage)throw Error('storage unavailable');const value=this.storage.getItem(KEY);if(value){const p=JSON.parse(value);if(p&&['confirm','execution.start'].includes(p.operation)&&/^[A-Za-z0-9_-]{1,64}$/.test(p.request_id)&&(!p.batch_id||/^[A-Za-z0-9_-]{1,64}$/.test(p.batch_id)))this.pending=p;}this.storage.setItem(KEY,value||'null');}
   catch{this.durable=false;this.onStorage?.('本机操作标识存储不可用：仅当前窗口可核对原操作；关闭后请在任务记录核对，不要重复创建。');}
  }
  persist(){if(!this.durable)return;try{if(this.pending)this.storage.setItem(KEY,JSON.stringify(this.pending));else this.storage.removeItem(KEY);}catch{this.durable=false;this.onStorage?.('本机操作标识存储不可用：仅当前窗口可核对原操作；关闭后请在任务记录核对。');}}
  send(name,params={},requestId=this.rid()){const raw=JSON.stringify({v:1,request_id:requestId,operation:'multi_publish.'+name,params});if(unescape(encodeURIComponent(raw)).length>16384)return Promise.reject(Error('本次资料超过接口容量，请缩短正文或减少视频后再检查。'));return this.api('multi_publish.'+name,params,requestId);}
  async recover(){
   if(!this.pending)return null;const original=this.pending,receipt=await this.send('receipt',{request_id:original.request_id});
   if(receipt?.acceptance){
    if(receipt.acceptance!=='rejected'||receipt.request_id!==original.request_id||receipt.operation!==original.operation)return null;
    this.pending=null;this.persist();const error=Error(receipt.message||'本次操作未受理，请重新检查。');error.notAccepted=true;throw error;
   }
   if(receipt){this.pending=null;this.persist();}return receipt;
  }
  write(name,params,extra={}){if(this.inflight)return this.inflight;if(this.pending)return Promise.reject(Error('原操作结果尚未核对，请先核对原操作，勿重复创建。'));
   this.pending={operation:name,request_id:this.rid(),...extra};this.persist();const original=this.pending;
   this.inflight=this.send(name,params,original.request_id).then(data=>{this.pending=null;this.persist();return data;}).catch(async error=>{let data;try{data=await this.recover();}catch(recoveryError){if(recoveryError.notAccepted)throw recoveryError;}if(data)return data;throw Error('原操作结果尚未确认：'+error.message+'。请核对原操作。');}).finally(()=>{this.inflight=null;});return this.inflight;
  }
 }
 root.P011MultiClient=Client;root.P011MultiBatches=root.P011MultiBatches||new Set();
})(globalThis);
