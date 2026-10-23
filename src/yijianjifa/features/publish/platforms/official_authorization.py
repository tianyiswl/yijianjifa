"""Trusted official execution boundary and host-only disabled factory.

Desktop adapters consume only safe observations and opaque operation handles.
ClientExecutionService is the operator-side synchronous execution library, not
an OAuth/backend product. Deploy its credential-bearing client/executor on the
trusted service; native installation never creates a signer or stores tokens.
"""
from dataclasses import dataclass
import copy
import hashlib
import json
from pathlib import Path
import re
import secrets
import struct
import threading
import time
from typing import Protocol
import zlib

from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.publish.coordinator import AdapterValidation,AdapterOutcome
from yijianjifa.features.publish.deliveries import PublishDeliveryRow,digest
from yijianjifa.features.library.media_identity import sha256_file
from .official_http import ProtocolError,remaining,reference,text
from .bilibili.client import BilibiliClient
from .kuaishou.client import KuaishouClient


@dataclass(frozen=True)
class IdentityBinding:
    account_id: str
    platform: str
    issuer: str
    subject: str
    canonical_uid: str


@dataclass(frozen=True)
class GrantObservation:
    issuer: str
    subject: str
    canonical_uid: str
    scopes: frozenset


class OfficialService(Protocol):
    """Authenticated host-to-service interface; none are public browser ops.

    binding is operator-maintained issuer identity mapping; observe performs
    actual identity/permission readback, never trusts a desktop valid flag.
    Every call is synchronous/finite; no detached RPC/platform request may
    survive its return. Final execution invokes callback immediately before
    sending immutable final bytes and durably deduplicates operation identity.
    Implementations with remote jobs must supply joined/supervised ownership
    proof rather than claiming a timed-out RPC means stopped remote worker.
    """
    def binding(self,account_id):...
    def observe(self,account_id,*,deadline):...
    def category_choices(self):...
    def stage(self,binding,payload,*,deadline):...
    def execute_final(self,handle,*,before_final,deadline):...
    def query(self,binding,payload,context,*,deadline):...


_REQUIRED={'bilibili':frozenset({'USER_INFO','ARC_BASE'}),'kuaishou':frozenset({'user_info','user_video_publish'})}
_QUERY={'bilibili':frozenset({'USER_INFO','ARC_BASE'}),'kuaishou':frozenset({'user_info','user_video_info'})}
_CAPTION_HELP='快手说明按标题、正文、标签顺序，以换行连接非空原文；不截断。需要已保存并冻结的首帧封面；无图像时保留待配置。'


def pending_descriptor(platform):
    if platform=='kuaishou':
        return {'reason':'official_service_not_configured','field_schema':{k:{'type':'string','help':_CAPTION_HELP} for k in ('title','body','tags')}}
    if platform=='bilibili':
        return {'reason':'official_service_not_configured','field_schema':{
            'title':{'type':'string','max_length':79,'help':'需官方授权服务与当前二级分区；封面上传签名待核实，已选封面保持待处理。'},
            'body':{'type':'string','max_length':249},'tags':{'type':'string','max_length':199,'help':'标签必填，用英文逗号分隔；不截断。'},
            'category_id':{'choices':[],'help':'二级分区须从当前授权服务的官方分区查询取得。'},'copyright':{'type':'string'},'source':{'type':'string'}}}
    if platform=='channels':
        return {'reason':'official_contract_pending','field_schema':{},'prerequisites':[
            '确认平台允许的正式接入方式与账号资格', '取得官方身份、上传、最终提交与原稿结果核对的请求及回执证据',
            '取得具体账号与内容的本次操作授权']}
    return {'reason':'adapter_not_integrated','field_schema':{}}


def _safe_choices(value):
    if type(value) is not list or len(value)>1000:raise ProtocolError('configuration_required')
    seen=set();result=[]
    for choice in value:
        if type(choice) is not dict or set(choice)!={'id','label'} or type(choice['id']) is not int or choice['id']<=0 or choice['id'] in seen:raise ProtocolError('configuration_required')
        seen.add(choice['id']);result.append({'id':choice['id'],'label':text(choice['label'])})
    return result


