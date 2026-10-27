"""Local V2 multi-target preparation, atomic receipts, and target controls.

Public preparation is an opaque handle; client DTOs never authorize a snapshot.
Platform access is only through host-injected PlatformCatalog adapters.
"""
from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import uuid

from sqlalchemy import select

from yijianjifa.storage.db import session_scope
from .finished_covers import FinishedCovers
from .bilibili_static_catalog import valid_category_choice, valid_statement_choice
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from yijianjifa.features.publish.history import PublishHistoryRepository, PublishHistoryRow, DuplicatePublishBlocked
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.features.publish.deliveries import (
    PublishDeliveries, PublishDeliveryRow, DeliveryAttemptRow, PublishResourceRow,
    ResourceConsumerRow, PublishBatchReceiptRow, write_session, canonical, digest,
)


def identifier(value, name):
    if type(value) is not str or not 1<=len(value)<=128 or not value.strip():
        raise ValueError('invalid '+name)
    return value


PUBLISH_FIELDS={'title','body','tags','category_id','category_name','category_mode',
    'category_choice','creation_statement_choice','creation_statement_id','creation_statement_name','content_authorization','reprint_source',
    'declaration','location','cover','copyright','source','channels_short_title',
    'content_declaration_choice','channels_original'}


def fields(value):
    if type(value) is not dict or set(value)-PUBLISH_FIELDS:
        raise ValueError('unsupported publish fields')
    value=copy.deepcopy(value)
    for key in ('title','body','tags'):
        if key in value and (type(value[key]) is not str or len(value[key])>10000):
            raise ValueError('invalid '+key)
    if 'category_id' in value and (type(value['category_id']) is not int or value['category_id']<=0):
        raise ValueError('invalid category_id')
    if 'copyright' in value and (type(value['copyright']) not in {str,int} or value['copyright'] not in {'original','repost',1,2}):raise ValueError('invalid copyright')
    if 'source' in value and (type(value['source']) is not str or len(value['source'])>2000):raise ValueError('invalid source')
    for key in ('category_name','creation_statement_name','reprint_source'):
        if key in value and (type(value[key]) is not str or len(value[key])>255):raise ValueError('invalid '+key)
    if 'category_mode' in value and (type(value['category_mode']) is not str or value['category_mode'] not in {'legacy','human'}):raise ValueError('invalid category_mode')
    if 'creation_statement_id' in value and type(value['creation_statement_id']) is not int:raise ValueError('invalid creation_statement_id')
    if 'category_choice' in value and not valid_category_choice(value['category_choice']):raise ValueError('invalid category_choice')
    if 'creation_statement_choice' in value and not valid_statement_choice(value['creation_statement_choice']):raise ValueError('invalid creation_statement_choice')
    for choice,concrete in (('category_choice',('category_id','category_name','category_mode')),
                            ('creation_statement_choice',('creation_statement_id','creation_statement_name'))):
        if choice in value and any(key in value for key in concrete):raise ValueError('conflicting publish selection')
    if 'content_authorization' in value and type(value['content_authorization']) is not bool:raise ValueError('invalid content_authorization')
    if 'channels_short_title' in value and (type(value['channels_short_title']) is not str or len(value['channels_short_title'])>16):raise ValueError('invalid channels_short_title')
    if 'content_declaration_choice' in value:
        from .kuaishou_declaration_catalog import valid_choice as kuaishou_choice
        from .channels_declaration_catalog import valid_choice as channels_choice
        if not (kuaishou_choice(value['content_declaration_choice']) or channels_choice(value['content_declaration_choice'])):
            raise ValueError('invalid content_declaration_choice')
    if 'channels_original' in value and type(value['channels_original']) is not bool:raise ValueError('invalid channels_original')
    # These richer fields need a host implementation that validates and freezes
    # them; preserve the request for a local hold, never silently strip fields.
    canonical(value)
    return value


def merge_fields(base,override):
    """A more specific selector replaces the inherited selector as one unit."""
    override=fields(override);result=copy.deepcopy(base)
    for choice,concrete in (('category_choice',('category_id','category_name','category_mode')),
                            ('creation_statement_choice',('creation_statement_id','creation_statement_name'))):
        if choice in override:
            for key in concrete:result.pop(key,None)
        elif any(key in override for key in concrete):result.pop(choice,None)
    result.update(override)
    return result


class SaveNotAccepted(ValueError):
    """Local rejection before commit; never used for an ambiguous commit."""


class PublishScheduleConflict(ValueError):
    """Another local target reserves this account's exact scheduled time."""


