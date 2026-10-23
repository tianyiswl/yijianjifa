/* One user intent retains its request ID until acknowledged. Private world only. */
(function(root){'use strict';
class WriteIntent{
 constructor(send,makeId){this.send=send;this.makeId=makeId;this.pending=null;this.inflight=null;}
 run(operation,params){
  if(this.inflight)return this.inflight;
  const body=JSON.stringify({operation,params});
  if(!this.pending||this.pending.body!==body)this.pending={body,id:this.makeId()};
  const id=this.pending.id;
  this.inflight=Promise.resolve().then(()=>this.send(operation,params,id)).then(result=>{this.pending=null;return result;}).finally(()=>{this.inflight=null;});
  return this.inflight;
 }
}
if(typeof module!=='undefined'&&module.exports)module.exports={WriteIntent};else root.P011WriteIntent=WriteIntent;
})(globalThis);