def _matches(expected,observed,uid,required):
    return (isinstance(expected,IdentityBinding) and isinstance(observed,GrantObservation) and
        type(observed.scopes) is frozenset and all(type(s) is str for s in observed.scopes) and
        (observed.issuer,observed.subject,observed.canonical_uid)==(expected.issuer,expected.subject,expected.canonical_uid) and
        expected.canonical_uid==uid and required<=observed.scopes and
        all(type(x) is str and x for x in (expected.issuer,expected.subject,expected.canonical_uid)))


def _owned_cover(payload,*,planned=False):
    selected=payload['fields'].get('cover');source=payload.get('source_cover')
    if type(selected) is not dict or type(source) is not dict:return False
    if selected.get('kind')!='first_frame' or type(selected.get('frame_ms')) is not int or selected['frame_ms']!=0 or source.get('kind')!='first_frame' or type(source.get('frame_ms')) is not int or source.get('frame_ms')!=0 or source.get('missing') is True:return False
    if not re.fullmatch(r'[a-f0-9]{64}',str(source.get('sha256',''))):return False
    if 'sha256' in selected and selected['sha256']!=source['sha256']:return False
    if planned:return True  # save creates owned resource/path, not prepare.
    path=source.get('path')
    if not source.get('resource_id') or type(path) is not str:return False
    try:return Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest()==source['sha256']
    except OSError:return False


def _png(path):
    """Validate frozen PNG bytes, CRC and bounded decoded scanlines; no conversion."""
    raw=Path(path).read_bytes()
    if not raw.startswith(b'\x89PNG\r\n\x1a\n') or not 0<len(raw)<10_000_000:raise ProtocolError('configuration_required')
    offset=8;image=bytearray();expected=None;ended=False
    while offset<len(raw):
        if offset+12>len(raw):raise ProtocolError('configuration_required')
        size=struct.unpack('!I',raw[offset:offset+4])[0];kind=raw[offset+4:offset+8];end=offset+8+size
        if end+4>len(raw) or zlib.crc32(raw[offset+4:end])&0xffffffff!=struct.unpack('!I',raw[end:end+4])[0]:raise ProtocolError('configuration_required')
        data=raw[offset+8:end]
        if kind==b'IHDR':
            if expected is not None or size!=13 or offset!=8:raise ProtocolError('configuration_required')
            width,height,depth,color,compression,filtering,interlace=struct.unpack('!IIBBBBB',data)
            channels={0:1,2:3,4:2,6:4}.get(color)
            if not channels or depth!=8 or compression or filtering or interlace or not 0<width*height<=16_000_000:raise ProtocolError('configuration_required')
            expected=height*(width*channels+1)
        elif kind==b'IDAT':image.extend(data)
        elif kind==b'IEND':
            if size or end+4!=len(raw):raise ProtocolError('configuration_required')
            ended=True
        offset=end+4
    if not ended or expected is None:raise ProtocolError('configuration_required')
    try:
        decoder=zlib.decompressobj();decoded=decoder.decompress(bytes(image),expected+1)
        if len(decoded)!=expected or not decoder.eof or decoder.unused_data:raise ProtocolError('configuration_required')
        if any(decoded[offset]>4 for offset in range(0,expected,width*channels+1)):raise ProtocolError('configuration_required')
    except zlib.error:raise ProtocolError('configuration_required') from None


def observe_bilibili_grant(binding,client,*,deadline):
    """Read actual scoped openid under the operator's matching signer issuer."""
    if binding.platform!='bilibili' or not client.issuer or client.issuer!=binding.issuer:raise ProtocolError('account_mismatch')
    subject,scopes=client.identity_scopes(deadline=deadline)
    uid=binding.canonical_uid if subject==binding.subject else ''
    return GrantObservation(client.issuer,subject,uid,scopes)