class MultiPublishService:
    def __init__(self, engine, paths, *, catalog=None, workers=None, read_only=False):
        self.engine,self.paths=engine,paths
        self.read_only=read_only
        self.catalog=catalog or PlatformCatalog()
        self.deliveries=PublishDeliveries(engine,workers=workers)
        self._drafts={}
        self._lock=threading.RLock()
        self._execution_lock=threading.Lock()

    def _hold(self, payload):
        if payload.get('content_type') == 'image_post':
            return self._hold_image_post(payload)
        platform=payload['platform']
        self.catalog.get(platform)
        try:adapter=self.catalog.transport_for(platform)
        except ValueError:adapter=None
        creator_statement='creation_statement_id' in getattr(adapter,'public_field_schema',{})
        schema=getattr(adapter,'public_field_schema',{})
        if 'channels_original' in payload['fields'] and (platform!='channels' or schema.get('channels_original',{}).get('type')!='boolean'):
            return 'waiting_configuration','platform_fields_require_validation'
        if 'content_declaration_choice' in payload['fields']:
            choices=schema.get('content_declaration_choice',{}).get('choices',[])
            if platform not in {'kuaishou','channels'} or not any(type(row) is dict and row.get('choice')==payload['fields']['content_declaration_choice'] for row in choices):
                return 'waiting_configuration','platform_fields_require_validation'
        if platform!='channels' and 'channels_short_title' in payload['fields']:
            return 'waiting_configuration','platform_fields_require_validation'
        if platform=='bilibili' and not (payload['fields'].get('category_id') or payload['fields'].get('category_choice')):
            return 'waiting_configuration', 'category_required'
        if platform=='bilibili' and not creator_statement:
            if 'copyright' not in payload['fields']:return 'waiting_configuration','copyright_required'
            if payload['fields']['copyright'] in {'repost',2} and not payload['fields'].get('source','').strip():
                return 'waiting_configuration','repost_source_required'
        elif platform!='bilibili' and any(key in payload['fields'] for key in ('category_id','category_name','category_mode','category_choice',
                'creation_statement_choice','creation_statement_id','creation_statement_name','content_authorization','reprint_source','copyright','source')):
            return 'waiting_configuration','platform_fields_require_validation'
        cover=payload['fields'].get('cover')
        if cover is not None and not (type(cover) is dict and cover.get('kind')=='first_frame' and
                cover.get('frame_ms')==0 and cover.get('missing') is not True and
                (set(cover)<={'kind','frame_ms'} or cover==payload.get('source_cover'))):
            return 'waiting_configuration','cover_requires_validation'
        if any(key in payload['fields'] for key in ('location','declaration')):
            return 'waiting_configuration', 'platform_fields_require_validation'
        try:adapter=self.catalog.transport_for(platform)
        except ValueError:return 'waiting_adapter','adapter_not_integrated'
        if (not callable(getattr(adapter,'validate_target',None)) or
            not callable(getattr(adapter,'submit',None)) or not callable(getattr(adapter,'reconcile',None))):
            return 'waiting_adapter','adapter_contract_unverified'
        verified=getattr(adapter,'verified_capabilities',frozenset())
        needed={'ordinary', 'platform_schedule' if payload['execution_mode']=='platform_schedule' else 'publish_now'}
        if not needed<=set(verified):return 'waiting_adapter','platform_capability_unverified'
        account=AccountService(self.engine).get(payload['account_id'])
        if account.login_state!='valid':return 'waiting_configuration','account_verification_required'
        return 'waiting',None

    def _hold_image_post(self, payload):
        if payload.get('platform') != 'douyin':
            return 'waiting_configuration', 'platform_fields_invalid'
        try:
            adapter = self.catalog.transport_for_content('douyin', 'image_post')
        except ValueError:
            return 'waiting_adapter', 'adapter_not_integrated'
        if any(not callable(getattr(adapter, method, None)) for method in ('validate_target','submit','reconcile')):
            return 'waiting_adapter', 'adapter_contract_unverified'
        needed = {'ordinary', 'platform_schedule' if payload['execution_mode'] == 'platform_schedule' else 'publish_now'}
        if not needed <= set(getattr(adapter, 'verified_capabilities', ())):
            return 'waiting_adapter', 'platform_capability_unverified'
        if payload['execution_mode'] not in {'publish_now', 'platform_schedule'}:
            return 'waiting_configuration', 'native_schedule_not_supported'
        readiness = getattr(adapter, 'plan_readiness', None)
        if callable(readiness):
            reason = readiness(payload)
            if reason:
                return 'waiting_configuration', reason
        account = AccountService(self.engine).get(payload['account_id'])
        if account.login_state != 'valid':
            return 'waiting_configuration', 'account_verification_required'
        return 'waiting', None

    def prepare(self, *, request_id, video_ids, targets, common_fields, execution_mode, scheduled_at=None,
                scheduled_times=None, schedule_by_platform=None, distribution_by_platform=None, fields_by_video=None, now=None,allow_new_request=False,
                inherit_account_settings=True):
        if type(allow_new_request) is not bool:raise ValueError('invalid publication intent')
        if type(inherit_account_settings) is not bool:raise ValueError('invalid account defaults inheritance')
        identifier(request_id,'request_id')
        now=now or datetime.now(timezone.utc)
        if now.tzinfo is None:raise ValueError('timezone required')
        if type(video_ids) is not list or not 1<=len(video_ids)<=100 or len(set(video_ids))!=len(video_ids):
            raise ValueError('invalid video selection')
        if type(targets) is not list or not 1<=len(targets)<=200:
            raise ValueError('invalid targets')
        common=fields(common_fields)
        def video_fields(value):
            if value is None:return {}
            if type(value) is not dict or set(value)-set(video_ids):raise ValueError('unselected video in fields_by_video')
            return {vid:fields(metadata) for vid,metadata in value.items()}
        public_video_fields=video_fields(fields_by_video)
        modes={'publish_now','platform_schedule','software_schedule'}
        if execution_mode not in modes:raise ValueError('invalid execution mode')
        def slots(value):
            if value is None:return {}
            if type(value) is not dict or set(value)-set(video_ids):raise ValueError('invalid scheduled video slots')
            result={}
            for vid,raw in value.items():
                try:time=datetime.fromisoformat(raw)
                except (TypeError,ValueError):raise ValueError('invalid scheduled_at') from None
                if time.tzinfo is None:raise ValueError('scheduled timezone required')
                result[vid]=time.astimezone(timezone.utc).isoformat()
            return result
        common_slots=slots(scheduled_times)
        if scheduled_at is not None:
            if common_slots:raise ValueError('choose scheduled_at or scheduled_times')
            common_slots=slots({vid:scheduled_at for vid in video_ids})
        platform_schedules=schedule_by_platform or {}
        distributions=distribution_by_platform or {}
        if type(platform_schedules) is not dict or type(distributions) is not dict:raise ValueError('invalid platform settings')
        for platform,config in platform_schedules.items():
            self.catalog.get(platform)
            if type(config) is not dict or set(config)-{'execution_mode','scheduled_times'} or config.get('execution_mode') not in modes:
                raise ValueError('invalid platform schedule')
            slots(config.get('scheduled_times'))
        for platform,distribution in distributions.items():
            self.catalog.get(platform)
            if distribution not in {'split','whole_batch'}:raise ValueError('invalid platform distribution')
        accounts=[]
        normalized=[]
        from .account_publish_settings import AccountPublishSettings
        configured=getattr(self,'account_publish_settings',None)
        account_defaults=configured.for_engine(self.engine) if configured is not None else AccountPublishSettings(self.engine,catalog=self.catalog)
        for target in targets:
            if type(target) is not dict or set(target)-{'account_id','platform','fields','fields_by_video'}:raise ValueError('invalid target fields')
            aid=identifier(target.get('account_id'),'account_id')
            self.catalog.get(target.get('platform'))
            try:account=AccountService(self.engine).get(aid)
            except KeyError:raise ValueError('target unavailable') from None
            if account.platform!=target['platform']:raise ValueError('target platform mismatch')
            if aid in [a['id'] for a in accounts]:raise ValueError('duplicate targets')
            captured={'id':aid,'platform':account.platform,'platform_user_id':account.platform_user_id}
            capture=getattr(self,'capture_account',None)
            if callable(capture):captured.update(capture(account))
            accounts.append(captured)
            normalized.append({'account_id':aid,'platform':account.platform,'fields':fields(target.get('fields',{})),
                'account_defaults':account_defaults.resolve_fields(aid,video_ids=video_ids) if inherit_account_settings else {},
                'fields_by_video':video_fields(target.get('fields_by_video'))})
        # Shared Task1 parser validates stable identities, including re-added accounts.
        PublishHistoryRepository(self.engine).states_for_targets([a['id'] for a in accounts])
        videos=[]
        for video_id in video_ids:
            identifier(video_id,'video_id')
            video=FinishedVideoService(self.engine).get(video_id)
            if video is None:raise ValueError('video unavailable')
            path=video.effective_path
            if not path.is_file() or sha256_file(path)!=video.content_hash:raise ValueError('video changed or missing')
            cover_reader=FinishedCovers(self.engine,self.paths)
            cover,cover_path=cover_reader.read(video.id)
            frozen_cover={key:cover[key] for key in ('kind','frame_ms','missing') if key in cover}
            source_cover=FinishedCovers._saved(self.engine,video.id)
            if source_cover is not None:frozen_cover['sha256']=json.loads(source_cover)['sha256']
            if cover_path is not None and sha256_file(cover_path)!=frozen_cover['sha256']:raise ValueError('cover changed during prepare')
            videos.append({'id':video.id,'path':str(path),'content_hash':video.content_hash,'duration_ms':video.duration_ms,'title':video.title,'body':video.body,'tags':video.tags,
                'cover':frozen_cover,'source_cover':source_cover,'cover_path':str(cover_path) if cover_path else None})
        groups={}
        for target in normalized:groups.setdefault(target['platform'],[]).append(target)
        if (set(platform_schedules)|set(distributions))-set(groups):raise ValueError('unselected platform settings')
        items=[]
        used_slots=set()
        selected_content=set()
        for index,video in enumerate(videos):
            for platform,group in groups.items():
                selected=group if distributions.get(platform,'split')=='whole_batch' else [group[index%len(group)]]
                config=platform_schedules.get(platform)
                mode=config['execution_mode'] if config else execution_mode
                times=slots(config.get('scheduled_times')) if config else common_slots
                time=times.get(video['id'])
                if mode=='publish_now' and times:raise ValueError('publish_now cannot contain schedule')
                if mode!='publish_now' and time is None:raise ValueError('every scheduled video requires a time')
                for target in selected:
                    account=next(a for a in accounts if a['id']==target['account_id'])
                    if time and (account['id'],time) in used_slots:raise ValueError('same account videos need distinct times')
                    if time:used_slots.add((account['id'],time))
                    base_metadata={key:copy.deepcopy(video[key]) for key in ('title','body','tags','cover')}
                    base_metadata=merge_fields(base_metadata,target['account_defaults'])
                    base_metadata=merge_fields(merge_fields(base_metadata,common),public_video_fields.get(video['id'],{}))
                    if platform=='bilibili':
                        try:target_schema=self.catalog.transport_for(platform).public_field_schema
                        except (ValueError,AttributeError):target_schema={}
                        if target_schema.get('category_choice',{}).get('choices_source')=='builtin':
                            from .bilibili_static_catalog import with_defaults
                            base_metadata=with_defaults(base_metadata)
                    overrides=merge_fields(target['fields'],target['fields_by_video'].get(video['id'],{}))
                    metadata=merge_fields(base_metadata,overrides)
                    if not metadata.get('title','').strip():raise ValueError('title required')
                    payload={'version':2,'platform':platform,'account_id':account['id'],'platform_user_id':account['platform_user_id'],
                        'video_id':video['id'],'video_hash':video['content_hash'],'source_cover':copy.deepcopy(video['cover']),'fields':metadata,'base_fields':base_metadata,'platform_overrides':copy.deepcopy(overrides),'execution_mode':mode,'scheduled_at':time}
                    if allow_new_request:payload['allow_new_request']=True
                    if 'session_fingerprint' in account:payload['session_fingerprint']=account['session_fingerprint']
                    content_key=(payload['platform'],payload['platform_user_id'],payload['video_hash'])
                    if content_key in selected_content:raise ValueError('duplicate content for selected target')
                    selected_content.add(content_key)
                    state,reason=self._hold(payload)
                    items.append({'payload':payload,'state':state,'hold_reason':reason})
        snapshot={'version':2,'kind':'multi_publish','request_id':request_id,'accounts':accounts,'videos':videos,'items':items}
        request_digest=digest({'version':2,'request_id':request_id,'accounts':accounts,
            'items':[i['payload'] for i in items]})
        previous=self._receipt_row(request_id)
        requested_quantity=len(items)
        excluded=[]
        if previous:
            if previous.request_digest!=request_digest:raise ValueError('request_id reused with changed content')
            saved=json.loads(previous.receipt_json)
            excluded=saved.get('excluded_targets',[])
            excluded_keys={(e['video_id'],e['account_id']) for e in excluded}
            items=[i for i in items if (i['payload']['video_id'],i['payload']['account_id']) not in excluded_keys]
        else:
            history=PublishHistoryRepository(self.engine)
            states=history.states_for_targets([a['id'] for a in accounts])
            eligible=[]
            for item in items:
                payload=item['payload']
                blocking=history.target_blocking_states(states.get((payload['video_hash'],payload['account_id']),set()),allow_new_request=allow_new_request)
                if blocking:
                    excluded.append({'video_id':payload['video_id'],'account_id':payload['account_id'],'platform':payload['platform'],
                        'reason':'already_published' if blocking=={'published'} else 'existing_target_unresolved',
                        'states':sorted(blocking),'history':self._history_links(payload)})
                else:eligible.append(item)
            items=eligible
            if not items:raise DuplicatePublishBlocked('all selected targets have existing or unverified history')
        snapshot['items']=items
        snapshot['requested_quantity']=requested_quantity
        snapshot['excluded_targets']=excluded
        token=secrets.token_hex(24)
        with self._lock:
            self._drafts={k:v for k,v in self._drafts.items() if v['expires']>now}
            while len(self._drafts)>=16:self._drafts.pop(next(iter(self._drafts)))
            self._drafts[token]={'expires':now+timedelta(minutes=20),'snapshot':snapshot,'digest':request_digest}
        return {'prepared_id':token,'request_id':request_id,'quantity':len(items),'requested_quantity':requested_quantity,
                'excluded_count':len(excluded),'excluded_targets':excluded,'issues':copy.deepcopy(excluded),'requires_partial_consent':bool(excluded),'submitted':False,
                'expires_at':(now+timedelta(minutes=20)).isoformat(),
                'targets':[{'platform':i['payload']['platform'],'account_id':i['payload']['account_id'],'video_id':i['payload']['video_id'],
                    'state':i['state'],'hold_reason':i['hold_reason'],'execution_mode':i['payload']['execution_mode'],
                    'scheduled_at':i['payload']['scheduled_at']} for i in items]}

    def _history_links(self, payload):
        result=[]
        with session_scope(self.engine) as s:
            cache={}
            target=(payload['platform'],payload['platform_user_id'])
            for row in s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.video_hash==payload['video_hash'])):
                identity=PublishHistoryRepository._frozen_history_identity(s,row,cache)
                if identity is None:
                    account=s.get(AccountRow,row.account_id)
                    if account and account.platform_user_id:identity=(account.platform,account.platform_user_id)
                if identity==target or identity is None:
                    result.append({'task_id':row.task_id,'history_id':row.id,'state':row.state})
        return result

    def _receipt_row(self, request_id):
        with session_scope(self.engine) as s:return s.get(PublishBatchReceiptRow,request_id)

    def receipt(self, request_id):
        identifier(request_id,'request_id')
        row=self._receipt_row(request_id)
        return json.loads(row.receipt_json) if row else None

    def save(self, prepared_id, *, request_id, consent_partial=False, now=None):
        identifier(request_id,'request_id')
        if type(prepared_id) is not str or len(prepared_id)!=48:raise SaveNotAccepted('invalid preparation handle')
        now=now or datetime.now(timezone.utc)
        with self._lock:record=copy.deepcopy(self._drafts.get(prepared_id))
        previous=self._receipt_row(request_id)
        if previous:
            # A known original commit cannot become a negative receipt merely
            # because its ephemeral preview expired or the host restarted.
            if record is None or previous.request_digest!=record['digest']:raise ValueError('original intent already accepted')
            return json.loads(previous.receipt_json)
        if record is None or record['expires']<=now:raise SaveNotAccepted('preparation expired')
        snapshot=record['snapshot']
        if type(consent_partial) is not bool:raise SaveNotAccepted('invalid partial consent')
        if snapshot.get('excluded_targets') and not consent_partial:raise SaveNotAccepted('explicit consent_partial required for partial plan')
        if snapshot['request_id']!=request_id:raise SaveNotAccepted('preparation request mismatch')
        root=self.paths.data_dir/'publish_resources'
        root.mkdir(parents=True,exist_ok=True)
        directory=Path(tempfile.mkdtemp(prefix='v2-',dir=root))
        copies={}
        cover_copies={}
        commit_attempted=False
        try:
            used_videos={i['payload']['video_id'] for i in snapshot['items']}
            for index,video in enumerate(snapshot['videos']):
                if video['id'] not in used_videos:continue
                source=Path(video['path'])
                target=directory/(str(index)+source.suffix)
                shutil.copyfile(source,target)
                if sha256_file(target)!=video['content_hash']:raise ValueError('video changed while freezing')
                copies[video['id']]=target
                if video['cover_path']:
                    cover_target=directory/(str(index)+'-cover.png')
                    shutil.copyfile(video['cover_path'],cover_target)
                    if sha256_file(cover_target)!=video['cover']['sha256']:raise ValueError('cover changed while freezing')
                    cover_copies[video['id']]=cover_target
            with self.engine.connect() as connection:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    existing=MultiPublishService(connection,self.paths)._receipt_row(request_id)
                    if existing:
                        if existing.request_digest!=record['digest']:raise ValueError('request_id reused with changed content')
                        connection.rollback();shutil.rmtree(directory)
                        return json.loads(existing.receipt_json)
                    # Revalidate metadata and source identity under the write lock.
                    for account in snapshot['accounts']:
                        actual=AccountService(connection).get(account['id'])
                        if (actual.platform,actual.platform_user_id)!=(account['platform'],account['platform_user_id']):raise ValueError('target identity changed')
                        if 'session_fingerprint' in account:
                            import hashlib
                            if hashlib.sha256((actual.session_ref or '').encode()).hexdigest()!=account['session_fingerprint']:raise ValueError('target session changed before freeze')
                    for video in snapshot['videos']:
                        actual=FinishedVideoService(connection).get(video['id'])
                        if actual is None or actual.content_hash!=video['content_hash'] or sha256_file(actual.effective_path)!=video['content_hash']:
                            raise ValueError('video changed before save')
                        if FinishedCovers._saved(connection,video['id'])!=video['source_cover']:
                            raise ValueError('source cover changed before save')
                        cover,cover_path=FinishedCovers(connection,self.paths).read(video['id'])
                        if bool(cover_path)!=bool(video['cover_path']) or (cover_path and sha256_file(cover_path)!=video['cover']['sha256']):
                            raise ValueError('source cover changed before save')
                    history=PublishHistoryRepository(connection)
                    for item in snapshot['items']:
                        p=item['payload'];history.assert_target_can_create(p['video_hash'],p['account_id'],allow_new_request=p.get('allow_new_request') is True)
                    task_id=str(uuid.uuid4())
                    frozen={'version':2,'kind':'multi_publish','request_id':request_id,'accounts':snapshot['accounts'],'quantity':len(snapshot['items']),'requested_quantity':snapshot['requested_quantity'],
                        'excluded_count':len(snapshot['excluded_targets']),'consent_partial':consent_partial}
                    receipt={'id':task_id,'request_id':request_id,'state':'waiting','quantity':len(snapshot['items']),'submitted':False,'version':2,'requested_quantity':snapshot['requested_quantity'],
                        'excluded_count':len(snapshot['excluded_targets']),'excluded_targets':snapshot['excluded_targets']}
                    with session_scope(connection) as s:
                        s.add(PublishTaskRow(id=task_id,platform='mixed',state='waiting',snapshot_json=canonical(frozen)))
                        s.add(TaskCenterRow(id=task_id,kind='multi_publish',name='多平台发布 · 本地安排',state='waiting',snapshot_json=canonical(frozen)))
                        s.flush()
                        resources={}
                        cover_resources={}
                        for video in snapshot['videos']:
                            if video['id'] not in used_videos:continue
                            resource=PublishResourceRow(id=str(uuid.uuid4()),task_id=task_id,path=str(copies[video['id']]),content_hash=video['content_hash'],cleaned=False)
                            s.add(resource);resources[video['id']]=resource
                            if video['id'] in cover_copies:
                                cover_resource=PublishResourceRow(id=str(uuid.uuid4()),task_id=task_id,path=str(cover_copies[video['id']]),content_hash=video['cover']['sha256'],cleaned=False)
                                s.add(cover_resource);cover_resources[video['id']]=cover_resource
                        for position,item in enumerate(snapshot['items']):
                            p=copy.deepcopy(item['payload']);resource=resources[p['video_id']]
                            p['resource_id']=resource.id;p['video_path']=resource.path
                            cover_resource=cover_resources.get(p['video_id'])
                            if cover_resource:
                                source_cover=p['source_cover']
                                frozen_cover={**source_cover,'path':cover_resource.path,'resource_id':cover_resource.id}
                                if p['fields'].get('cover')==source_cover:p['fields']['cover']=copy.deepcopy(frozen_cover)
                                if p['base_fields'].get('cover')==source_cover:p['base_fields']['cover']=copy.deepcopy(frozen_cover)
                                p['source_cover']=frozen_cover
                            delivery_id,item_id,history_id=[str(uuid.uuid4()) for _ in range(3)]
                            s.add(TaskItemRow(id=item_id,task_id=task_id,item_key=delivery_id,state=item['state'],detail_json=canonical({'delivery_id':delivery_id,'video_id':p['video_id'],'account_id':p['account_id'],'platform':p['platform']})))
                            # Existing stable history is already checked, so a new target
                            # is attempt 1. Reprepare keeps this exact history_id.
                            prior=list(s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.video_hash==p['video_hash'],PublishHistoryRow.account_id==p['account_id'])))
                            s.add(PublishHistoryRow(id=history_id,video_hash=p['video_hash'],account_id=p['account_id'],attempt=max((h.attempt for h in prior),default=0)+1,state=item['state'],task_id=task_id))
                            s.flush()
                            s.add(PublishDeliveryRow(id=delivery_id,task_id=task_id,item_id=item_id,history_id=history_id,platform=p['platform'],account_id=p['account_id'],platform_user_id=p['platform_user_id'],video_id=p['video_id'],video_hash=p['video_hash'],snapshot_json=canonical(p),request_digest=digest(p),execution_mode=p['execution_mode'],scheduled_at=p['scheduled_at'],attempt=1,position=position,hidden=False,cancel_requested=False))
                            s.flush()
                            s.add(DeliveryAttemptRow(id=str(uuid.uuid4()),delivery_id=delivery_id,number=1,snapshot_json=canonical(p),request_digest=digest(p)))
                            s.add(ResourceConsumerRow(id=str(uuid.uuid4()),resource_id=resource.id,delivery_id=delivery_id,released=False))
                            if cover_resource:s.add(ResourceConsumerRow(id=str(uuid.uuid4()),resource_id=cover_resource.id,delivery_id=delivery_id,released=False))
                        s.add(PublishBatchReceiptRow(request_id=request_id,request_digest=record['digest'],task_id=task_id,receipt_json=canonical(receipt)))
                        s.flush();self.deliveries.aggregate(s,task_id)
                    commit_attempted=True
                    connection.commit()
                    return receipt
                except BaseException:
                    connection.rollback();raise
        except BaseException as error:
            if not commit_attempted:
                shutil.rmtree(directory,ignore_errors=True)
                if isinstance(error,(ValueError,KeyError)):raise SaveNotAccepted(str(error)) from error
            raise

    def get_task(self, task_id):
        from yijianjifa.features.publish.outcomes import COMPLETED_DELIVERY_STATES
        with session_scope(self.engine) as s:
            task=s.get(PublishTaskRow,task_id)
            if task is None:raise KeyError(task_id)
            frozen=json.loads(task.snapshot_json)
            if frozen.get('version')!=2:
                return {'id':task_id,'legacy_version':1,'state':task.state,'read_only':True}
            center=s.get(TaskCenterRow,task_id)
            children=[]
            for row in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==task_id).order_by(PublishDeliveryRow.position)):
                item=s.get(TaskItemRow,row.item_id)
                p=json.loads(row.snapshot_json)
                attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id,DeliveryAttemptRow.number==row.attempt))
                reason=None if item.state=='waiting' else item.state
                if item.state in {'waiting_adapter','waiting_configuration'} and not self.read_only:
                    _,reason=self._hold(p)
                    reason=reason or 'reprepare_required'
                if reason is None and center.state=='paused':reason='batch_paused'
                if reason is None and row.cancel_requested:reason='cancel_requested'
                blocker=None
                if reason is None:
                    from yijianjifa.features.publish.deliveries import account_blocker, account_blocker_reason
                    blocker=account_blocker(s,row)
                    if blocker:reason=account_blocker_reason(blocker)
                current=s.get(AccountRow,row.account_id)
                if reason is None and (current is None or current.login_state!='valid' or (current.platform,current.platform_user_id)!=(row.platform,row.platform_user_id)):
                    reason='account_verification_required'
                if reason is None and not self.read_only:
                    hold,hold_reason=self._hold(p)
                    if hold!='waiting':reason=hold_reason
                if reason is None and row.execution_mode=='software_schedule':
                    delta=(datetime.now(timezone.utc)-datetime.fromisoformat(row.scheduled_at)).total_seconds()
                    if delta<0:reason='software_schedule_not_due'
                    elif delta>60:reason='software_schedule_missed'
                if reason is None:
                    try:
                        PublishHistoryRepository.assert_no_other_content_history(s,row.content_type,row.content_hash,row.account_id,
                            history_id=row.history_id,identity=(row.platform,row.platform_user_id),allow_new_request=p.get('allow_new_request') is True)
                    except ValueError:reason='existing_target_unresolved'
                if reason is None and self.read_only:
                    # 空目录只能读取持久状态，不能据此断言当前客户端未接入通道。
                    reason='readiness_not_checked'
                result=json.loads(attempt.result_json) if attempt and attempt.result_json else {}
                children.append({'id':row.id,'item_id':row.item_id,'history_id':row.history_id,'platform':row.platform,'account_id':row.account_id,
                    'video_id':row.video_id,'content_type':row.content_type,'content_id':row.content_id,'title':p['fields']['title'],'state':item.state,'execution_mode':row.execution_mode,
                    'scheduled_at':row.scheduled_at,'attempt':row.attempt,'hidden':row.hidden,'cancel_requested':row.cancel_requested,
                    'final_started':bool(attempt and attempt.final_json),
                    'can_submit':reason is None,'readiness_reason':reason,
                    **({'blocked_by':blocker} if blocker else {}),
                    'error_code':result.get('error_code'),
                    'receipt_summary':{'state':result.get('state'),'reference':result.get('receipt_reference',{})}})
            completed=sum(d['state'] in COMPLETED_DELIVERY_STATES for d in children)
            state=center.state
            if state=='action_required' and children and completed==len(children):
                # Older hosts omitted accepted/processing/reviewing from the
                # parent completion. Project that display without rewriting
                # history, changing any child, or resuming an explicit pause.
                state='completed'
            result={'id':task_id,'version':2,'state':state,'paused':center.state=='paused','quantity':len(children),'completed':completed,'deliveries':children,'submitted':any(d['final_started'] for d in children)}
            if state=='completed':
                states={d['state'] for d in children}
                result['status_label']=('平台审核中' if 'reviewing' in states else '平台处理中' if 'processing' in states
                    else '平台已接收定时' if states=={'platform_scheduled'} else '已公开' if states=={'published'} else '平台已接收')
            elif state=='completed_with_errors':
                result['status_label']='发布结束，有未完成项'
            return result

    def delivery_state(self, delivery_id):
        with session_scope(self.engine) as s:return self.deliveries.load(s,delivery_id)[1].state

    def pause(self, task_id):
        with write_session(self.engine) as s:
            center=s.get(TaskCenterRow,task_id);task=s.get(PublishTaskRow,task_id)
            if center is None or center.kind!='multi_publish':raise ValueError('V2 task required')
            center.state=task.state='paused'
        return self.get_task(task_id)

    def resume_task(self, task_id):
        with write_session(self.engine) as s:
            center=s.get(TaskCenterRow,task_id)
            if center is None or center.kind!='multi_publish':raise ValueError('V2 task required')
            center.state='waiting';self.deliveries.aggregate(s,task_id)
        return self.get_task(task_id)

    def cancel_delivery(self, delivery_id):
        with write_session(self.engine) as s:
            row,item,history,attempt,*_=self.deliveries.load(s,delivery_id)
            row.cancel_requested=True
            if attempt.final_json or (attempt.owner_hash and not self.deliveries.workers.stopped(attempt.owner_hash)):
                pass  # Already submitted/unknown or active owner: retain authority/evidence.
            elif item.state not in {'published','platform_scheduled','result_unknown'}:
                item.state=history.state='cancelled'
                for consumer in s.scalars(select(ResourceConsumerRow).where(ResourceConsumerRow.delivery_id==row.id)):
                    consumer.released=True
            self.deliveries.aggregate(s,row.task_id)
        self.cleanup_resources()
        return {'id':delivery_id,'state':self.delivery_state(delivery_id),'cancel_requested':True,'withdrawn':False}

    def hide_delivery(self, delivery_id):
        with write_session(self.engine) as s:
            row,*_=self.deliveries.load(s,delivery_id)
            row.hidden=True
        return {'id':delivery_id,'hidden':True}

    def cleanup_resources(self):
        # Only this service's software-owned copy can be deleted. Retained
        # success/unknown consumers remain references until explicit safe release.
        with write_session(self.engine) as s:
            for resource in s.scalars(select(PublishResourceRow).where(PublishResourceRow.cleaned.is_(False))):
                refs=list(s.scalars(select(ResourceConsumerRow).where(ResourceConsumerRow.resource_id==resource.id)))
                if refs and all(ref.released for ref in refs):
                    path=Path(resource.path)
                    root=(self.paths.data_dir/'publish_resources').resolve()
                    if not path.resolve().is_relative_to(root):raise ValueError('resource ownership mismatch')
                    if path.is_file() and sha256_file(path)!=resource.content_hash:raise ValueError('resource replaced; retain for review')
                    path.unlink(missing_ok=True)
                    resource.cleaned=True

    def reprepare_delivery(self, delivery_id, corrected_fields, *, reset_fields=None):
        return self._reprepare_delivery(delivery_id,corrected_fields,reset_fields=reset_fields)

    def reschedule_delivery(self, delivery_id, *, execution_mode, scheduled_at=None):
        if execution_mode not in {'publish_now','platform_schedule','software_schedule'}:raise ValueError('invalid execution mode')
        if execution_mode=='publish_now':
            if scheduled_at is not None:raise ValueError('publish_now cannot contain schedule')
        else:
            try:time=datetime.fromisoformat(scheduled_at)
            except (TypeError,ValueError):raise ValueError('scheduled_at required') from None
            if time.tzinfo is None:raise ValueError('scheduled timezone required')
            scheduled_at=time.astimezone(timezone.utc).isoformat()
        return self._reprepare_delivery(delivery_id,{},schedule=(execution_mode,scheduled_at))

    def _reprepare_delivery(self, delivery_id, corrected_fields, *, reset_fields=None, schedule=None):
        corrected=fields(corrected_fields)
        reset_fields=[] if reset_fields is None else reset_fields
        if type(reset_fields) is not list or any(type(k) is not str or k not in PUBLISH_FIELDS for k in reset_fields) or set(reset_fields)&set(corrected):
            raise ValueError('invalid override reset')
        with write_session(self.engine) as s:
            row,item,history,attempt,*rest=self.deliveries.load(s,delivery_id)
            if attempt.final_json or attempt.context_json or row.cancel_requested:
                raise ValueError('final or parked delivery requires reconciliation')
            if attempt.owner_hash and (not self.deliveries.workers.stopped(attempt.owner_hash) or not attempt.result_json or json.loads(attempt.result_json)['state']!='pre_submit_failed'):
                raise ValueError('old worker must be stopped with complete pre-final evidence')
            if item.state not in {'waiting','waiting_adapter','waiting_configuration','pre_submit_failed','missed_schedule'}:
                raise ValueError('delivery cannot be re-prepared')
            p=json.loads(row.snapshot_json)
            overrides=p.get('platform_overrides',{}).copy()
            for key in reset_fields:overrides.pop(key,None)
            overrides=merge_fields(overrides,corrected)
            p['platform_overrides']=overrides
            p['fields']=merge_fields(p['base_fields'],overrides)
            if schedule is not None:
                mode,time=schedule
                for other in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.platform==row.platform,PublishDeliveryRow.platform_user_id==row.platform_user_id,PublishDeliveryRow.id!=row.id)):
                    if time is not None and other.scheduled_at==time and s.get(TaskItemRow,other.item_id).state!='cancelled':
                        raise PublishScheduleConflict('same account videos need distinct times')
                p['execution_mode']=mode;p['scheduled_at']=time
                row.execution_mode=mode;row.scheduled_at=time
            if not p['fields'].get('title','').strip():raise ValueError('title required')
            state,reason=self._hold(p)
            row.attempt+=1;row.snapshot_json=canonical(p);row.request_digest=digest(p)
            s.add(DeliveryAttemptRow(id=str(uuid.uuid4()),delivery_id=row.id,number=row.attempt,snapshot_json=row.snapshot_json,request_digest=row.request_digest))
            item.state=history.state=state
            self.deliveries.aggregate(s,row.task_id)
        return {'id':delivery_id,'state':state,'attempt':row.attempt,'hold_reason':reason,'execution_mode':row.execution_mode,'scheduled_at':row.scheduled_at}

    def check_schedule(self, delivery_id, *, now=None):
        now=now or datetime.now(timezone.utc)
        with write_session(self.engine) as s:
            row,item,history,attempt,*_=self.deliveries.load(s,delivery_id)
            if row.execution_mode!='software_schedule':return 'due'
            scheduled=datetime.fromisoformat(row.scheduled_at)
            if now<scheduled:return 'future'
            if (now-scheduled).total_seconds()>60:
                if item.state=='waiting' and not attempt.owner_hash:
                    item.state=history.state='missed_schedule';self.deliveries.aggregate(s,row.task_id)
                return 'missed'
            return 'due'

    def recover_after_restart(self):
        return self.deliveries.recover_after_restart()

    def recover_delivery(self, delivery_id):
        """Explicit host reconciliation; no API-provided worker-stopped proof.

        Unknown registry owners cannot recover. Host supervisors may provide a
        registry with externally verified stopped-process evidence, but no
        client-supplied boolean can authorize this path.
        """
        from yijianjifa.features.publish.coordinator import AdapterOutcome, outcome_evidence, has_channels_immediate_ack
        with session_scope(self.engine) as s:
            row,item,history,attempt,*_=self.deliveries.load(s,delivery_id)
            if not attempt.owner_hash or not self.deliveries.workers.stopped(attempt.owner_hash):
                raise ValueError('trusted host has not proved old worker stopped')
            if item.state not in {'interrupted','paused_auth','result_unknown','platform_scheduled','platform_accepted','processing','reviewing'}:
                raise ValueError('delivery does not require recovery')
            fixed=(row.attempt,row.request_digest,attempt.owner_hash,attempt.final_json,item.state,attempt.result_json)
            payload=json.loads(row.snapshot_json)
            previous_result=json.loads(attempt.result_json) if attempt.result_json else {}
            payload['receipt_reference']=previous_result.get('receipt_reference',{})
            context=json.loads(attempt.context_json) if attempt.context_json else None
        adapter=self.catalog.transport_for_content(payload['platform'],payload.get('content_type','video'))
        if 'result_query' not in getattr(adapter,'verified_capabilities',()):
            raise ValueError('adapter reconciliation is not verified')
        outcome=adapter.reconcile(copy.deepcopy(payload),context=context)
        if not isinstance(outcome,AdapterOutcome):raise ValueError('trusted reconciliation outcome required')
        normalized=outcome_evidence(outcome,fixed[1])
        with write_session(self.engine) as s:
            row,item,history,attempt,*_=self.deliveries.load(s,delivery_id)
            if fixed!=(row.attempt,row.request_digest,attempt.owner_hash,attempt.final_json,item.state,attempt.result_json) or not self.deliveries.workers.stopped(attempt.owner_hash):
                raise ValueError('recovery authority changed')
            if not attempt.final_json:
                if outcome.state!='not_submitted' or outcome.context is not None:
                    raise ValueError('complete no-final reconciliation evidence required')
                state='pre_submit_failed'
            else:
                accepted={'platform_accepted','processing','reviewing'}
                state=outcome.state if outcome.state in {'published','platform_scheduled','platform_rejected'}|accepted else 'result_unknown'
                if not normalized['receipt_reference']:normalized['receipt_reference']=previous_result.get('receipt_reference',{})
                ack_confirmed=has_channels_immediate_ack(normalized,platform=row.platform,execution_mode=row.execution_mode,
                    request_digest=row.request_digest,final_started=
                    json.loads(attempt.final_json).get('request_digest')==row.request_digest)
                if state in accepted and not normalized['receipt_reference'] and not ack_confirmed:state='result_unknown'
                if state=='platform_scheduled' and (row.execution_mode!='platform_schedule' or outcome.scheduled_at!=row.scheduled_at):
                    state='result_unknown'
            evidence={**normalized,'state':state}
            previous_recovery=json.loads(attempt.recovery_json) if attempt.recovery_json else {}
            events=previous_recovery.get('events',[]) if previous_recovery else []
            if previous_recovery and 'events' not in previous_recovery:events=[previous_recovery]
            old_result=json.loads(attempt.result_json) if attempt.result_json else None
            if old_result==evidence:return {'id':delivery_id,'state':state,'resubmitted':False}
            # A failed query cannot undo an earlier confirmed platform receipt.
            # Revalidate that receipt on this exact frozen attempt and journal;
            # unknown/protocol-invalid records never gain confirmation here.
            retained=False
            if (state=='result_unknown' and attempt.final_json and old_result and not old_result.get('protocol_violation')
                    and old_result.get('state')==item.state and old_result.get('request_digest')==row.request_digest):
                try:
                    prior=outcome_evidence(AdapterOutcome(old_result['state'],scheduled_at=old_result.get('scheduled_at'),
                        receipt_reference=old_result.get('receipt_reference'),error_code=old_result.get('error_code'),
                        submission_ack=old_result.get('submission_ack')),row.request_digest)
                    ack_confirmed=has_channels_immediate_ack(prior,platform=row.platform,execution_mode=row.execution_mode,
                        request_digest=row.request_digest,final_started=
                        json.loads(attempt.final_json).get('request_digest')==row.request_digest)
                    retained=(item.state in {'platform_accepted','processing','reviewing'} and (bool(prior['receipt_reference']) or ack_confirmed) or
                        item.state=='platform_scheduled' and row.execution_mode=='platform_schedule' and type(row.scheduled_at) is str and bool(row.scheduled_at) and prior['scheduled_at']==row.scheduled_at)
                except ValueError:pass
            event={'old_attempt':row.attempt,'old_owner_hash':attempt.owner_hash,'old_result':old_result,
                'host_verified_stopped':True,'verified_result':evidence}
            if retained:event['retained_confirmed_result']=True
            events.append(event)
            attempt.recovery_json=canonical({'events':events})
            if retained:state=item.state
            else:
                attempt.result_json=canonical(evidence)
                if state!='result_unknown':attempt.context_json=None
                item.state=history.state=state
            self.deliveries.aggregate(s,row.task_id)
        return {'id':delivery_id,'state':state,'resubmitted':False}

    def release_delivery_resources(self, delivery_id):
        """Release exactly one safely terminal consumer, retaining its evidence."""
        with write_session(self.engine) as s:
            row,item,history,attempt,*_=self.deliveries.load(s,delivery_id)
            if item.state not in {'published','platform_rejected','cancelled'} or attempt.context_json:
                raise ValueError('unresolved target retains its resources')
            if attempt.owner_hash and not self.deliveries.workers.stopped(attempt.owner_hash):
                raise ValueError('old worker is not proved stopped')
            if item.state!='cancelled' and (not attempt.result_json or json.loads(attempt.result_json)['state']!=item.state):
                raise ValueError('terminal evidence is incomplete')
            for consumer in s.scalars(select(ResourceConsumerRow).where(ResourceConsumerRow.delivery_id==row.id)):
                consumer.released=True
        self.cleanup_resources()
        return {'id':delivery_id,'resources_released':True,'evidence_retained':True}
