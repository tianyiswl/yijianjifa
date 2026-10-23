"""Strict native-only V2 API. Transient verification bypasses durable receipts."""
import copy
import hashlib
import json
import re
import threading
from sqlalchemy import select,text
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.publish.deliveries import PublishDeliveryRow,ResourceConsumerRow,canonical,digest
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
from .library_queries import InvalidRequest,CommandRejected,LibraryCommands
from .multi_publish_runtime import AUTH,PAUSE,HostMultiPublishService,authorized_targets,update_authorization
from .multi_publish_dto import safe_fields,summary,REASONS,STAGES
from . import multi_publish_settings as memory

READS={'catalog','candidates','receipt','detail','execution.check','settings.read','presets.list','presets.get','intervention.status','progress.status',
       'platform_options.cached','platform_options.status','inspection.status','inspection.current'}
WRITES={'confirm','execution.start','batch.pause','batch.resume','target.pause','target.resume','target.cancel','target.hide',
    'target.reprepare','target.reschedule','target.reconcile','settings.save','presets.save','presets.delete'}


def identity(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):raise InvalidRequest()


def ids(value):
    if type(value) is not list or not 1<=len(value)<=200 or any(type(v) is not str for v in value) or len(set(value))!=len(value):raise InvalidRequest()
    for v in value:identity(v)


