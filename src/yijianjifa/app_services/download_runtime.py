"""Finite download intents and independent cancellable background execution.

Bridge calls only write intent on their supplied connection or read committed
safe DTOs. Native paths/private media/provider objects never become DTOs. Long
operations hold no receipt transaction; two network slots and one local slot
are shared by previews, batches and explicit model operations.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from yijianjifa.features.downloads.contracts import DownloadError, DownloadOptions, OutputResource, Region, SourceInput, StageResult
from yijianjifa.features.downloads.input import normalize_inputs
from yijianjifa.features.downloads.repository import DownloadRepository
from yijianjifa.features.downloads.files import OwnedFiles, _Guard, _safe_final_name
from yijianjifa.features.downloads.local import selected_files, receive_file
from yijianjifa.features.downloads.network import SecureFetcher, SourceRegistry
from yijianjifa.features.downloads.providers import DirectProvider, DouyinProvider
from yijianjifa.features.downloads.models import ModelStore
from yijianjifa.features.downloads.media import MediaProcessor
from yijianjifa.features.downloads.asr import AsrEngine
from yijianjifa.features.downloads.ocr import OcrEngine, DEFAULT_OCR_MODEL_IDS
from yijianjifa.features.downloads.subtitles import SubtitleStore
from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.features.notifications.service import NotificationService
from yijianjifa.storage.models import DownloadAuxiliaryJobRow, DownloadCandidateRow, DownloadDecisionRow, DownloadItemRow, DownloadOutputRow, DownloadResolutionRow, DownloadSourceRow, DownloadStageRow, DownloadSubtitleDraftRow, DownloadSubtitleOriginalRow, DownloadSubtitleRevisionRow, Setting
from .download_access import DownloadAccess
from .download_commands import DownloadCommands, MESSAGES, MODEL_IDS, local_message
from .download_dto import batch_detail

_LOG=logging.getLogger(__name__)
_PREFIX='download-runtime:'
_AUTH={'verification_required','account_busy','account_unavailable','session_missing','session_invalid'}
_MODELS={'model_required','model_invalid','model_unknown'}
_FINAL={'completed','completed_with_errors','cancelled'}


def _id():return uuid.uuid4().hex

def _now():return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _NativeSelection:
    token: str
    operation: str
    metadata: dict
    response: dict | None = None


class _Cancel:
    def __init__(self, runtime, event=None):self.runtime,self.event=runtime,event or threading.Event()
    def cancelled(self):return self.runtime._stop.is_set() or self.event.is_set()


def _check(cancel):
    if cancel.cancelled():raise DownloadError('cancelled')


@contextmanager
def _slot(semaphore,cancel):
    _check(cancel)
    while not semaphore.acquire(timeout=.05):_check(cancel)
    try:
        _check(cancel)
        yield
    finally:semaphore.release()


class DownloadRuntime:
    def __init__(self,engine,paths,*,media,providers=None,fetcher=None,cover_fetcher=None,files=None,models=None,
                 processor=None,asr=None,ocr=None,access=None,browser_pool=None,clock=time.monotonic):
        self.engine,self.paths,self.media=engine,paths,media
        self.clock=clock;self.network_slots=threading.BoundedSemaphore(2);self.local_slot=threading.BoundedSemaphore(1)
        self.repository=DownloadRepository(engine,known_model_ids=tuple(MODEL_IDS))
        self.registry=SourceRegistry()
        self.fetcher=fetcher or SecureFetcher(self.registry)
        # Covers have an independent finite body budget, with the same checked
        # connector policy. Never share mutable request budget state.
        connector=getattr(getattr(self.fetcher,'transport',None),'connector',None)
        self.cover_fetcher=cover_fetcher or SecureFetcher(self.registry,max_bytes=16*1024*1024,connector=connector)
        self.files=files or OwnedFiles(self.repository,paths)
        self.models=models or ModelStore(paths.data_dir/'models',registry=self.registry,fetcher=self.fetcher,network_slots=self.network_slots)
        # ModelStore.install owns this leaf semaphore; Runtime never takes it a
        # second time around install. All installations share the same budget.
        self.models.network_slots=self.network_slots
        self.processor=processor or MediaProcessor(self.files)
        self.asr=asr or AsrEngine(self.models)
        self.ocr=ocr or OcrEngine(self.models)
        self.subtitles=SubtitleStore(self.repository)
        self.access=access or DownloadAccess(self.files,engine,paths,media)
        self.providers=dict(providers or {'direct':DirectProvider(self.registry)})
        self._browser_pool=browser_pool;self._lease=None
        self._epoch=_id();self._stop=threading.Event();self._wake=threading.Event();self._lock=threading.RLock()
        self._thread=None;self._executor=None;self._platform_executor=None;self._closed_ok=False
        self._active={};self._events={};self._progress={};self._issued={};self._sources={};self._sessions={};self._cursors={}
        self._actual_durations={}
        self._local_inputs={};self._local_ready={}
        self._pending_native={};self._urls={};self._cover_reads={};self._heartbeat={};self._recovery_errors=[]
        self._last_maintenance=clock()
        self.notifications=NotificationService(engine,sound_enabled=False)
        self.files._revoke_callbacks.append(self._forget_url)
        self.recovery_complete=False
        self._output={'grant_id':None,'display_path':None,'needs_selection':True}
        catalog=getattr(self.models,'_models',{})
        self._model_cache=[{'id':identity,'name':entry['name'],'status':'checking','size_bytes':entry['size_bytes']} for identity,entry in catalog.items()]

    def _get(self,connection,kind,identity,default=None):
        raw=connection.execute(select(Setting.value_json).where(Setting.key==_PREFIX+kind+':'+identity)).scalar_one_or_none()
        if raw is None:return deepcopy(default)
        try:
            if len(raw.encode('utf-8'))>8192:raise ValueError
            value=json.loads(raw)
            if type(value) is not dict or type(value.get('v')) is not int or value['v']!=1:raise ValueError
            return value
        except (ValueError,TypeError,UnicodeError,RecursionError):raise DownloadError('metadata_missing') from None

    def _put(self,connection,kind,identity,value):
        self.repository._write(connection)
        encoded=json.dumps({'v':1,**value},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(encoded.encode('utf-8'))>8192:raise DownloadError('input_limit')
        statement=sqlite_insert(Setting).values(key=_PREFIX+kind+':'+identity,value_json=encoded)
        connection.execute(statement.on_conflict_do_update(index_elements=['key'],set_={'value_json':encoded}))

    def _control(self,connection,task_id):
        value=self._get(connection,'control',task_id,{'v':1,'epoch':'','enabled':False,'cancel':False,'show_browser':False})
        if set(value)!={'v','epoch','enabled','cancel','show_browser'} or type(value['epoch']) is not str or any(type(value[key]) is not bool for key in ('enabled','cancel','show_browser')):
            raise DownloadError('metadata_missing')
        return value

    def _reason(self,connection,item_id):
        value=self._get(connection,'item',item_id,{'v':1,'reason':None,'error_code':None,'watermark':'unknown','source_id':None})
        if (set(value)!={'v','reason','error_code','watermark','source_id'} or value['reason'] not in (None,'auth','watermark','model')
            or value['watermark'] not in ('clean','original','unknown') or value['error_code'] is not None and value['error_code'] not in MESSAGES
            or value['source_id'] is not None and (type(value['source_id']) is not str or len(value['source_id'])>64)):
            raise DownloadError('metadata_missing')
        return value

    def prepare_native(self,operation,params,selected):
        DownloadCommands.validate(operation,params)
        if operation not in ('download.output.choose','download.models.import','download.subtitle.export','download.local.choose','download.local.drop'):
            raise DownloadError('unsupported_source')
        if self._stop.is_set():raise DownloadError('cancelled')
        if operation in ('download.local.choose','download.local.drop'):
            selections=selected_files(selected)
            value=_NativeSelection(_id(),operation,{'selections':selections})
            with self._lock:
                if len(self._local_inputs)>=32:raise DownloadError('input_limit')
                self._pending_native[value.token]=value
            return value
        if isinstance(selected,(list,tuple)):
            if len(selected)!=1:raise DownloadError('input_limit')
            selected=selected[0]
        if not isinstance(selected,(str,Path)):raise DownloadError('input_limit')
        target=Path(selected)
        if not target.is_absolute() or len(str(target))>4000:raise DownloadError('unsafe_source')
        response=None
        if operation=='download.output.choose':
            response=self.files.choose_output(target)
            metadata={'path':str(target),'grant_id':response['grant_id']}
        elif operation=='download.models.import':
            with _Guard(target,directory=False) as guard:
                guard.verify();metadata={'path':str(target),'identity':list(guard.identity)}
        else:
            if not _safe_final_name(target.name) or target.suffix.lower()!='.'+params['format']:
                raise DownloadError('unsafe_source')
            with _Guard(target.parent) as guard:
                if target.exists() or target.is_symlink():raise DownloadError('unsafe_source')
                guard.verify();metadata={'path':str(target),'identity':list(guard.identity)}
        value=_NativeSelection(_id(),operation,metadata,response)
        with self._lock:self._pending_native[value.token]=value
        return value

    def _native(self,connection,operation,native):
        if not isinstance(native,_NativeSelection) or native.operation!=operation:
            raise DownloadError('unsafe_source')
        with self._lock:
            if self._pending_native.get(native.token) is not native:raise DownloadError('unsafe_source')
        # External local selections are lifetime-only grants. They never enter
        # persisted settings/snapshots and cannot be reread on application start.
        if operation not in ('download.local.choose','download.local.drop'):
            self._put(connection,'private',native.token,{'operation':operation,**native.metadata})
        with self._lock:self._pending_native.pop(native.token,None)
        return native

    def _forget_url(self,resource_id):
        with self._lock:
            self._urls.pop(resource_id,None);self._cover_reads.pop(resource_id,None)
            for cid,rid in tuple(self._local_ready.items()):
                if rid==resource_id:self._local_ready.pop(cid,None)

    def _cover_read_room(self,resource_id):
        revoked=[]
        with self._lock:
            self._cover_reads.pop(resource_id,None)
            while len(self._cover_reads)>=20:
                oldest=next(iter(self._cover_reads));self._cover_reads.pop(oldest)
                self._urls.pop(oldest,None);revoked.append(oldest)
        for identity in revoked:self.media.revoke_read('download:'+identity)

    def _remember_preview(self,resource_id,response):
        with self._lock:
            self._urls[resource_id]={**response,'resource_id':resource_id}
            if response.get('kind')=='cover':
                self._cover_reads.pop(resource_id,None);self._cover_reads[resource_id]=None

    def _yield_cover_read(self):
        with self._lock:
            if not self._cover_reads:return False
            identity=next(iter(self._cover_reads));self._cover_reads.pop(identity)
            self._urls.pop(identity,None)
        self.media.revoke_read('download:'+identity)
        return True

    def _session_group(self,connection,session_id):
        value=self._get(connection,'session',session_id,{'v':1,'session_ids':[session_id],'job_id':None})
        if (set(value)!={'v','session_ids','job_id'} or type(value['session_ids']) is not list
            or not 1<=len(value['session_ids'])<=20 or len(set(value['session_ids']))!=len(value['session_ids'])
            or any(type(identity) is not str or not re.fullmatch(r'resolve_[0-9a-f]{32}',identity) for identity in value['session_ids'])
            or value['job_id'] is not None and (type(value['job_id']) is not str or len(value['job_id'])>64)):
            raise DownloadError('metadata_missing')
        return value

    def _catalog_members(self,connection,session_id):
        metadata=self._get(connection,'catalog',session_id+':meta')
        if metadata is None:
            return list(connection.execute(select(DownloadSourceRow.candidate_id).where(DownloadSourceRow.session_id==session_id)).scalars())
        if set(metadata)!={'v','blocks'} or type(metadata['blocks']) is not int or not 0<=metadata['blocks']<=3:
            raise DownloadError('metadata_missing')
        result=[]
        for index in range(metadata['blocks']):
            block=self._get(connection,'catalog',session_id+':'+str(index))
            if (block is None or set(block)!={'v','ids'} or type(block['ids']) is not list or not 1<=len(block['ids'])<=200
                or any(type(identity) is not str or not re.fullmatch(r'[0-9a-f]{32}',identity) for identity in block['ids'])):
                raise DownloadError('metadata_missing')
            result.extend(block['ids'])
        if len(result)>500 or len(set(result))!=len(result):raise DownloadError('metadata_missing')
        return result

    def _save_catalog(self,connection,session_id,identities):
        identities=list(dict.fromkeys(identities))
        if len(identities)>500:raise DownloadError('input_limit')
        if any(type(identity) is not str or not re.fullmatch(r'[0-9a-f]{32}',identity) for identity in identities):
            raise DownloadError('metadata_missing')
        blocks=[identities[index:index+200] for index in range(0,len(identities),200)]
        for index,block in enumerate(blocks):self._put(connection,'catalog',session_id+':'+str(index),{'ids':block})
        self._put(connection,'catalog',session_id+':meta',{'blocks':len(blocks)})

    def _new_job(self,connection,kind,**inputs):
        identity=self.repository.create_auxiliary_job(connection,kind=kind,**inputs)
        self._put(connection,'job',identity,{'epoch':self._epoch,'enabled':True,'cancel':False,'show_browser':False,'session_ids':[],'cursor':None})
        return identity

    def enqueue(self,connection,operation,params,native=None):
        """Only SQL intent/metadata writes on caller's active receipt transaction."""
        self.repository._write(connection);DownloadCommands.validate(operation,params)
        if self._stop.is_set():raise DownloadError('cancelled')
        name=operation.removeprefix('download.')
        if name=='output.choose':return dict(self._native(connection,operation,native).response)
        if name=='settings.save':
            previous=self._get(connection,'settings','current',{'v':1,'source_mode':'link'})
            self._put(connection,'settings','current',{'options':params['options'],'filter':params['filter'],
                'source_mode':params.get('source_mode',previous.get('source_mode','link'))})
            return {'saved':True}
        if name in ('local.choose','local.drop'):
            selection=self._native(connection,operation,native)
            source=SourceInput('local','local:'+_id()+_id(),None)
            main=self.repository.create_resolution(connection,source)
            job=self._new_job(connection,'resolve',session_id=main)
            self._put(connection,'job',job,{'epoch':self._epoch,'enabled':True,'cancel':False,'show_browser':False,
                'session_ids':[main],'cursor':None})
            self._put(connection,'session',main,{'session_ids':[main],'job_id':job})
            self._save_catalog(connection,main,[])
            self._put(connection,'intake',main,{'received':0,'failed':0,'blocks':0})
            with self._lock:
                self._local_inputs[job]=selection.metadata['selections']
                self._sessions[main]={'has_more':False,'cursor':None,'completion':'partial'}
            return {'job_id':job,'session_id':main}
        if name=='resolve.start':
            sources=normalize_inputs(params['input']) if 'input' in params else None
            if sources:
                session_ids=[self.repository.create_resolution(connection,source) for source in sources]
                main=session_ids[0]
                with self._lock:
                    for session,source in zip(session_ids,sources):self._sources[session]=source
            else:
                main=params['session_id'];self.repository._row(connection,DownloadResolutionRow,main)
                with self._lock:
                    binding=self._cursors.get(params['cursor'])
                if binding is None or binding[0]!=main:raise DownloadError('source_missing')
                session_ids=[main]
            job=self._new_job(connection,'resolve',session_id=main)
            self._put(connection,'job',job,{'epoch':self._epoch,'enabled':True,'cancel':False,
                'show_browser':params.get('show_browser',False),'session_ids':session_ids,'cursor':params.get('cursor')})
            group=self._session_group(connection,main)
            if sources:group['session_ids']=session_ids
            self._put(connection,'session',main,{'session_ids':group['session_ids'],'job_id':job})
            return {'job_id':job,'session_id':main}
        if name=='start':
            options=DownloadOptions(**{**params['options'],'model_ids':tuple(params['options']['model_ids'])})
            ids=tuple(params['candidate_ids']);regions={identity:self.repository.get_region(connection,identity) for identity in ids}
            platforms={self.repository._row(connection,DownloadCandidateRow,identity)['platform'] for identity in ids}
            local=platforms=={'local'}
            if 'local' in platforms and not local:raise DownloadError('unsupported_source')
            if local:
                if options.save_video or options.prefer_clean:raise DownloadError('unsupported_source')
                with self._lock:ready={identity:self._local_ready.get(identity) for identity in ids}
                for identity,rid in ready.items():
                    if rid is None:raise DownloadError('local_source_missing')
                    resource=self.repository._row(connection,DownloadOutputRow,rid)
                    journal=connection.execute(select(Setting.value_json).where(Setting.key=='download-owned:journal:'+rid)).scalar_one_or_none()
                    if (resource['candidate_id']!=identity or resource['item_id'] is not None or resource['kind']!='source_video'
                        or journal is None or json.loads(journal).get('state')!='committed'):
                        raise DownloadError('local_source_missing')
            if not params.get('confirm_existing',False):
                existing,active=self._existing_candidates(connection,ids)
                if existing or active:return {'confirmation_required':True,'existing_count':len(existing),'active_count':len(active)}
            task=self.repository.create_batch(connection,ids,options,params['directory_grant_id'],regions)
            self._put(connection,'control',task,{'epoch':self._epoch,'enabled':True,'cancel':False,'show_browser':False})
            return {'task_id':task,'count':len(ids)}
        if name in ('cancel','models.cancel'):
            if 'job_id' in params:
                job=params['job_id'];self.repository._row(connection,DownloadAuxiliaryJobRow,job)
                value=self._get(connection,'job',job)
                if value is None:raise DownloadError('metadata_missing')
                value['cancel']=True;self._put(connection,'job',job,value)
                return {'job_id':job}
            task=params['task_id'];self.repository._summary(connection,task)
            control=self._control(connection,task);control['cancel']=True;control['enabled']=False
            self._put(connection,'control',task,control);self._refresh(connection,task)
            return {'task_id':task}
        if name in ('resume','retry','decision'):
            task=params['task_id'];summary=self.repository._summary(connection,task)
            if summary['snapshot'].get('source_mode')=='local' and (name=='decision' or params.get('show_browser',False)):
                raise DownloadError('unsupported_source')
            if summary['state']=='cancelling':raise DownloadError('input_limit')
            if name=='decision' and self._control(connection,task)['cancel']:raise DownloadError('input_limit')
            by_id={item['id']:item for item in summary['items']}
            if name=='decision':
                item=params['item_id']
                if item not in by_id or self._reason(connection,item)['reason']!='watermark':raise DownloadError('input_limit')
                self.repository.record_decision(connection,item,params['choice'])
                reason=self._reason(connection,item);reason['reason']=None;reason['error_code']=None
                if params['choice']=='original':reason['watermark']='original'
                self._put(connection,'item',item,reason)
            else:
                selected=params.get('item_ids',list(by_id))
                if not set(selected)<=set(by_id):raise DownloadError('identity_mismatch')
                for item in selected:
                    stages=params.get('stages',list(by_id[item]['stages']))
                    if not set(stages)<=set(by_id[item]['stages']):raise DownloadError('input_limit')
                    if name=='retry' and any(value['state']=='running' for value in by_id[item]['stages'].values()):
                        raise DownloadError('input_limit')
                    for stage in stages:
                        if by_id[item]['stages'][stage]['state']=='running':continue
                        self.repository.reset_stage(connection,item,stage,intent='retry' if name=='retry' else 'resume')
                    reason=self._reason(connection,item)
                    if reason['reason']!='watermark':reason['reason']=None;reason['error_code']=None
                    self._put(connection,'item',item,reason)
            self._put(connection,'control',task,{'epoch':self._epoch,'enabled':True,'cancel':False,'show_browser':params.get('show_browser',False)})
            self._refresh(connection,task)
            return {'task_id':task}
        if name=='preview.region.save':
            self.repository.set_region(connection,params['candidate_id'],Region(**params['region']))
            return {'candidate_id':params['candidate_id'],'region':params['region']}
        if name=='subtitle.save':return self.subtitles.save(connection,params['resource_id'],params['origin'],params['expected_revision'],tuple(params['patches']))
        if name=='models.download':return {'job_id':self._new_job(connection,'model_download',model_id=params['model_id'])}
        if name=='models.import':
            selected=self._native(connection,operation,native)
            return {'job_id':self._new_job(connection,'model_import',model_id=params['model_id'],input_grant_id=selected.token)}
        if name=='subtitle.export':
            selected=self._native(connection,operation,native)
            original=connection.execute(select(DownloadSubtitleOriginalRow.id,DownloadSubtitleOriginalRow.origin).where(DownloadSubtitleOriginalRow.id==params['resource_id'])).mappings().first()
            if original is None:raise DownloadError('source_missing')
            if original['origin']!=params['origin']:raise DownloadError('identity_mismatch')
            drafts=connection.execute(select(DownloadSubtitleDraftRow.id).where(DownloadSubtitleDraftRow.original_id==original['id'])).scalars().all()
            if len(drafts)>1:raise DownloadError('subtitle_invalid')
            draft=drafts[0] if drafts else None
            revision=connection.execute(select(func.max(DownloadSubtitleRevisionRow.revision)).where(DownloadSubtitleRevisionRow.draft_id==draft)).scalar() or 0
            return {'job_id':self._new_job(connection,'subtitle_export',subtitle_original_id=original['id'],subtitle_draft_id=draft if revision else None,
                subtitle_revision=revision,subtitle_origin=params['origin'],export_format=params['format'],output_grant_id=selected.token)}
        if name=='preview.prepare':return {'job_id':self._new_job(connection,'preview',candidate_id=params['candidate_id'])}
        if name in ('preview','reveal','file.delete','import'):
            if name=='preview':
                with self._lock:
                    cached=deepcopy(self._urls.get(params['resource_id']))
                    if cached is not None and cached.get('kind')=='cover':
                        self._cover_reads.pop(params['resource_id'],None);self._cover_reads[params['resource_id']]=None
                if cached is not None:return cached
            kind={'preview':'resource_preview','reveal':'resource_reveal','file.delete':'resource_delete','import':'resource_import'}[name]
            return {'job_id':self._new_job(connection,kind,output_resource_id=params['resource_id'],
                                        category_ids=tuple(params.get('category_ids',())))}
        raise DownloadError('unsupported_source')

    def _aux_dto(self,job_id):
        row=self.repository.get_auxiliary_job(job_id)
        result={key:row[key] for key in ('kind','state','completion','error_code','resource_id','library_id','installed_model_id')}
        result['job_id']=job_id;result['message']=MESSAGES.get(row['error_code'],'')
        result['verification_required']=row['error_code'] in _AUTH
        if row['session_id']:result['session_id']=row['session_id']
        if row['kind']=='resource_preview' and row['state']=='succeeded':
            result['resource_id']=row['output_resource_id']
            with self._lock:result.update(deepcopy(self._urls.get(row['output_resource_id'],{})))
        with self._lock:
            if ('job',job_id) in self._progress:result['progress']=deepcopy(self._progress[('job',job_id)])
            session=deepcopy(self._sessions.get(row['session_id'],{}))
        if row['kind']=='resolve':
            result.update({key:session[key] for key in ('cursor','has_more','completion') if key in session})
            result.setdefault('has_more',False);result.setdefault('cursor',None)
            result['next_cursor']=result['cursor']
            with self.engine.connect() as connection:
                resolution=self.repository._row(connection,DownloadResolutionRow,row['session_id'])
                result['source_kind']=resolution['source_kind']
                if resolution['source_kind']=='local':
                    intake=self._get(connection,'intake',row['session_id'],{'v':1,'received':0,'failed':0,'blocks':0})
                    errors=[]
                    for index in range(intake['blocks']):
                        errors.extend(self._get(connection,'intake-errors',row['session_id']+':'+str(index))['items'])
                    result.update(received=intake['received'],failed=intake['failed'],errors=errors)
                    result['message']=local_message(row['error_code'])
                    if row['state']=='interrupted':result['message']=MESSAGES['local_reselect_required']
        return result

    def query(self,operation,params):
        """Committed DB/cache reads only: no source/model/file hashing or tools."""
        DownloadCommands.validate(operation,params)
        name=operation.removeprefix('download.')
        if name=='output.read':
            with self._lock:return deepcopy(self._output)
        if name=='settings.read':
            with self.engine.connect() as connection:
                value=self._get(connection,'settings','current',{'v':1,'options':asdict(DownloadOptions()),'filter':{'query':'','state':''}})
            return {'options':value['options'],'filter':value['filter'],'source_mode':value.get('source_mode','link')}
        if name=='models.list':
            with self._lock:items=deepcopy(self._model_cache)
            with self.engine.connect() as connection:
                jobs=connection.execute(select(DownloadAuxiliaryJobRow.id).where(DownloadAuxiliaryJobRow.kind.in_(('model_download','model_import'))).order_by(DownloadAuxiliaryJobRow.created_at.desc()).limit(20)).scalars().all()
            return {'items':items,'jobs':[self._aux_dto(identity) for identity in jobs],'cache_path':str(getattr(self.models,'root',self.paths.data_dir/'models'))}
        if name in ('resolve.status','preview.status') or name=='detail' and 'job_id' in params:
            return self._aux_dto(params['job_id'])
        if name=='subtitle.read':return self.subtitles.read(params['resource_id'],params['origin'],params['cursor'])
        if name=='detail':
            result=batch_detail(self.engine,params['task_id'])
            with self.engine.connect() as connection:
                for item in result['items']:
                    reason=self._reason(connection,item['id'])
                    item.update(waiting_reason=reason['reason'],decision_required=reason['reason']=='watermark',watermark=reason['watermark'])
            result['verification_required']=any(item['waiting_reason']=='auth' for item in result['items'])
            result['message']='需要手动完成下载验证后继续。' if result['verification_required'] else '部分条目需要选择原样、跳过或准备模型。' if result['state']=='paused' else ''
            result['recovering']=not self.recovery_complete
            with self._lock:
                for item in result['items']:
                    if ('item',item['id']) in self._progress:item['progress']=deepcopy(self._progress[('item',item['id'])])
            return result
        if name=='list':
            with self.engine.connect() as connection:
                rows=connection.execute(select(TaskCenterRow.id,TaskCenterRow.name,TaskCenterRow.state,TaskCenterRow.created_at).where(TaskCenterRow.kind=='download').order_by(TaskCenterRow.created_at.desc())).mappings().all()
                hidden={key.removeprefix('task-hidden:') for key in connection.execute(select(Setting.key).where(Setting.key.like('task-hidden:%'))).scalars()}
            rows=[row for row in rows if row['id'] not in hidden and (not params['filter']['query'] or params['filter']['query'] in row['name'])
                  and params['filter']['state'] in ('','all',row['state'])]
            start=(params['page']-1)*20
            items=[]
            for row in rows[start:start+20]:
                summary=self.repository.summary(row['id'])
                items.append({'task_id':row['id'],'title':row['name'],'state':row['state'],'created_at':row['created_at'].isoformat(),
                              'total':summary['total'],'counts':summary['counts'],
                              'source_mode':summary['snapshot'].get('source_mode','link'),
                              'source_kind':'local' if summary['snapshot'].get('source_mode')=='local' else 'link'})
            return {'items':items,'page':params['page'],'page_size':20,'total':len(rows),'total_pages':max(1,(len(rows)+19)//20)}
        if name=='resolve.list':
            session_id=params['session_id']
            with self.engine.connect() as connection:
                main=self.repository._row(connection,DownloadResolutionRow,session_id)
                group=self._session_group(connection,session_id)
                sessions=group['session_ids']
                if type(sessions) is not list or not 1<=len(sessions)<=20:raise DownloadError('metadata_missing')
                identities=list(dict.fromkeys(identity for session in sessions for identity in self._catalog_members(connection,session)))
                if len(identities)>500:raise DownloadError('metadata_missing')
                rows=connection.execute(select(DownloadCandidateRow.__table__).where(DownloadCandidateRow.id.in_(identities)).order_by(DownloadCandidateRow.created_at,DownloadCandidateRow.id).limit(500)).mappings().all()
                output_rows=connection.execute(select(DownloadOutputRow.id,DownloadOutputRow.candidate_id,DownloadOutputRow.item_id,Setting.value_json).join(Setting,
                    Setting.key=='download-owned:journal:'+DownloadOutputRow.id).where(DownloadOutputRow.candidate_id.in_(identities),DownloadOutputRow.kind!='cover')).all()
                output_ids=set()
                with self._lock:invalid={row.get('resource_id') for row in self._recovery_errors}
                local_ids={row['id'] for row in rows if row['platform']=='local'}
                for identity,candidate_id,item_id,encoded in output_rows:
                    if candidate_id in local_ids and item_id is None:continue
                    try:
                        if len(encoded.encode('utf-8'))<=8192 and json.loads(encoded).get('state')=='committed' and identity not in invalid:
                            output_ids.add(candidate_id)
                    except (ValueError,TypeError,AttributeError,RecursionError):pass
                kinds=connection.execute(select(DownloadResolutionRow.source_kind).where(DownloadResolutionRow.id.in_(sessions))).scalars().all()
            total_read=len(rows);query=params['filter']['query'];state=params['filter']['state']
            with self._lock:local_ready=dict(self._local_ready)
            def candidate_state(row):
                if row['platform']=='local':return 'processed' if row['id'] in output_ids else 'received' if row['id'] in local_ready else 'interrupted'
                return 'downloaded' if row['id'] in output_ids else 'not_downloaded'
            rows=[row for row in rows if (not query or query in row['title'] or query in row['author']) and
                  (state in ('','all') or state==candidate_state(row))]
            with self._lock:page=deepcopy(self._sessions.get(session_id,{}))
            start=(params['page']-1)*20
            items=[{key:row[key] for key in ('id','platform','work_id','title','author','duration_ms','published_at','cover_resource_id')} |
                   {'state':candidate_state(row),'region':json.loads(row['region_json'])} for row in rows[start:start+20]]
            for item in items:
                if item['platform']=='local':item['source_resource_id']=local_ready.get(item['id'])
            with self.engine.connect() as connection:
                for item in items:
                    latest=select(DownloadAuxiliaryJobRow.id).where(DownloadAuxiliaryJobRow.candidate_id==DownloadCandidateRow.id,
                        DownloadAuxiliaryJobRow.kind=='cover').order_by(DownloadAuxiliaryJobRow.created_at.desc(),DownloadAuxiliaryJobRow.id.desc())
                    latest=latest.limit(1).correlate(DownloadCandidateRow).scalar_subquery()
                    cover=connection.execute(select(DownloadCandidateRow.cover_resource_id,DownloadAuxiliaryJobRow.id,
                        DownloadAuxiliaryJobRow.state).outerjoin(DownloadAuxiliaryJobRow,DownloadAuxiliaryJobRow.id==latest)
                        .where(DownloadCandidateRow.id==item['id'])).one()
                    # Pointer and receipt must come from the same committed read.
                    item['cover_resource_id']=cover.cover_resource_id
                    item['cover_job_id']=cover.id
                    item['cover_state']='succeeded' if cover.cover_resource_id else cover.state
            return {'items':items,'page':params['page'],'page_size':20,'total':len(rows),'total_pages':max(1,(len(rows)+19)//20),
                    'session_id':session_id,'source_kind':kinds[0] if len(set(kinds))==1 else 'mixed','total_read':total_read,
                    'completion':page.get('completion',main['completion']),'has_more':page.get('has_more',False),'next_cursor':page.get('cursor')}
        raise DownloadError('unsupported_source')

    def poll(self,task_id=None):
        return self.query('download.detail',{'task_id':task_id}) if task_id else self.query('download.list',{'page':1,'filter':{'query':'','state':''}})

    def _existing_candidates(self,connection,identities):
        # Warning is based on committed ownership metadata, not a synchronous
        # physical probe. Confirming retains every selected candidate unchanged.
        committed=case((func.json_valid(Setting.value_json)==1,func.json_extract(Setting.value_json,'$.state')),else_=None)
        existing=set(connection.execute(select(DownloadOutputRow.candidate_id).join(Setting,
            Setting.key=='download-owned:journal:'+DownloadOutputRow.id).where(
                DownloadOutputRow.candidate_id.in_(identities),DownloadOutputRow.kind.in_(('video','source_video','preview')),
                func.length(Setting.value_json)<=8192,committed=='committed',
                ~((DownloadOutputRow.candidate_id.in_(select(DownloadCandidateRow.id).where(DownloadCandidateRow.platform=='local')))
                  & DownloadOutputRow.item_id.is_(None))).distinct()).scalars())
        active=set(connection.execute(select(DownloadItemRow.candidate_id).join(TaskCenterRow,TaskCenterRow.id==DownloadItemRow.task_id)
            .join(DownloadStageRow,DownloadStageRow.item_id==DownloadItemRow.id).where(
                DownloadItemRow.candidate_id.in_(identities),TaskCenterRow.kind=='download',TaskCenterRow.state.not_in(_FINAL),
                DownloadStageRow.state.in_(('waiting','running','interrupted'))).distinct()).scalars())
        return existing,active

    def _refresh(self,connection,task_id):
        summary=self.repository._summary(connection,task_id);control=self._control(connection,task_id)
        with self._lock:active=any(owner==task_id for _,owner in self._active.values())
        pending=[];running=False
        for item in summary['items']:
            unfinished=[stage for stage in item['stages'].values() if stage['state'] in ('waiting','running','interrupted')]
            running|=any(stage['state']=='running' for stage in unfinished)
            if unfinished:pending.append(self._reason(connection,item['id'])['reason'])
        if control['cancel']:state='cancelling' if active or pending else 'cancelled'
        elif not control['enabled'] and pending:state='interrupted'
        elif running or active:state='running'
        elif pending:
            if any(reason is None for reason in pending):state='waiting'
            else:state='paused_auth' if all(reason=='auth' for reason in pending) else 'paused'
        else:state='completed_with_errors' if summary['counts']['partial'] or summary['counts']['failed'] or summary['counts']['interrupted'] else 'completed'
        connection.execute(update(TaskCenterRow).where(TaskCenterRow.id==task_id).values(state=state,updated_at=_now()))
        self._notify_transition(connection,task_id,state,summary['counts'],tuple(sorted({reason for reason in pending if reason})))

    def _notify_transition(self,connection,task_id,state,counts,reasons):
        # A delivery marker, never scheduler/batch authority. It is committed
        # with the note and task state, so polling/recovery cannot resend it.
        event=state if state in ('completed','completed_with_errors','paused_auth','paused') else None
        marker=self._get(connection,'notice',task_id,{'v':1,'event':None})
        if set(marker)!={'v','event'} or marker['event'] not in (None,'completed','completed_with_errors','paused_auth','paused'):
            raise DownloadError('metadata_missing')
        if marker['event']==event:return
        if event is not None:self.notifications.emit_download_transition(connection,task_id=task_id,state=event,counts=counts,reasons=reasons)
        self._put(connection,'notice',task_id,{'event':event})

    def start(self):
        with self._lock:
            if self._thread is not None or self._stop.is_set():return
            self._executor=ThreadPoolExecutor(max_workers=6,thread_name_prefix='download-work')
            self._platform_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='download-browser')
            self._thread=threading.Thread(target=self._run,name='download-scheduler',daemon=False)
            self._thread.start()

    def wake(self):self._wake.set()

    def close(self,timeout=0):
        self._stop.set();self._wake.set()
        with self._lock:thread=self._thread
        if thread is None:
            self._closed_ok=True;return True
        thread.join(max(0,float(timeout)))
        return not thread.is_alive() and self._closed_ok

    def _progress_for(self,key):
        def callback(progress):
            current,total=progress.current,progress.total
            if progress.stage not in ('download','audio','silent','asr','ocr','model_download','model_import','model_verify','local_copy','local_verify'):return
            if type(current) is not int or current<0 or total is not None and (type(total) is not int or total<current):return
            with self._lock:self._progress[key]={'stage':progress.stage,'current':current,'total':total}
            now=self.clock()
            if now-self._heartbeat.get(key,-10)>=1:
                self._heartbeat[key]=now
                with self.engine.begin() as connection:self._put(connection,'heartbeat',key[1],{'tick':now})
        return callback

    def _refresh_models(self):
        rows=self.models.list()
        safe=[{key:row[key] for key in ('id','name','size_bytes','version') if key in row} | {'status':row.get('state',row.get('status','missing'))} for row in rows]
        with self._lock:self._model_cache=safe

    def _refresh_output(self):
        try:value=self.access.read_output()
        except DownloadError:value={'grant_id':None,'display_path':None,'needs_selection':True}
        with self._lock:self._output=value

    def _job_control(self,connection,job_id):
        value=self._get(connection,'job',job_id)
        if (value is None or set(value)!={'v','epoch','enabled','cancel','show_browser','session_ids','cursor'}
            or type(value['epoch']) is not str or any(type(value[key]) is not bool for key in ('enabled','cancel','show_browser'))
            or type(value['session_ids']) is not list or len(value['session_ids'])>20
            or any(type(identity) is not str or len(identity)>64 for identity in value['session_ids'])
            or value['cursor'] is not None and (type(value['cursor']) is not str or len(value['cursor'])>64)):
            raise DownloadError('metadata_missing')
        return value

    def _record(self,task_id,item_id,result):
        with self.engine.begin() as connection:
            self.repository.record_stage(connection,item_id,result);self._refresh(connection,task_id)

    def _set_reason(self,item_id,**changes):
        with self.engine.begin() as connection:
            value=self._reason(connection,item_id);value.update(changes)
            self._put(connection,'item',item_id,value)

    def _validate_resource(self,resource,cancel):
        with _slot(self.local_slot,cancel),self.files.open_resource(resource.id,cancel=cancel,touch=False) as bound:
            duration,_=self.files._validate(bound.path,resource.kind,cancel)
            if duration!=resource.duration_ms:raise DownloadError('media_invalid')
            if self.files._hash(bound.guard,cancel)!=(resource.sha256,resource.size_bytes,bound.journal['identity']):
                raise DownloadError('identity_mismatch')
            bound.verify()
        self._verified_duration(resource)
        return resource

    def _verified_duration(self,resource):
        # Only a successfully promoted or freshly revalidated owned video is
        # authoritative. Provider durations are estimates, not subtitle bounds.
        if resource.kind not in ('source_video','video','preview') or type(resource.duration_ms) is not int or resource.duration_ms<=0:return
        with self.engine.begin() as connection:
            connection.execute(update(DownloadCandidateRow).where(DownloadCandidateRow.id==resource.candidate_id).values(duration_ms=resource.duration_ms))
            platform=connection.execute(select(DownloadCandidateRow.platform).where(DownloadCandidateRow.id==resource.candidate_id)).scalar_one()
        with self._lock:
            self._actual_durations[resource.candidate_id]=resource.duration_ms
            if platform=='local' and resource.kind=='source_video' and resource.item_id is None:
                self._local_ready[resource.candidate_id]=resource.id
            if len(self._actual_durations)>4000:self._actual_durations.pop(next(iter(self._actual_durations)))

    def _recover(self):
        cancel=_Cancel(self)
        # Freeze old unfinished intents before any slow file/model recovery.
        with self.engine.begin() as connection:
            tasks=connection.execute(select(TaskCenterRow.id).where(TaskCenterRow.kind=='download')).scalars().all()
            for task in tasks:
                control=self._control(connection,task)
                if control['epoch']==self._epoch:continue
                summary=self.repository._summary(connection,task)
                control['enabled']=False;control['cancel']|=summary['state']=='cancelled'
                self._put(connection,'control',task,control)
                if control['cancel']:self._settle_cancelled(connection,summary)
                else:
                    for item in summary['items']:
                        for stage,result in item['stages'].items():
                            if result['state'] in ('running','waiting','interrupted'):
                                self.repository.reset_stage(connection,item['id'],stage,intent='recovery',error_code='metadata_missing')
                self._refresh(connection,task)
            jobs=connection.execute(select(DownloadAuxiliaryJobRow.id,DownloadAuxiliaryJobRow.state).where(DownloadAuxiliaryJobRow.state.in_(('waiting','running','interrupted')))).all()
            for job,state in jobs:
                control=self._get(connection,'job',job)
                if control is None:
                    control={'v':1,'epoch':'','enabled':False,'cancel':False,'show_browser':False,'session_ids':[],'cursor':None}
                    self._put(connection,'job',job,control)
                else:control=self._job_control(connection,job)
                if control['epoch']!=self._epoch and state in ('waiting','running','interrupted'):
                    control['enabled']=False;self._put(connection,'job',job,control)
                    row=self.repository._row(connection,DownloadAuxiliaryJobRow,job)
                    local=row['kind']=='resolve' and self.repository._row(connection,DownloadResolutionRow,row['session_id'])['source_kind']=='local'
                    self.repository.record_auxiliary_job(connection,job,state='interrupted',error_code='local_reselect_required' if local else None)
        try:
            _check(cancel)
            self.files.renew_cache_grants()
            _check(cancel);self.files.recover(cancel=cancel)
        except DownloadError as error:
            if cancel.cancelled():raise
            self._recovery_errors.append({'error_code':error.code})
        with self.engine.connect() as connection:
            rows=connection.execute(select(DownloadOutputRow.__table__)).mappings().all()
        for row in rows:
            _check(cancel)
            resource=OutputResource(**{key:row[key] for key in OutputResource.__dataclass_fields__})
            try:self._validate_resource(resource,cancel)
            except DownloadError as error:
                if cancel.cancelled():raise
                code=error.code if error.code in ('source_missing','identity_mismatch','media_invalid') else 'metadata_missing'
                self._recovery_errors.append({'resource_id':resource.id,'error_code':code})
                self._recovery_errors=self._recovery_errors[-512:]
                with self.engine.begin() as connection:
                    if resource.kind=='cover':
                        candidate=self.repository._row(connection,DownloadCandidateRow,resource.candidate_id)
                        if candidate['cover_resource_id']==resource.id:self.repository.attach_cover(connection,resource.candidate_id,None)
                    stages=connection.execute(select(DownloadStageRow.item_id,DownloadStageRow.stage).where(DownloadStageRow.resource_id==resource.id,DownloadStageRow.state=='succeeded')).all()
                    for item_id,stage in stages:
                        item=self.repository._item(connection,item_id)
                        self.repository.reset_stage(connection,item_id,stage,intent='recovery',error_code=code)
                        control=self._control(connection,item['task_id']);control['enabled']=False
                        self._put(connection,'control',item['task_id'],control)
                        if control['cancel']:self._settle_cancelled(connection,self.repository._summary(connection,item['task_id']))
                        self._refresh(connection,item['task_id'])
                    jobs=connection.execute(select(DownloadAuxiliaryJobRow.id).where(DownloadAuxiliaryJobRow.resource_id==resource.id,DownloadAuxiliaryJobRow.state=='succeeded')).scalars().all()
                    for job in jobs:self.repository.record_auxiliary_job(connection,job,state='interrupted',error_code=code)
        with _slot(self.local_slot,cancel):
            self._refresh_models();_check(cancel);self.files.cleanup_previews();_check(cancel)
        self._refresh_output()

    def _run(self):
        try:
            try:self._recover()
            except DownloadError as error:
                if not self._stop.is_set():_LOG.warning('download_recovery_failed code=%s',error.code)
            except Exception:
                _LOG.warning('download_recovery_failed code=metadata_missing')
            finally:self.recovery_complete=True
            while not self._stop.is_set():
                try:self._cycle()
                except DownloadError as error:_LOG.warning('download_scheduler_failed code=%s',error.code)
                except Exception:_LOG.warning('download_scheduler_failed code=metadata_missing')
                self._wake.wait(.05);self._wake.clear()
        finally:
            with self._lock:
                for event in self._events.values():event.set()
            while True:
                self._harvest()
                with self._lock:active=bool(self._active)
                if not active:break
                self._wake.wait(.05);self._wake.clear()
            self._interrupt_remaining()
            # Playwright objects are created, used and repeatedly closed on the
            # same dedicated owner thread; never move a live lease to the bridge.
            while not self._platform_executor.submit(self._close_platform).result():
                time.sleep(.05)
            self._executor.shutdown(wait=True);self._platform_executor.shutdown(wait=True)
            pending=len(self.models.pending_cleanup())
            if pending:_LOG.warning('download_cleanup_pending code=model_invalid residual_count=%d',pending)
            with self._lock:
                ids=list(self._urls)
            for identity in ids:self.media.revoke_read('download:'+identity)
            self._urls.clear();self._cover_reads.clear();self._issued.clear();self._sources.clear();self._sessions.clear();self._cursors.clear()
            self._pending_native.clear();self._local_inputs.clear();self._local_ready.clear();self._closed_ok=True

    def _harvest(self):
        with self._lock:finished=[(key,future,owner) for key,(future,owner) in self._active.items() if future.done()]
        for key,future,owner in finished:
            try:future.result()
            except Exception:_LOG.warning('download_worker_failed code=metadata_missing')
            with self._lock:self._active.pop(key,None);self._events.pop(key,None)
            if owner:
                with self.engine.begin() as connection:self._refresh(connection,owner)

    def _submit(self,key,owner,method,*args):
        event=threading.Event()
        with self._lock:
            self._events[key]=event
            future=self._executor.submit(method,*args,_Cancel(self,event),self._progress_for(key))
            self._active[key]=(future,owner)

    def _cycle(self):
        self._harvest();self._refresh_output()
        if self.clock()-self._last_maintenance>=3600 and self.local_slot.acquire(blocking=False):
            try:
                _check(_Cancel(self));self.files.cleanup_previews();self._last_maintenance=self.clock()
            finally:self.local_slot.release()
        with self.engine.connect() as connection:
            tasks=connection.execute(select(TaskCenterRow.id).where(TaskCenterRow.kind=='download',TaskCenterRow.state.not_in(_FINAL)).order_by(TaskCenterRow.created_at)).scalars().all()
            jobs=connection.execute(select(DownloadAuxiliaryJobRow.id,DownloadAuxiliaryJobRow.state).where(DownloadAuxiliaryJobRow.state.in_(('waiting','running'))).order_by(DownloadAuxiliaryJobRow.created_at)).all()
        for task in tasks:
            with self.engine.connect() as connection:
                control=self._control(connection,task);summary=self.repository._summary(connection,task)
            if control['cancel']:
                self._cancel_batch(task,summary);continue
            if not control['enabled'] or control['epoch']!=self._epoch:continue
            for item in summary['items']:
                with self._lock:
                    if ('item',item['id']) in self._active or len(self._active)>=6:continue
                with self.engine.connect() as connection:reason=self._reason(connection,item['id'])
                if reason['reason'] is not None:continue
                if any(stage['state'] in ('waiting','interrupted') for stage in item['stages'].values()):
                    self._submit(('item',item['id']),task,self._execute_item,task,item['id'])
            with self.engine.begin() as connection:self._refresh(connection,task)
        for job,state in jobs:
            with self.engine.connect() as connection:control=self._job_control(connection,job)
            with self._lock:active=('job',job) in self._active
            if control['cancel']:
                if active:
                    with self._lock:self._events[('job',job)].set()
                elif state=='waiting':
                    with self.engine.begin() as connection:self.repository.record_auxiliary_job(connection,job,state='cancelled',error_code='cancelled')
                    with self._lock:self._local_inputs.pop(job,None)
                continue
            with self._lock:full=len(self._active)>=6
            if state=='waiting' and control['enabled'] and control['epoch']==self._epoch and not active and not full:
                self._submit(('job',job),None,self._execute_job,job)

    def _cancel_batch(self,task_id,summary):
        with self._lock:
            active={key[1] for key,(_,owner) in self._active.items() if key[0]=='item' and owner==task_id}
            for identity in active:self._events[('item',identity)].set()
        with self.engine.begin() as connection:
            self._settle_cancelled(connection,summary,active)
            self._refresh(connection,task_id)

    def _settle_cancelled(self,connection,summary,active=()):
        # Preserve successful/skipped/failed history; only unfinished work is
        # settled. Startup and shutdown call this after old workers have stopped.
        for item in summary['items']:
            if item['id'] in active:continue
            for stage,result in item['stages'].items():
                if result['state'] in ('waiting','running','interrupted'):
                    self.repository.record_stage(connection,item['id'],StageResult(stage,'cancelled',error_code='cancelled'))

    def _interrupt_remaining(self):
        with self.engine.begin() as connection:
            tasks=connection.execute(select(TaskCenterRow.id).where(TaskCenterRow.kind=='download',TaskCenterRow.state.not_in(_FINAL))).scalars().all()
            for task in tasks:
                control=self._control(connection,task);control['enabled']=False;self._put(connection,'control',task,control)
                summary=self.repository._summary(connection,task)
                if control['cancel']:self._settle_cancelled(connection,summary)
                else:
                    for item in summary['items']:
                        for stage,result in item['stages'].items():
                            if result['state'] in ('waiting','running','interrupted'):
                                self.repository.reset_stage(connection,item['id'],stage,intent='recovery',error_code='metadata_missing')
                self._refresh(connection,task)
            jobs=connection.execute(select(DownloadAuxiliaryJobRow.id).where(DownloadAuxiliaryJobRow.state.in_(('waiting','running')))).scalars().all()
            for job in jobs:self.repository.record_auxiliary_job(connection,job,state='interrupted')

    def _provider_call(self,provider,method,*args,cancel,show_browser=False):
        if provider is self.providers.get('direct'):return getattr(provider,method)(*args,cancel=cancel)
        def on_owner():
            self._show_browser=show_browser
            return getattr(provider,method)(*args,cancel=cancel)
        return self._platform_executor.submit(on_owner).result()

    def _douyin(self):
        if 'douyin' in self.providers:return self.providers['douyin']
        runtime=self
        class Reader:
            def read(self,url,*,cancel):
                lease=runtime._browser_lease(show_browser=getattr(runtime,'_show_browser',False),cancel=cancel)
                if getattr(runtime,'_show_browser',False):
                    if lease.verification_required and lease.interactive:
                        lease.complete_verification(cancel=cancel)
                    elif not lease.interactive and (lease.verification_required or
                            getattr(lease.verification_event,'state',None)!='resolved'):
                        lease.begin_verification(url)
                        raise DownloadError('verification_required')
                return lease.read(url,cancel=cancel)
            def read_next(self,url,*,cancel):return runtime._browser_lease(cancel=cancel).read_next(url,cancel=cancel)
        provider=DouyinProvider(self.registry,Reader());self.providers['douyin']=provider
        return provider

    def _browser_lease(self,show_browser=False,*,cancel=None):
        _check(_Cancel(self))
        if cancel is not None:_check(cancel)
        if self._lease is not None and (self._lease.closed or show_browser and not self._lease.visible):
            self._lease.close()
            if self._lease.cleanup_errors:raise DownloadError('account_busy')
            self._lease=None
        if self._lease is None:
            if self._browser_pool is None:
                from yijianjifa.features.accounts.service import AccountService
                from yijianjifa.features.accounts.session_store import SessionStore
                from yijianjifa.features.downloads.account_session import ManagedDownloadSessions
                from yijianjifa.features.downloads.browser import DownloadBrowserPool
                sessions=ManagedDownloadSessions(AccountService(self.engine),SessionStore(self.paths.data_dir/'profiles'))
                self._browser_pool=DownloadBrowserPool(self.paths.data_dir,managed_sessions=sessions)
            self._browser_pool.visible=show_browser
            self._lease=self._browser_pool.acquire(None)
        return self._lease

    def _close_platform(self):
        try:
            provider=self.providers.get('douyin')
            clean=True
            if self._lease is not None:
                self._lease.close()
                clean=not self._lease.cleanup_errors
            retry=getattr(self._browser_pool,'retry_pending_cleanup',None)
            if retry is not None:clean=retry() and clean
            if not clean:return False
            if provider is not None and hasattr(provider,'close'):provider.close()
            direct=self.providers.get('direct')
            if isinstance(direct,DirectProvider):
                for source in direct._sources.values():direct.registry.revoke(source)
                direct._sources.clear()
            return True
        except Exception:
            _LOG.warning('download_browser_cleanup_pending code=account_busy')
            return False

    def _resolve(self,job_id,cancel,progress):
        with self.engine.connect() as connection:
            job=self.repository._row(connection,DownloadAuxiliaryJobRow,job_id);control=self._job_control(connection,job_id)
            resolution=self.repository._row(connection,DownloadResolutionRow,job['session_id'])
        if resolution['source_kind']=='local':return self._resolve_local(job_id,resolution,cancel,progress)
        main=job['session_id'];identities=[];has_more=False;completion='complete';next_cursor=None
        for session_id in control['session_ids']:
            _check(cancel)
            with self._lock:source=self._sources.get(session_id)
            if source is None:
                with self.engine.connect() as connection:row=self.repository._row(connection,DownloadResolutionRow,session_id)
                if row['source_kind']=='direct':raise DownloadError('source_missing')
                source=SourceInput(row['source_kind'],row['source_key'],row['canonical_source'])
            provider=self.providers['direct'] if source.kind=='direct' else self._douyin()
            cursor=None
            if control['cursor']:
                with self._lock:binding=self._cursors.get(control['cursor'])
                if binding is None or binding[0]!=main:raise DownloadError('source_missing')
                cursor=binding[1]
            with _slot(self.network_slots,cancel):
                page=self._provider_call(provider,'resolve',source,cursor,cancel=cancel,show_browser=control['show_browser'])
            if page.completion not in ('partial','complete','limited') or len(page.candidates)>20:raise DownloadError('metadata_missing')
            with self.engine.begin() as connection:
                group=self._session_group(connection,main)
                members=self._catalog_members(connection,session_id)
                all_members={identity for session in group['session_ids'] for identity in self._catalog_members(connection,session)}
                for candidate in page.candidates:
                    cid=self.repository.save_candidate(connection,candidate,durable_id=_id())
                    with self._lock:duration=self._actual_durations.get(cid)
                    if duration is not None:
                        connection.execute(update(DownloadCandidateRow).where(DownloadCandidateRow.id==cid).values(duration_ms=duration))
                    all_members.add(cid)
                    if len(all_members)>500:raise DownloadError('input_limit')
                    self.repository.link_source(connection,cid,source,session_id=session_id);identities.append(cid)
                    members.append(cid)
                    with self._lock:
                        if len(self._issued)>=4000 and cid not in self._issued:raise DownloadError('input_limit')
                        self._issued[cid]=(provider,candidate)
                    if candidate.platform=='douyin' and callable(getattr(provider,'cover',None)):
                        saved=self.repository._row(connection,DownloadCandidateRow,cid)
                        pending=connection.execute(select(DownloadAuxiliaryJobRow.id).where(
                            DownloadAuxiliaryJobRow.candidate_id==cid,DownloadAuxiliaryJobRow.kind=='cover',
                            DownloadAuxiliaryJobRow.state.in_(('waiting','running'))).limit(1)).first()
                        if saved['cover_resource_id'] is None and pending is None:
                            # This independent job must never rewrite resolve completion.
                            self._new_job(connection,'cover',candidate_id=cid)
                self._save_catalog(connection,session_id,members)
                connection.execute(update(DownloadResolutionRow).where(DownloadResolutionRow.id==session_id).values(completion=page.completion,updated_at=_now()))
            if page.has_more:
                if page.cursor is None:raise DownloadError('metadata_missing')
                next_cursor=_id();has_more=True
                with self._lock:self._cursors[next_cursor]=(main,page.cursor)
            if page.completion=='limited':completion='limited'
            elif page.completion=='partial' and completion!='limited':completion='partial'
        with self._lock:
            self._sessions[main]={'candidate_ids':identities,'cursor':next_cursor,'has_more':has_more,'completion':completion}
            if control['cursor']:self._cursors.pop(control['cursor'],None)
        return {'completion':completion}

    def _save_intake(self,connection,session_id,received,errors):
        # Even rejected, synthetic long Unicode filenames fit each finite row.
        blocks=[errors[index:index+5] for index in range(0,len(errors),5)]
        for index,block in enumerate(blocks):self._put(connection,'intake-errors',session_id+':'+str(index),{'items':block})
        self._put(connection,'intake',session_id,{'received':received,'failed':len(errors),'blocks':len(blocks)})

    def _resolve_local(self,job_id,resolution,cancel,progress):
        with self._lock:selections=self._local_inputs.get(job_id)
        if selections is None:raise DownloadError('local_reselect_required')
        main=resolution['id'];members=[];errors=[];received=0
        source=SourceInput('local',resolution['source_key'],None)
        for selected in selections:
            _check(cancel)
            try:
                with _slot(self.local_slot,cancel):cid,resource=receive_file(self,selected,cancel,progress)
                self._verified_duration(resource)
                _check(cancel)
                if cid not in members:members.append(cid)
                received+=1
                with self.engine.begin() as connection:
                    self.repository.link_source(connection,cid,source,session_id=main)
                    self._save_catalog(connection,main,members)
                    self._save_intake(connection,main,received,errors)
            except DownloadError as error:
                if cancel.cancelled():raise
                errors.append({'name':selected['name'],'code':error.code,'message':local_message(error.code)})
                with self.engine.begin() as connection:self._save_intake(connection,main,received,errors)
        _check(cancel)
        with self.engine.begin() as connection:
            connection.execute(update(DownloadResolutionRow).where(DownloadResolutionRow.id==main).values(completion='complete',updated_at=_now()))
        with self._lock:self._sessions[main]={'cursor':None,'has_more':False,'completion':'complete'}
        if not members and errors:raise DownloadError(errors[0]['code'])
        return {'completion':'complete'}

    def _candidate_source(self,candidate_id,cancel,show_browser=False):
        with self.engine.connect() as connection:
            if self.repository._row(connection,DownloadCandidateRow,candidate_id)['platform']=='local':
                raise DownloadError('local_source_missing')
        with self._lock:binding=self._issued.get(candidate_id)
        if binding is None:
            with self.engine.connect() as connection:row=self.repository._row(connection,DownloadCandidateRow,candidate_id)
            if row['platform']=='direct':raise DownloadError('source_missing')
            source=normalize_inputs(row['canonical_source'])[0];provider=self._douyin()
            page=self._provider_call(provider,'resolve',source,None,cancel=cancel,show_browser=show_browser)
            candidate=next((value for value in page.candidates if value.work_id==row['work_id']),None)
            if candidate is None:raise DownloadError('identity_mismatch')
            binding=(provider,candidate)
            with self._lock:self._issued[candidate_id]=binding
        return self._provider_call(binding[0],'media',binding[1],cancel=cancel,show_browser=show_browser)

    def _find_source(self,candidate_id,cancel,*,item_id=None):
        with self.engine.connect() as connection:
            query=select(DownloadOutputRow.__table__).where(DownloadOutputRow.candidate_id==candidate_id,
                DownloadOutputRow.kind.in_(('source_video','video','preview')))
            rows=connection.execute(query.order_by(DownloadOutputRow.created_at.desc()).limit(32)).mappings().all()
        rows=sorted(rows,key=lambda row:row['item_id']!=item_id)
        for row in rows:
            resource=OutputResource(**{key:row[key] for key in OutputResource.__dataclass_fields__})
            try:return self._validate_resource(resource,cancel)
            except DownloadError:
                _check(cancel)
        return None

    def _copy_owned(self,resource,candidate_id,item_id,kind,grant_id,cancel):
        temporary=None
        try:
            with _slot(self.local_slot,cancel):
                temporary=self.files.allocate(candidate_id,kind,item_id=item_id,grant_id=grant_id)
                with self.files.open_resource(resource.id,cancel=cancel) as source,self.files.open_temporary(temporary) as target:
                    reader=source.guard.reader_fd()
                    flags=os.O_WRONLY|getattr(os,'O_NOFOLLOW',0)
                    writer=os.open(temporary if os.name=='nt' else temporary.name,flags,
                        **({} if os.name=='nt' else {'dir_fd':target.guard.parent_fd}))
                    try:
                        info=os.fstat(writer)
                        if (info.st_dev,info.st_ino)!=target.guard.identity:raise DownloadError('identity_mismatch')
                        with os.fdopen(reader,'rb') as stream,os.fdopen(writer,'wb') as output:
                            reader=writer=None
                            while chunk:=stream.read(1024*1024):_check(cancel);output.write(chunk)
                            output.flush();os.fsync(output.fileno())
                        source.verify();target.guard.verify();target.area.verify();target.root.verify()
                    finally:
                        if reader is not None:os.close(reader)
                        if writer is not None:os.close(writer)
                result=self.files.promote(candidate_id,temporary,kind,item_id=item_id,cancel=cancel)
                temporary=None;return result
        finally:
            if temporary is not None:self._release(temporary)

    def _release(self,temporary):
        try:self.files.release_temporary(temporary)
        except DownloadError as error:_LOG.warning('download_temporary_cleanup_pending code=%s',error.code)

    def _ensure_cover(self,candidate_id,cancel,progress):
        with self._lock:binding=self._issued.get(candidate_id)
        if binding is None:raise DownloadError('source_missing')
        temporary=None
        try:
            with _slot(self.network_slots,cancel):
                source=self._provider_call(binding[0],'cover',binding[1],cancel=cancel)
                if source is None:return None
                temporary=self.files.allocate(candidate_id,'cover',grant_id=self.files.cover_cache_grant())
                self.cover_fetcher.download(source,temporary,cancel=cancel,progress=progress)
                # The trusted writer returned successfully. Finish its finite
                # identity handoff even if cancellation just arrived, so owned
                # release can address the new inode. The next slot checks the
                # token before hashing/decoding or publishing anything.
                self.files.confirm_temporary(temporary)
            with _slot(self.local_slot,cancel):
                resource=self.files.promote(candidate_id,temporary,'cover',cancel=cancel)
            temporary=None;return resource
        finally:
            if temporary is not None:self._release(temporary)

    def _ensure_source(self,task_id,item,options,grant_id,cancel,progress,*,preview=False):
        candidate_id=item['candidate_id'];item_id=None if preview else item['id']
        with self.engine.connect() as connection:
            local=self.repository._row(connection,DownloadCandidateRow,candidate_id)['platform']=='local'
        kind='preview' if preview else 'video' if options.save_video else 'source_video'
        resource=self._find_source(candidate_id,cancel,item_id=item_id)
        if resource is not None:
            if local:
                if options.save_video and not preview or options.prefer_clean and not preview:
                    raise DownloadError('unsupported_source')
                if resource.item_id!=item_id or resource.kind!=kind:
                    if preview:grant_id=self.files.local_cache_grant()
                    resource=self._copy_owned(resource,candidate_id,item_id,kind,grant_id,cancel)
                self._verified_duration(resource)
                return resource
            with self.engine.connect() as connection:
                watermark=self._get(connection,'candidate',candidate_id,{'v':1,'watermark':'unknown'})['watermark']
            if not preview:
                with self.engine.connect() as connection:
                    decision=connection.execute(select(DownloadDecisionRow.decision).where(DownloadDecisionRow.item_id==item_id).order_by(DownloadDecisionRow.created_at.desc(),DownloadDecisionRow.id.desc())).scalar()
                if options.prefer_clean and watermark!='clean' and decision!='original':
                    self._set_reason(item_id,reason='watermark',watermark=watermark,error_code=None);return None
                self._set_reason(item_id,watermark='clean' if watermark=='clean' else 'original')
            if resource.item_id!=item_id or resource.kind!=kind:
                resource=self._copy_owned(resource,candidate_id,item_id,kind,grant_id,cancel)
            self._verified_duration(resource);return resource
        if local:raise DownloadError('local_source_missing')
        temporary=None
        try:
            with _slot(self.network_slots,cancel):
                with self.engine.connect() as connection:control=self._control(connection,task_id) if task_id else {'show_browser':False}
                source=self._candidate_source(candidate_id,cancel,control['show_browser'])
                watermark=source.watermark
                if watermark not in ('clean','original','unknown'):raise DownloadError('metadata_missing')
                with self.engine.begin() as connection:self._put(connection,'candidate',candidate_id,{'watermark':watermark})
                if not preview:
                    with self.engine.connect() as connection:
                        decision=connection.execute(select(DownloadDecisionRow.decision).where(DownloadDecisionRow.item_id==item_id).order_by(DownloadDecisionRow.created_at.desc(),DownloadDecisionRow.id.desc())).scalar()
                    if options.prefer_clean and watermark!='clean' and decision!='original':
                        self._set_reason(item_id,reason='watermark',watermark=watermark,error_code=None);return None
                    self._set_reason(item_id,watermark='clean' if watermark=='clean' else 'original')
                temporary=self.files.allocate(candidate_id,kind,item_id=item_id,grant_id=grant_id)
                self.fetcher.download(source,temporary,cancel=cancel,progress=progress)
                self.files.confirm_temporary(temporary,cancel=cancel)
            with _slot(self.local_slot,cancel):result=self.files.promote(candidate_id,temporary,kind,item_id=item_id,cancel=cancel)
            temporary=None;self._verified_duration(result);return result
        finally:
            if temporary is not None:self._release(temporary)

    def _conversion(self,source,kind,cancel,progress):
        temporary=None
        try:
            temporary=self.processor.run(source,kind,cancel=cancel,progress=progress)
            self.files.confirm_temporary(temporary,cancel=cancel)
            resource=self.files.promote(source.candidate_id,temporary,{'mp3':'audio_mp3','wav':'audio_wav','silent':'silent_video'}[kind],item_id=source.item_id,cancel=cancel)
            temporary=None;return resource
        finally:
            if temporary is not None:self._release(temporary)
            self.files.reconcile_allocations()

    def _recognition(self,source,stage,options,region,cancel,progress):
        temporary=None;audio=None
        try:
            required=(MODEL_IDS[0],) if stage=='asr' else tuple(DEFAULT_OCR_MODEL_IDS)
            if not set(required)<=set(options.model_ids):raise DownloadError('model_required')
            for identity in required:_check(cancel);self.models.require(identity)
            if stage=='asr':
                audio=self.processor.run(source,'asr_wav',cancel=cancel,progress=progress)
                self.files.confirm_temporary(audio,cancel=cancel)
                segments=self.asr.transcribe(audio,model_id=MODEL_IDS[0],language=options.asr_language,cancel=cancel,progress=progress)
            else:
                video=self.files.path_for(source.id,cancel=cancel)
                segments=self.ocr.recognize(video,region=region,model_ids=required,cancel=cancel,progress=progress)
            _check(cancel)
            content='\n'.join(segment.text for segment in segments)
            if not content.strip():raise DownloadError('no_speech' if stage=='asr' else 'no_text')
            temporary=self.files.allocate(source.candidate_id,stage,item_id=source.item_id,grant_id=self.files._journal(source.id)['grant_id'])
            with self.files.open_temporary(temporary) as target:
                flags=os.O_WRONLY|getattr(os,'O_NOFOLLOW',0)
                writer=os.open(temporary if os.name=='nt' else temporary.name,flags,
                    **({} if os.name=='nt' else {'dir_fd':target.guard.parent_fd}))
                with os.fdopen(writer,'w',encoding='utf-8',newline='\n') as output:
                    info=os.fstat(output.fileno())
                    if (info.st_dev,info.st_ino)!=target.guard.identity:raise DownloadError('identity_mismatch')
                    output.write(content);output.flush();os.fsync(output.fileno())
                target.guard.verify();target.area.verify();target.root.verify()
            resource=self.files.promote(source.candidate_id,temporary,stage,item_id=source.item_id,cancel=cancel)
            temporary=None;return resource,segments
        finally:
            if audio is not None:self._release(audio)
            if temporary is not None:self._release(temporary)
            self.files.reconcile_allocations()

    def _execute_item(self,task_id,item_id,cancel,progress):
        with self.engine.connect() as connection:
            summary=self.repository._summary(connection,task_id)
            frozen=next(item for item in summary['snapshot']['items'] if item['id']==item_id)
        options=DownloadOptions(**{**frozen['options'],'model_ids':tuple(frozen['options']['model_ids'])})
        region=Region(**frozen['region'])
        try:
            _check(cancel)
            if 'video' in frozen['stages']:
                stage=next(item for item in summary['items'] if item['id']==item_id)['stages']['video']
                if stage['state']!='succeeded':self._record(task_id,item_id,StageResult('video','running'))
            source=self._ensure_source(task_id,frozen,options,summary['snapshot']['directory_grant_id'],cancel,progress)
            if source is None:
                if 'video' in frozen['stages']:
                    with self.engine.connect() as connection:
                        state=connection.execute(select(DownloadStageRow.state).where(DownloadStageRow.item_id==item_id,DownloadStageRow.stage=='video')).scalar_one()
                    if state!='skipped':self._record(task_id,item_id,StageResult('video','waiting'))
                return
            self._set_reason(item_id,source_id=source.id)
            if 'video' in frozen['stages']:
                self._record(task_id,item_id,StageResult('video','succeeded',resource_id=source.id))
            for stage in frozen['stages']:
                if stage=='video':continue
                _check(cancel)
                with self.engine.connect() as connection:
                    current=connection.execute(select(DownloadStageRow.state).where(DownloadStageRow.item_id==item_id,DownloadStageRow.stage==stage)).scalar_one()
                if current not in ('waiting','interrupted'):continue
                self._record(task_id,item_id,StageResult(stage,'running'))
                try:
                    with _slot(self.local_slot,cancel):
                        if stage in ('audio','silent'):
                            resource=self._conversion(source,options.audio_format if stage=='audio' else 'silent',cancel,progress)
                            segments=None
                        else:resource,segments=self._recognition(source,stage,options,region,cancel,progress)
                    with self.engine.begin() as connection:
                        if segments is not None:
                            original=self.repository.save_subtitle_original(connection,source.candidate_id,item_id,stage,segments)
                            self.repository.create_subtitle_draft(connection,original)
                        self.repository.record_stage(connection,item_id,StageResult(stage,'succeeded',resource_id=resource.id));self._refresh(connection,task_id)
                except DownloadError as error:
                    if cancel.cancelled():raise
                    if error.code in _MODELS:
                        self._set_reason(item_id,reason='model',error_code=error.code)
                        self._record(task_id,item_id,StageResult(stage,'waiting',error_code=error.code))
                    else:self._record(task_id,item_id,StageResult(stage,'failed',error_code=error.code))
        except DownloadError as error:
            with self.engine.begin() as connection:
                summary=self.repository._summary(connection,task_id)
                state=next(item for item in summary['items'] if item['id']==item_id)
                reason='auth' if error.code in _AUTH else None
                if reason:self._put(connection,'item',item_id,{**self._reason(connection,item_id),'reason':reason,'error_code':error.code})
                for stage,result in state['stages'].items():
                    if result['state'] in ('succeeded','skipped','cancelled','failed'):continue
                    outcome='waiting' if reason else 'interrupted' if self._stop.is_set() else 'cancelled' if error.code=='cancelled' else 'failed'
                    self.repository.record_stage(connection,item_id,StageResult(stage,outcome,error_code=None if reason else error.code))
                self._refresh(connection,task_id)
        except Exception:
            with self.engine.begin() as connection:
                summary=self.repository._summary(connection,task_id)
                item=next(item for item in summary['items'] if item['id']==item_id)
                for stage,result in item['stages'].items():
                    if result['state'] in ('waiting','running','interrupted'):
                        self.repository.record_stage(connection,item_id,StageResult(stage,'failed',error_code='metadata_missing'))
                self._refresh(connection,task_id)
            _LOG.warning('download_item_failed code=metadata_missing')
        finally:self._wake.set()

    def _private_grant(self,identity,operation):
        with self.engine.connect() as connection:value=self._get(connection,'private',identity)
        if (value is None or set(value)!={'v','operation','path','identity'} or value['operation']!=operation
            or type(value['path']) is not str or len(value['path'])>4000 or not Path(value['path']).is_absolute()
            or type(value['identity']) is not list or len(value['identity'])!=2 or any(type(number) is not int for number in value['identity'])):
            raise DownloadError('metadata_missing')
        return value

    def _execute_job(self,job_id,cancel,progress):
        result={}
        with self.engine.begin() as connection:self.repository.record_auxiliary_job(connection,job_id,state='running')
        row=self.repository.get_auxiliary_job(job_id)
        try:
            _check(cancel);kind=row['kind']
            if kind=='resolve':result=self._resolve(job_id,cancel,progress)
            elif kind=='cover':
                resource=self._ensure_cover(row['candidate_id'],cancel,progress)
                if resource is None:
                    _check(cancel)
                    with self.engine.begin() as connection:self.repository.record_auxiliary_job(connection,job_id,state='skipped')
                    return
                result={'resource_id':resource.id}
            elif kind=='preview':
                with self.engine.connect() as connection:
                    self.repository._row(connection,DownloadCandidateRow,row['candidate_id'])
                item={'candidate_id':row['candidate_id'],'id':None}
                output=self.access.read_output()
                resource=self._ensure_source(None,item,DownloadOptions(),output['grant_id'],cancel,progress,preview=True)
                result={'resource_id':resource.id}
            elif kind=='model_download':
                with _slot(self.local_slot,cancel):
                    self.models.install(row['model_id'],cancel=cancel,progress=progress);self._refresh_models()
                result={'installed_model_id':row['model_id']}
            elif kind=='model_import':
                grant=self._private_grant(row['input_grant_id'],'download.models.import')
                with _slot(self.local_slot,cancel),_Guard(grant['path'],directory=False) as selected:
                    if list(selected.identity)!=grant['identity']:raise DownloadError('identity_mismatch')
                    self.models.import_file(row['model_id'],selected.path,cancel=cancel,progress=progress)
                    selected.verify();self._refresh_models()
                result={'installed_model_id':row['model_id']}
            elif kind=='subtitle_export':
                grant=self._private_grant(row['output_grant_id'],'download.subtitle.export');target=Path(grant['path'])
                with _slot(self.local_slot,cancel),_Guard(target.parent) as selected:
                    if list(selected.identity)!=grant['identity']:raise DownloadError('identity_mismatch')
                    resource=self.access.export_subtitle(self.subtitles,row['subtitle_original_id'],row['subtitle_origin'],row['export_format'],target,
                        cancel=cancel,progress=progress,expected_revision=row['subtitle_revision'])
                    selected.verify()
                result={'resource_id':resource.id}
            elif kind in ('resource_preview','resource_reveal','resource_import','resource_delete'):
                identity=row['output_resource_id']
                with _slot(self.local_slot,cancel):
                    if kind=='resource_preview':
                        if self.repository.get_output(identity).kind=='cover':self._cover_read_room(identity)
                        try:response=self.access.preview(identity)
                        except ValueError as error:
                            # At the shared 64-owner ceiling, a cover may yield
                            # one slot. Never revoke a player/library owner or
                            # retry unrelated validation failures.
                            if str(error)!='preview limit' or not self._yield_cover_read():raise
                            _check(cancel);response=self.access.preview(identity)
                        if cancel.cancelled():
                            self.media.revoke_read('download:'+identity);raise DownloadError('cancelled')
                        self._remember_preview(identity,response)
                    elif kind=='resource_reveal':self.access.reveal(identity)
                    elif kind=='resource_import':result={'library_id':self.access.import_video(identity,tuple(row['category_ids']),cancel=cancel)['asset_id']}
                    else:
                        self.access.delete_file(identity,auxiliary_job_id=job_id)
                        with self._lock:self._urls.pop(identity,None)
            else:raise DownloadError('unsupported_source')
            _check(cancel)
            with self.engine.begin() as connection:
                if kind=='cover':self.repository.attach_cover(connection,row['candidate_id'],result['resource_id'])
                self.repository.record_auxiliary_job(connection,job_id,state='succeeded',**result)
        except DownloadError as error:
            state='interrupted' if self._stop.is_set() else 'cancelled' if error.code=='cancelled' else 'failed'
            with self.engine.begin() as connection:self.repository.record_auxiliary_job(connection,job_id,state=state,error_code=error.code)
        except Exception:
            with self.engine.begin() as connection:self.repository.record_auxiliary_job(connection,job_id,state='failed',error_code='metadata_missing')
            _LOG.warning('download_job_failed code=metadata_missing')
        finally:
            with self._lock:self._local_inputs.pop(job_id,None)
            self._wake.set()