class ClientExecutionService:
    """Operator-side composition, usable locally with synthetic clients.

    Production needs durable one-shot storage keyed by full binding and the
    service's own stopped-job evidence. This in-memory implementation must not
    be used as a restart-safe hosted relay; final restart without its operation
    proof conservatively returns unknown. No retries are performed.
    """
    def __init__(self,*,bindings,client_for,observe,category_choices=()):
        self._bindings=dict(bindings);self._client_for,self._observe=client_for,observe
        self._choices=_safe_choices(list(category_choices));self._operations={};self._by_binding={};self._lock=threading.Lock()
        for key,binding in self._bindings.items():
            if not isinstance(binding,IdentityBinding) or key!=binding.account_id or binding.platform not in _REQUIRED:raise ProtocolError('configuration_required')
    def binding(self,account_id):return self._bindings.get(account_id)
    def observe(self,account_id,*,deadline):remaining(deadline);return self._observe(account_id,deadline)
    def category_choices(self):return copy.deepcopy(self._choices)
    def _check(self,payload,required,*,deadline):
        expected=self.binding(payload['account_id'])
        if expected is None or expected.platform!=payload['platform'] or not _matches(expected,self.observe(payload['account_id'],deadline=deadline),payload['platform_user_id'],required):raise ProtocolError('permission_missing')
    def stage(self,binding,payload,*,deadline):
        if (type(binding) is not tuple or len(binding)!=4 or type(binding[2]) is not int or binding[2]<=0 or binding[3]!=digest(payload)):
            raise ProtocolError('configuration_required')
        self._check(payload,_REQUIRED[payload['platform']],deadline=deadline)
        client=self._client_for(payload['account_id'])
        if getattr(client,'platform',None)!=payload['platform'] or getattr(client,'issuer',None)!=self.binding(payload['account_id']).issuer:raise ProtocolError('configuration_required')
        if payload['platform']=='bilibili':
            BilibiliClient.submit_fields(payload['fields'])
            choices=client.categories(deadline=deadline)
            if payload['fields']['category_id'] not in {c['id'] for c in choices}:raise ProtocolError('configuration_required')
        else:
            if not _owned_cover(payload):raise ProtocolError('configuration_required')
            _png(payload['source_cover']['path'])
            KuaishouClient.caption(payload['fields'])
        upload=client.upload(payload['video_path'],deadline=deadline)
        request=client.final_request(upload,payload['fields']) if payload['platform']=='bilibili' else client.final_request(upload,KuaishouClient.caption(payload['fields']),payload['source_cover']['path'])
        with self._lock:
            key=tuple(binding)
            if key in self._by_binding:raise ProtocolError('result_unknown')
            handle=secrets.token_hex(24)
            self._operations[handle]={'binding':key,'payload':copy.deepcopy(payload),'client':client,'request':request,'started':False,'outcome':None}
            self._by_binding[key]=handle
        return handle
    def execute_final(self,handle,*,before_final,deadline):
        with self._lock:
            operation=self._operations.get(handle)
            if operation is None:raise ProtocolError('result_unknown')
            if operation['started']:return operation['outcome'] or AdapterOutcome('result_unknown',context={'handle':handle},error_code='result_unknown')
            operation['started']=True
        remaining(deadline)
        # Native callback reobserves identity and atomically marks SQL final.
        before_final()
        try:
            receipt=operation['client'].execute_final(operation['request'],deadline=deadline)
            outcome=AdapterOutcome('platform_accepted',context={'handle':handle},receipt_reference=receipt)
        except ProtocolError as error:
            outcome=AdapterOutcome('result_unknown',context={'handle':handle},error_code=str(error))
        except Exception:
            # Coordinator durable marker determines pre-final failure vs unknown.
            raise ProtocolError() from None
        operation['outcome']=outcome
        return outcome
    def query(self,binding,payload,context,*,deadline):
        self._check(payload,_QUERY[payload['platform']],deadline=deadline)
        key=tuple(binding)
        with self._lock:
            handle=self._by_binding.get(key);operation=self._operations.get(handle)
        if operation is None or (context and context.get('handle')!=handle):return AdapterOutcome('result_unknown',error_code='result_unknown')
        outcome=operation['outcome'];receipt=payload.get('receipt_reference') or (outcome.receipt_reference if outcome else None)
        if not receipt or not outcome or receipt!=outcome.receipt_reference:return AdapterOutcome('result_unknown',error_code='result_unknown')
        state=operation['client'].query(receipt,deadline=deadline)
        return AdapterOutcome(state,receipt_reference=receipt,error_code='result_unknown' if state=='result_unknown' else None)