class MultiPublishCommands:
    def __init__(self,engine,runtime,*,declarations,locations):
        self.engine,self.runtime=engine,runtime;self.declarations=declarations;self.locations=locations;self.lock=threading.RLock()
    def validate(self,name,p):
        def exact(keys):
            if set(p)!=set(keys):raise InvalidRequest()
        if name in {'catalog','settings.read','presets.list','progress.status','inspection.current'}:exact(())
        elif name=='intervention.status':
            if p:
                if set(p) not in ({'delivery_id'},{'account_id'}):raise InvalidRequest()
                identity(next(iter(p.values())))
        elif name=='inspection.start':
            exact(('prepared_id',))
            if type(p['prepared_id']) is not str or not re.fullmatch('[a-f0-9]{48}',p['prepared_id']):raise InvalidRequest()
        elif name in {'inspection.status','inspection.cancel'}:exact(('inspection_id',));identity(p['inspection_id'])
        elif name=='inspection.respond':
            if not {'inspection_id','id','action'}<=set(p) or set(p)-{'inspection_id','id','action','code','consent_same_identity'}:raise InvalidRequest()
            identity(p['inspection_id']);identity(p['id'])
            if p['action'] not in {'code','resend_code','pointer','done','cancel','visible','scroll','refresh'}:raise InvalidRequest()
            if 'consent_same_identity' in p and p['consent_same_identity'] is not True:raise InvalidRequest()
        elif name=='candidates':
            if not {'query','offset','limit','account_ids'}<=set(p) or set(p)-{'query','offset','limit','account_ids','advanced','include_published','allow_new_request'}:raise InvalidRequest()
            ids(p['account_ids'])
            if type(p['query']) is not str or len(p['query'])>200 or type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise InvalidRequest()
            for k in ('advanced','include_published','allow_new_request'):
                if k in p and type(p[k]) is not bool:raise InvalidRequest()
        elif name=='prepare':
            if not {'video_ids','targets','common_fields','execution_mode'}<=set(p) or set(p)-{'video_ids','targets','common_fields','execution_mode','scheduled_times','schedule_by_platform','distribution_by_platform','fields_by_video','allow_new_request'}:raise InvalidRequest()
            ids(p['video_ids'])
            if type(p['targets']) is not list or not 1<=len(p['targets'])<=200:raise InvalidRequest()
            for t in p['targets']:
                if type(t) is not dict or not {'account_id','platform'}<=set(t) or set(t)-{'account_id','platform','fields','fields_by_video'}:raise InvalidRequest()
                identity(t['account_id']);PlatformCatalog().get(t['platform'])
        elif name=='confirm':
            exact(('prepared_id','intent_id','consent_partial'));identity(p['intent_id'])
            if type(p['prepared_id']) is not str or not re.fullmatch('[a-f0-9]{48}',p['prepared_id']) or type(p['consent_partial']) is not bool:raise InvalidRequest()
        elif name=='receipt':exact(('request_id',));identity(p['request_id'])
        elif name in {'platform_options.cached','platform_options.refresh'}:exact(('account_id',));identity(p['account_id'])
        elif name=='platform_options.status':exact(('ticket',));identity(p['ticket'])
        elif name=='platform_options.cancel':exact(('ticket',));identity(p['ticket'])
        elif name=='platform_options.respond':
            exact(('ticket','verification_id','action'));identity(p['ticket']);identity(p['verification_id'])
            if p['action'] not in {'show','continue','cancel'}:raise InvalidRequest()
        elif name in {'detail','batch.pause','batch.resume','presets.get'}:exact(('id',));identity(p['id'])
        elif name=='execution.check':exact(('id','delivery_ids'));identity(p['id']);ids(p['delivery_ids'])
        elif name=='execution.start':
            exact(('id','delivery_ids','digest','consent','consent_partial'));identity(p['id']);ids(p['delivery_ids'])
            if p['consent'] is not True or type(p['consent_partial']) is not bool or type(p['digest']) is not str or not re.fullmatch('[a-f0-9]{64}',p['digest']):raise InvalidRequest()
        elif name in {'target.cancel','target.hide','target.pause','target.reconcile'}:exact(('delivery_id',));identity(p['delivery_id'])
        elif name=='target.resume':
            exact(('delivery_id','consent_same_identity'));identity(p['delivery_id'])
            if p['consent_same_identity'] is not True:raise InvalidRequest()
        elif name=='target.reprepare':exact(('delivery_id','fields','reset_fields'));identity(p['delivery_id'])
        elif name=='target.reschedule':exact(('delivery_id','execution_mode','scheduled_at'));identity(p['delivery_id'])
        elif name=='settings.save':exact(('settings',));memory.validate(p['settings'])
        elif name=='presets.save':
            exact(('id','name','settings'))
            if p['id'] is not None:identity(p['id'])
            memory.validate(p['settings'],preset=True)
        elif name=='presets.delete':exact(('ids',));ids(p['ids'])
        elif name in {'intervention.respond','intervention.defer'}:
            if name=='intervention.defer':exact(('id','delivery_id'))
            elif not {'id','delivery_id','action'}<=set(p) or set(p)-{'id','delivery_id','action','code','consent_same_identity'}:raise InvalidRequest()
            identity(p['id']);identity(p['delivery_id'])
            if name.endswith('respond') and p['action'] not in {'code','resend_code','pointer','done','cancel','visible','scroll','refresh'}:raise InvalidRequest()
            if 'consent_same_identity' in p and p['consent_same_identity'] is not True:raise InvalidRequest()
        else:raise InvalidRequest()
    def _resolve_fields(self,value,aid,video_ids):
        from .multi_publish import fields
        value=fields(value)
        cover=value.get('cover')
        if cover is not None and cover!={'kind':'first_frame','frame_ms':0}:raise InvalidRequest()
        for key in ('location','declaration'):
            if key not in value or value[key] is None:continue
            ref=value[key]
            if type(ref) is not dict or set(ref)!={'token','id'}:raise InvalidRequest()
            if key=='location':value[key]=self.locations.resolve(aid,ref)
            else:
                result=self.declarations.resolve(aid,ref['token'],video_ids,ref['id'],{})
                value[key]=result['items'][video_ids[0]]
        return value
    def prepare(self,p,rid):
        p=copy.deepcopy(p)
        # Resolve every account-scoped reference before freezing; public fields
        # are never treated as host option evidence or arbitrary native paths.
        common=p['common_fields'];common_video=p.get('fields_by_video',{})
        from .multi_publish import fields
        common=fields(common)
        if type(common_video) is not dict:raise InvalidRequest()
        common_video={vid:fields(f) for vid,f in common_video.items()}
        for target in p['targets']:
            target['fields']=fields(target.get('fields',{}))
            overrides=target.get('fields_by_video',{})
            if type(overrides) is not dict:raise InvalidRequest()
            target['fields_by_video']={vid:fields(f) for vid,f in overrides.items()}
        # Account-bound metadata references cannot be shared across unrelated
        # accounts. Other common fields keep their actual inheritance layer.
        rich_common={k:v for k,v in common.items() if k in {'location','declaration'}}
        p['common_fields']={k:v for k,v in common.items() if k not in rich_common}
        p['common_fields']=self._resolve_fields(p['common_fields'],p['targets'][0]['account_id'],p['video_ids'])
        p['fields_by_video']={vid:{k:v for k,v in f.items() if k not in {'location','declaration'}} for vid,f in common_video.items()}
        p['fields_by_video']={vid:self._resolve_fields(f,p['targets'][0]['account_id'],[vid]) for vid,f in p['fields_by_video'].items()}
        for target in p['targets']:
            aid=target['account_id'];base={**rich_common,**target.get('fields',{})}
            target['fields']=self._resolve_fields(base,aid,p['video_ids'])
            by_video={}
            for vid in set(common_video)|set(target.get('fields_by_video',{})):
                if vid not in p['video_ids']:raise InvalidRequest()
                inherited_rich={k:v for k,v in common_video.get(vid,{}).items() if k in {'location','declaration'}}
                by_video[vid]=self._resolve_fields({**inherited_rich,**target.get('fields_by_video',{}).get(vid,{})},aid,[vid])
            target['fields_by_video']=by_video
        result=self.runtime.service.prepare(request_id=rid,**p)
        with self.runtime.service._lock:record=self.runtime.service._drafts[result['prepared_id']]
        for target,item in zip(result['targets'],record['snapshot']['items']):
            target['effective_fields']=safe_fields(item['payload']['fields']);target['can_submit']=target['state']=='waiting'
            reason=target.get('hold_reason')
            target['reason_message']=REASONS.get(reason,STAGES.get(target['state'],'需要处理')+'；请检查本目标发布资料。') if reason else ''
        result['summary']=summary(result['targets'],result['excluded_count'])
        return result
    def check(self,p,*,service=None):
        from .multi_publish_dto import detail
        result=detail(service,p['id'],coordinator=self.runtime.coordinator) if service else self.runtime.detail(p['id'])
        if service:
            with session_scope(service.engine) as s:authorized=authorized_targets(s,p['id'])
            for child in result.get('deliveries',[]):child['accepted']=child['id'] in authorized
        mapping={d['id']:d for d in result['deliveries']}
        if not set(p['delivery_ids'])<=set(mapping):raise InvalidRequest()
        selected=[mapping[d] for d in p['delivery_ids']]
        # Snapshot digests stay private. This check digest binds only the exact
        # safe readiness projection and explicit selected children.
        bound={'id':p['id'],'deliveries':selected,'paused':result['paused']}
        return {'id':p['id'],'delivery_ids':p['delivery_ids'],'digest':digest(bound),'summary':summary(selected),
            'available':all(d['can_submit'] for d in selected),'targets':selected,'requires_partial_consent':len(selected)!=len(mapping)}
    def _receipt(self,connection,rid):
        return connection.execute(text('SELECT payload_hash,response_json FROM web_command_receipts WHERE request_id=:id'),{'id':rid}).first()
    def receipt(self,rid):
        with self.engine.connect() as c:row=self._receipt(c,rid)
        if row:
            result=json.loads(row[1])
            return result['data'] if result['ok'] else result.get('rejection')
        return self.runtime.service.receipt(rid)
    def dispatch(self,obj):
        name=obj['operation'].removeprefix('multi_publish.');p=obj['params'];rid=obj['request_id']
        try:
            self.validate(name,p)
            if self.runtime.closed.is_set() and name not in READS:raise CommandRejected('app_closing')
        except (ValueError,KeyError) as error:
            if name not in {'confirm','execution.start'}:raise
            code=str(error) if isinstance(error,CommandRejected) else 'invalid_request'
            request_hash=digest({'operation':'multi_publish.'+name,'params':p})
            with self.lock:
                with self.engine.connect() as c:previous=self._receipt(c,rid)
                if previous:
                    if previous[0]!=request_hash:raise CommandRejected('request_conflict')
                    return json.loads(previous[1])
                # An earlier confirm can have committed its batch before its
                # command receipt was written. Never negate that original intent.
                intent=p.get('intent_id')
                if name=='confirm' and type(intent) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',intent) and self.runtime.service.receipt(intent):
                    return LibraryCommands._error(code,rid)
                return self._rejected(name,rid,request_hash,code=code)
        if name=='prepare':data=self.prepare(p,rid)
        elif name in READS:data=self.read(name,p)
        elif name=='platform_options.refresh':data=self.runtime.platform_options.refresh(p['account_id'])
        elif name=='platform_options.cancel':data=self.runtime.platform_options.cancel(p['ticket'])
        elif name=='platform_options.respond':data=self.runtime.platform_options.respond(p['ticket'],p['verification_id'],p['action'])
        elif name=='inspection.start':
            with self.lock:data=self.runtime.inspections.start(p['prepared_id'])
        elif name=='inspection.cancel':data=self.runtime.inspections.cancel(p['inspection_id'])
        elif name=='inspection.respond':data=self.runtime.inspections.respond(p)
        elif name=='intervention.respond':data=self.runtime.respond(p)
        elif name=='intervention.defer':
            if not any(r['id']==p['id'] and r.get('delivery_id')==p['delivery_id'] for r in self.runtime.interventions()):raise InvalidRequest()
            self.runtime.deferred.add(p['id']);data={'deferred':True,'delivery_id':p['delivery_id']}
        else:return self.write(name,p,rid)
        return {'v':1,'request_id':rid,'ok':True,'data':data}
    def read(self,name,p):
        if name=='inspection.status':return self.runtime.inspections.status(p['inspection_id'])
        if name=='inspection.current':return self.runtime.inspections.current()
        if name=='platform_options.cached':return self.runtime.platform_options.cached(p['account_id'])
        if name=='platform_options.status':return self.runtime.platform_options.status(p['ticket'])
        if name=='catalog':
            items=self.runtime.catalog.list()
            login_platforms=getattr(self,'login_platforms',None)
            login_ready=set(login_platforms()) if callable(login_platforms) else set()
            for item in items:
                try:adapter=self.runtime.catalog.transport_for(item['platform'])
                except ValueError:adapter=None
                capabilities=set(getattr(adapter,'verified_capabilities',()))
                from yijianjifa.features.publish.platforms.official_authorization import pending_descriptor
                pending=pending_descriptor(item['platform'])
                item.update({'login_ready':item['platform'] in login_ready,
                    'ordinary_ready':'ordinary' in capabilities,'schedule_ready':'platform_schedule' in capabilities,
                    'publish_now_ready':'publish_now' in capabilities,'query_ready':'result_query' in capabilities,
                    'field_schema':getattr(adapter,'public_field_schema',pending['field_schema']),'reason':None if adapter else pending['reason']})
            return {'items':items}
        if name=='candidates':
            from .publish_planning import PublishPlanning
            return PublishPlanning(self.engine).candidates(**p)
        if name=='receipt':return self.receipt(p['request_id'])
        if name=='detail':return self.runtime.detail(p['id'])
        if name=='execution.check':return self.check(p)
        if name=='settings.read':return memory.read(self.engine)
        if name=='presets.list':return {'items':memory.presets(self.engine)}
        if name=='presets.get':
            record=next((r for r in memory.presets(self.engine) if r['id']==p['id']),None)
            if record is None:raise KeyError(p['id'])
            return record
        if name=='intervention.status':return self.runtime.intervention_status(**p)
        if name=='progress.status':
            from yijianjifa.features.tasks.service import TaskCenterRow
            with session_scope(self.engine) as s:tasks=[t.id for t in s.scalars(select(TaskCenterRow).where(TaskCenterRow.kind=='multi_publish'))]
            return {'items':[self.runtime.detail(t) for t in tasks]}
        raise InvalidRequest()
    def _rejected(self,name,rid,request_hash,*,code='invalid_request'):
        result=LibraryCommands._error(code,rid)
        proof={'request_id':rid,'operation':name,'acceptance':'rejected','message':'本次操作未受理，请修改后重新检查。'}
        if code!='invalid_request':proof['message']=result['error']['message']
        result['error']['message']=proof['message'];result['rejection']=proof
        with self.engine.begin() as c:
            c.execute(text('INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES (:id,:hash,:response)'),{'id':rid,'hash':request_hash,'response':canonical(result)})
        return result

    def write(self,name,p,rid):
        request_hash=digest({'operation':'multi_publish.'+name,'params':p})
        with self.lock:
            with self.engine.connect() as c:previous=self._receipt(c,rid)
            if previous:
                if previous[0]!=request_hash:raise CommandRejected('request_conflict')
                return json.loads(previous[1])
            if name=='confirm':
                from .multi_publish import SaveNotAccepted
                try:data=self.runtime.service.save(p['prepared_id'],request_id=p['intent_id'],consent_partial=p['consent_partial'])
                except SaveNotAccepted:
                    if self.runtime.service.receipt(p['intent_id']):raise ValueError('original intent already accepted')
                    return self._rejected(name,rid,request_hash)
                result={'v':1,'request_id':rid,'ok':True,'data':data}
                with self.engine.begin() as c:c.execute(text('INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES (:id,:hash,:response)'),{'id':rid,'hash':request_hash,'response':canonical(result)})
                return result
            with self.engine.connect() as c:
                c.exec_driver_sql('BEGIN IMMEDIATE')
                commit_attempted=False
                try:
                    previous=self._receipt(c,rid)
                    if previous:
                        if previous[0]!=request_hash:raise CommandRejected('request_conflict')
                        c.rollback();return json.loads(previous[1])
                    data=self.apply(c,name,p)
                    result={'v':1,'request_id':rid,'ok':True,'data':data}
                    c.execute(text('INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES (:id,:hash,:response)'),{'id':rid,'hash':request_hash,'response':canonical(result)})
                    commit_attempted=True
                    c.commit()
                except (ValueError,KeyError) as error:
                    c.rollback()
                    if name=='execution.start' and not commit_attempted:return self._rejected(name,rid,request_hash,code=str(error) if isinstance(error,CommandRejected) else 'invalid_request')
                    raise
                except BaseException:c.rollback();raise
        if name in {'execution.start','batch.resume'}:self.runtime.wake()
        if name=='target.resume':
            if result['data'].get('verification'):self.runtime.enqueue('resume',p['delivery_id'])
            else:self.runtime.wake()
        if name=='target.reconcile':self.runtime.enqueue('reconcile',p['delivery_id'])
        if name=='target.cancel':
            self.runtime.service.cleanup_resources();self.runtime.cancel_session(p['delivery_id'])
        return result
    def apply(self,c,name,p):
        service=HostMultiPublishService(c,self.runtime.paths,catalog=self.runtime.catalog,workers=self.runtime.registry)
        if name=='execution.start':
            busy=self.runtime.inspections.busy_accounts()
            with session_scope(c) as s:
                if any(s.get(PublishDeliveryRow,did) is not None and s.get(PublishDeliveryRow,did).account_id in busy for did in p['delivery_ids']):
                    raise CommandRejected('inspection_busy')
            check=self.check({'id':p['id'],'delivery_ids':p['delivery_ids']},service=service)
            if check['digest']!=p['digest'] or not check['available']:raise InvalidRequest()
            if check['requires_partial_consent'] and not p['consent_partial']:raise InvalidRequest()
            with session_scope(c) as s:
                update_authorization(s,p['id'],add=p['delivery_ids'])
            return {'id':p['id'],'delivery_ids':p['delivery_ids'],'accepted':True,'stage':'waiting_for_worker','submitted':False}
        if name=='batch.pause':return service.pause(p['id'])
        if name=='batch.resume':return service.resume_task(p['id'])
        if name=='target.cancel':return service.cancel_delivery(p['delivery_id'])
        if name=='target.hide':return service.hide_delivery(p['delivery_id'])
        if name=='target.reprepare':
            with session_scope(c) as s:row,*_=service.deliveries.load(s,p['delivery_id'])
            normalized=self._resolve_fields(p['fields'],row.account_id,[row.content_id])
            result=service.reprepare_delivery(p['delivery_id'],normalized,reset_fields=p['reset_fields'])
            with session_scope(c) as s:update_authorization(s,row.task_id,remove=[row.id])
            return result
        if name=='target.reschedule':
            with session_scope(c) as s:row,*_=service.deliveries.load(s,p['delivery_id'])
            from .multi_publish import PublishScheduleConflict
            try:result=service.reschedule_delivery(p['delivery_id'],execution_mode=p['execution_mode'],scheduled_at=p['scheduled_at'])
            except PublishScheduleConflict as exc:raise CommandRejected('publish_schedule_conflict') from exc
            with session_scope(c) as s:update_authorization(s,row.task_id,remove=[row.id])
            return result
        if name in {'target.resume','target.reconcile'}:
            did=p['delivery_id']
            with session_scope(c) as s:row,*_=service.deliveries.load(s,did);task_id=row.task_id
            from .multi_publish_dto import detail
            if name.removeprefix('target.') not in next(d for d in detail(service,task_id,coordinator=self.runtime.coordinator)['deliveries'] if d['id']==did)['allowed_actions']:raise InvalidRequest()
            adapter=self.runtime.catalog.transport_for_content(row.platform,row.content_type)
            authorizer=getattr(adapter,'authorize_resume',None)
            state=service.delivery_state(did)
            if name=='target.resume':
                if state=='paused_auth' and authorizer:authorizer(did)
                with session_scope(c) as s:
                    update_authorization(s,task_id,add=[did])
                    paused=s.get(Setting,PAUSE+did)
                    if paused:s.delete(paused)
            return {'delivery_id':did,'accepted':True,'stage':'waiting_for_worker','verification':state=='paused_auth'}
        if name=='target.pause':
            from .multi_publish_dto import detail
            with session_scope(c) as s:
                row,*_=service.deliveries.load(s,p['delivery_id'])
                if 'pause' not in next(d for d in detail(service,row.task_id,coordinator=self.runtime.coordinator)['deliveries'] if d['id']==row.id)['allowed_actions']:raise InvalidRequest()
                if s.get(Setting,AUTH+row.task_id) is not None:update_authorization(s,row.task_id,remove=[row.id])
                if s.get(Setting,PAUSE+row.id) is None:s.add(Setting(key=PAUSE+row.id,value_json='true'))
            return {'delivery_id':p['delivery_id'],'paused':True}
        if name=='settings.save':return memory.write(c,p['settings'])
        if name=='presets.save':return memory.save_preset(c,p)
        if name=='presets.delete':
            with session_scope(c) as s:
                for pid in p['ids']:
                    row=s.get(Setting,memory.PREFIX+pid)
                    if row:s.delete(row)
            return {'ids':p['ids']}
        raise InvalidRequest()
    def delete_v2(self,p,rid):
        cancelled_ids=[]
        with session_scope(self.engine) as s:
            task=s.get(TaskCenterRow,p['id'])
            if task is None or task.kind!='multi_publish':return None
        self.validate('detail',p)
        request_hash=digest({'operation':'task.delete','params':p})
        with self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                previous=self._receipt(c,rid)
                if previous:
                    if previous[0]!=request_hash:raise CommandRejected('request_conflict')
                    c.rollback();return json.loads(previous[1])
                with session_scope(c) as s:
                    for row in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==p['id'])):
                        cancelled_ids.append(row.id)
                        _,item,history,attempt,*_=self.runtime.service.deliveries.load(s,row.id)
                        row.cancel_requested=True;row.hidden=True
                        stopped=not attempt.owner_hash or self.runtime.registry.stopped(attempt.owner_hash)
                        if not attempt.final_json and stopped and item.state not in {'published','platform_scheduled','platform_accepted','processing','reviewing','result_unknown','interrupted'}:
                            item.state=history.state='cancelled'
                            for consumer in s.scalars(select(ResourceConsumerRow).where(ResourceConsumerRow.delivery_id==row.id)):consumer.released=True
                    self.runtime.service.deliveries.aggregate(s,p['id'])
                result={'v':1,'request_id':rid,'ok':True,'data':{'id':p['id'],'deleted':True,'retained_evidence':True}}
                c.execute(text('INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES (:id,:hash,:response)'),{'id':rid,'hash':request_hash,'response':canonical(result)})
                c.commit()
            except BaseException:c.rollback();raise
        self.runtime.service.cleanup_resources()
        for did in cancelled_ids:self.runtime.cancel_session(did)
        return result