class OfficialAdapter:
    verified_capabilities=frozenset({'ordinary','publish_now','result_query'})
    def __init__(self,engine,platform,service,*,budget_s=30):
        if platform not in _REQUIRED or not 0<budget_s<=120:raise ProtocolError('configuration_required')
        self.engine,self.platform_id,self.service,self.budget_s=engine,platform,service,budget_s
        self._binding=None;self._deadline=None;self._closed=threading.Event()
        self.public_field_schema=pending_descriptor(platform)['field_schema']
        if platform=='bilibili':self.public_field_schema['category_id']['choices']=_safe_choices(service.category_choices())
    def plan_readiness(self,payload):
        if self.service.binding(payload['account_id']) is None:return 'official_grant_not_configured'
        if payload['execution_mode']=='platform_schedule':return 'native_schedule_not_supported'
        try:
            if self.platform_id=='bilibili':
                fields=payload['fields']
                if not fields.get('category_id'):return 'category_required'
                if 'copyright' not in fields:return 'copyright_required'
                if fields['copyright'] in {'repost',2} and not fields.get('source','').strip():return 'repost_source_required'
                if fields.get('cover') is not None:return 'cover_upload_not_supported'
                BilibiliClient.submit_fields(payload['fields'])
                choices=_safe_choices(self.service.category_choices())
                if payload['fields']['category_id'] not in {c['id'] for c in choices}:return 'category_list_required'
            else:
                KuaishouClient.caption(payload['fields'])
                if not _owned_cover(payload,planned=True):return 'owned_cover_required'
        except (ProtocolError,KeyError,TypeError):return 'official_fields_require_configuration'
        return None
    def bind_delivery(self,*,digest,delivery_id,task_id):
        with session_scope(self.engine) as s:
            row=s.get(PublishDeliveryRow,delivery_id)
            if row is None or row.request_digest!=digest or row.task_id!=task_id or row.platform!=self.platform_id:raise ProtocolError()
            self._binding=(task_id,delivery_id,row.attempt,digest)
        self._deadline=time.monotonic()+self.budget_s
    def validate_target(self,target,payload,execution):
        if self._closed.is_set() or self._binding is None or execution=='platform_schedule':return AdapterValidation('',False)
        try:
            expected=self.service.binding(payload['account_id']);observation=self.service.observe(payload['account_id'],deadline=self._deadline)
            valid=expected is not None and expected.platform==self.platform_id and payload['platform']==self.platform_id and _matches(expected,observation,payload['platform_user_id'],_REQUIRED[self.platform_id])
            return AdapterValidation(observation.canonical_uid if isinstance(observation,GrantObservation) else '',valid)
        except Exception:return AdapterValidation('',False)
    def submit(self,payload,*,context,before_final):
        try:
            if self._binding is None or self.plan_readiness(payload):raise ProtocolError('configuration_required')
            if sha256_file(Path(payload['video_path']))!=payload['video_hash']:raise ProtocolError()
            handle=self.service.stage(self._binding,payload,deadline=self._deadline)
            reference(handle)  # bounded opaque handle, not an upload/OAuth token.
            if self._closed.is_set():raise ProtocolError()
            return self.service.execute_final(handle,before_final=before_final,deadline=self._deadline)
        except ProtocolError as error:return AdapterOutcome('pre_submit_failed',error_code=str(error))
        except Exception:return AdapterOutcome('pre_submit_failed',error_code='adapter_failed')
    def reconcile(self,payload,*,context):
        # Restart path does not call bind_delivery. Rebuild only trusted SQL identity.
        with session_scope(self.engine) as s:
            key=digest({k:v for k,v in payload.items() if k!='receipt_reference'})
            rows=list(s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.request_digest==key,PublishDeliveryRow.platform==self.platform_id)))
            if len(rows)!=1:return AdapterOutcome('result_unknown',error_code='result_unknown')
            row=rows[0]
            binding=(row.task_id,row.id,row.attempt,row.request_digest)
        try:return self.service.query(binding,payload,context,deadline=time.monotonic()+self.budget_s)
        except Exception:return AdapterOutcome('result_unknown',error_code='result_unknown')
    def request_shutdown(self):self._closed.set()
    def close(self):self.request_shutdown()


def make_official_adapters(engine,*,service=None,budget_s=30):
    """Native host injection only. No service means no network/new transport.

    Service implements the trusted protocol and operator identity mapping;
    there are no environment credentials, settings flags or page providers.
    Runtime readiness proves configured code support, not live acceptance.
    """
    if service is None:return {}
    required=('binding','observe','category_choices','stage','execute_final','query')
    if any(not callable(getattr(service,name,None)) for name in required):raise ProtocolError('configuration_required')
    return {platform:OfficialAdapter(engine,platform,service,budget_s=budget_s) for platform in _REQUIRED}
