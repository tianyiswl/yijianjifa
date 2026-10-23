"""Transient, identity-bound form checks on the existing native browser worker.

This controller cannot save a publishing task or grant final authority. The
dedicated platform adapter also fences network writes in inspection contexts.
"""
import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid

from sqlalchemy import select
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from .finished_covers import FinishedCovers
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, digest
from yijianjifa.storage.db import session_scope
from .library_queries import CommandRejected, InvalidRequest


TERMINAL={'inspection_complete','failed','cancelled'}
ERRORS={
    'account_mismatch':'检测到账号身份或登录资料已变更，请重新选择账号后检查。',
    'permission_missing':'当前账号尚不满足此平台的发布要求。',
    'configuration_required':'此平台发布资料尚未设置完整，请补齐后再检查。',
    'timeout':'平台页面在检查时未及时响应，本次未发表。',
    'inspection_final_blocked':'检查过程中出现发表动作，已拦截并结束检查。',
    'inspection_media_changed':'视频或封面已经变化，请重新选择后检查。',
    'inspection_failed':'上传或表单检查未完成，本次未发表。请核对账号和发布资料。',
    'inspection_verification_expired':'账号验证等待已超时，本次未发表。请重新检查。',
}


class InspectionFinalBlocked(ValueError):pass
class InspectionMediaChanged(ValueError):pass


class InspectionJobs:
    def __init__(self,runtime):
        self.runtime=runtime;self.lock=threading.RLock();self.jobs={};self.tokens={}

    def busy_accounts(self):
        with self.lock:return {j['payload']['account_id'] for j in self.jobs.values()
            if j['state'] not in TERMINAL or j.get('cleanup_failed') is True}

    def _adapter(self,platform):
        if platform not in {'channels','bilibili','kuaishou'}:raise CommandRejected('inspection_unavailable')
        adapter=self.runtime.catalog.transport_for(platform)
        if not callable(getattr(adapter,'create_inspection_adapter',None)):raise CommandRejected('inspection_unavailable')
        return adapter

    def start(self,prepared_id):
        if self.runtime.closed.is_set():raise CommandRejected('app_closing')
        with self.lock:
            previous=self.tokens.get(prepared_id)
            if previous:
                if previous not in self.jobs:raise CommandRejected('inspection_expired')
                return {**self.status(previous),'accepted':True}
            with self.runtime.service._lock:record=copy.deepcopy(self.runtime.service._drafts.get(prepared_id))
            if record is None or record['expires']<=datetime.now(timezone.utc):raise CommandRejected('inspection_expired')
            snap=record['snapshot']
            if len(snap['videos'])!=1 or len(snap['accounts'])!=1 or len(snap['items'])!=1 or snap.get('excluded_targets'):
                raise CommandRejected('inspection_selection_required')
            item=snap['items'][0];payload=copy.deepcopy(item['payload'])
            if item['state']!='waiting':raise CommandRejected('inspection_configuration_required')
            self._adapter(payload['platform'])
            if self.busy_accounts():raise CommandRejected('inspection_busy')
            # Never consume an existing authorized publishing worker's account.
            from .multi_publish_runtime import AUTH, authorized_targets
            from yijianjifa.storage.models import Setting
            with session_scope(self.runtime.engine) as db:
                for setting in db.scalars(select(Setting).where(Setting.key.startswith(AUTH))):
                    for did in authorized_targets(db,setting.key[len(AUTH):]):
                        delivery=db.get(PublishDeliveryRow,did)
                        if delivery.account_id==payload['account_id'] and self.runtime.service.delivery_state(did) in {'waiting','running','paused_auth','uploading','submitting'}:
                            raise CommandRejected('inspection_busy')
            while len(self.jobs)>=16:
                old=next((key for key,j in self.jobs.items() if j['state'] in TERMINAL and not j.get('cleanup_failed')),None)
                if old is None:raise CommandRejected('inspection_busy')
                self.jobs.pop(old)
            iid=uuid.uuid4().hex;account=AccountService(self.runtime.engine).get(payload['account_id'])
            self.jobs[iid]={'inspection_id':iid,'payload':payload,'video':copy.deepcopy(snap['videos'][0]),
                'state':'queued','stage':'检查已开始，正在等待上传','message':'仅检查，不发表。',
                'account_name':account.nickname,'platform':payload['platform'],'report':[], 'error_code':None,
                'cancel':threading.Event(),'adapter':None,'context':None,'media':None,'cleanup_failed':False}
            self.tokens[prepared_id]=iid
        try:self.runtime.enqueue('inspection_start',iid)
        except Exception:
            with self.lock:self.jobs[iid].update(state='failed',stage='检查未开始',error_code='inspection_failed',message=ERRORS['inspection_failed'])
            raise
        return {**self.status(iid),'accepted':True}

    def current(self):
        with self.lock:
            active=next((key for key,j in self.jobs.items() if j['state'] not in TERMINAL),None)
            return {'inspection':self.status(active) if active else None}

    def status(self,iid):
        with self.lock:
            j=self.jobs.get(iid)
            if j is None:raise InvalidRequest()
            result={k:copy.deepcopy(j[k]) for k in ('inspection_id','state','stage','message','account_name','platform','report','error_code')}
            adapter=j['adapter']
            if adapter and j['state']=='running':
                with getattr(adapter,'_lock',self.lock):
                    sessions=getattr(adapter,'sessions',{})
                    for session in sessions.values():
                        stage=getattr(session,'stage',None)
                        if type(stage) is str and len(stage)<=200:result['stage']=stage
            result['submitted']=False
            return result

    def _verify_source(self,j):
        p=j['payload'];v=j['video'];account=AccountService(self.runtime.engine).get(p['account_id'])
        if (account.platform!=p['platform'] or account.platform_user_id!=p['platform_user_id'] or
            hashlib.sha256((account.session_ref or '').encode()).hexdigest()!=p.get('session_fingerprint')):
            raise ValueError('account_mismatch')
        video=FinishedVideoService(self.runtime.engine).get(p['video_id'])
        if video is None or video.content_hash!=p['video_hash'] or not video.effective_path.is_file():raise InspectionMediaChanged()
        if sha256_file(video.effective_path)!=p['video_hash']:raise InspectionMediaChanged()
        cover,cover_path=FinishedCovers(self.runtime.engine,self.runtime.paths).read(video.id)
        if FinishedCovers._saved(self.runtime.engine,video.id)!=v['source_cover']:raise InspectionMediaChanged()
        if bool(cover_path)!=bool(v['cover_path']):raise InspectionMediaChanged()
        if cover_path and sha256_file(cover_path)!=v['cover']['sha256']:raise InspectionMediaChanged()
        return video.effective_path,cover_path

    def _stage_media(self,j):
        source,cover=self._verify_source(j)
        folder=tempfile.TemporaryDirectory(prefix='publish-inspection-',dir=self.runtime.paths.cache_dir)
        j['media']=folder
        target=Path(folder.name)/('video'+source.suffix.lower());shutil.copyfile(source,target)
        if sha256_file(target)!=j['payload']['video_hash']:raise InspectionMediaChanged()
        j['payload']['video_path']=str(target)
        if cover:
            dest=Path(folder.name)/('cover'+cover.suffix.lower());shutil.copyfile(cover,dest)
            if sha256_file(dest)!=j['video']['cover']['sha256']:raise InspectionMediaChanged()
            j['payload']['cover_path']=str(dest)

    @staticmethod
    def _no_final():raise InspectionFinalBlocked('inspection_final_blocked')

    def _finish(self,j,state,code=None):
        # Cleanup occurs on the browser's owning worker before terminal feedback.
        adapter=j['adapter']
        cleanup_ok=True
        if adapter:
            try:adapter.close()
            except Exception:cleanup_ok=False
        if j['media']:
            try:j['media'].cleanup()
            except OSError:pass
        with self.lock:
            # Terminal feedback is independent of successful browser cleanup.
            # Keep a failed clone owned until shutdown retries it on the same
            # worker; dropping it would also strand its shared SDK lease.
            j.update(state=state,error_code=code,cleanup_failed=not cleanup_ok)
            if cleanup_ok:j.update(adapter=None,context=None)
            closure='检查会话已关闭。' if cleanup_ok else ''
            if state=='inspection_complete':
                j.update(stage='检查完成，未发表',message='上传和发布表单检查已完成，未点击发表或保存草稿。'+closure+'平台可能自动暂存草稿。',
                    report=['已核对所选账号','已完成视频上传','已核对发布资料与所选时间','未发表，未主动保存草稿'])
            elif state=='cancelled':j.update(stage='检查已取消，未发表',message='检查已取消。'+closure)
            else:j.update(stage='检查未完成，未发表',message=ERRORS.get(code,ERRORS['inspection_failed']))
            if not cleanup_ok:j['message']+=' 检查窗口未能确认关闭，请重开客户端后再检查。'

    def run(self,iid,*,resume=False):
        with self.lock:
            j=self.jobs.get(iid)
            if j is None or j['state'] in TERMINAL:return
            if resume and j['state']!='paused_auth':return
            j['state']='running';j['stage']='正在核对账号与视频'
        try:
            if j['cancel'].is_set() or self.runtime.closed.is_set():self._finish(j,'cancelled');return
            if resume:
                outcome=j['adapter'].resume(j['payload'],context=j['context'],before_final=self._no_final)
            else:
                self._stage_media(j)
                if j['cancel'].is_set() or self.runtime.closed.is_set():self._finish(j,'cancelled');return
                adapter=self._adapter(j['platform']).create_inspection_adapter(j['cancel']);j['adapter']=adapter
                if getattr(adapter,'inspection_only',False) is not True:raise InspectionFinalBlocked()
                adapter.bind_delivery(digest=digest(j['payload']),delivery_id=iid,task_id='inspection-'+iid)
                validation=adapter.validate_target(j['payload'],j['payload'],j['payload']['execution_mode'])
                if isinstance(validation,AdapterOutcome) and validation.state=='paused_auth':outcome=validation
                elif not isinstance(validation,AdapterValidation) or validation.authorized is not True or validation.observed_uid!=j['payload']['platform_user_id']:
                    outcome=AdapterOutcome('pre_submit_failed',error_code='account_mismatch')
                else:outcome=adapter.submit(j['payload'],context=None,before_final=self._no_final)
            if j['cancel'].is_set() or self.runtime.closed.is_set():self._finish(j,'cancelled')
            elif not isinstance(outcome,AdapterOutcome):self._finish(j,'failed','inspection_failed')
            elif outcome.state=='inspection_complete':self._finish(j,'inspection_complete')
            elif outcome.state=='paused_auth' and outcome.context is not None:
                with self.lock:j.update(state='paused_auth',stage='等待账号验证，未发表',message='请完成所选账号验证，检查将继续停在发表之前。',context=outcome.context,verification_deadline=time.monotonic()+600)
            else:self._finish(j,'failed',outcome.error_code if outcome.error_code in ERRORS else 'inspection_failed')
        except InspectionFinalBlocked:self._finish(j,'failed','inspection_final_blocked')
        except InspectionMediaChanged:self._finish(j,'failed','inspection_media_changed')
        except Exception as error:self._finish(j,'cancelled' if j['cancel'].is_set() else 'failed',str(error) if str(error) in ERRORS else 'inspection_failed')

    def interventions(self):
        with self.lock:
            result=[]
            for iid,j in self.jobs.items():
                if j['state'] in TERMINAL or not j['adapter']:continue
                for request in j['adapter'].interventions():
                    item={k:v for k,v in request.items() if k!='delivery_id'}
                    item.update(inspection_id=iid,platform=j['platform'],final_started=False)
                    result.append(item)
            return result

    def respond(self,p):
        with self.lock:
            j=self.jobs.get(p['inspection_id'])
            if j is None or j['state']!='paused_auth':raise InvalidRequest()
            if not any(r['id']==p['id'] and r['inspection_id']==p['inspection_id'] for r in self.interventions()):raise InvalidRequest()
            if p['action']=='cancel':return self.cancel(p['inspection_id'])
            view=p['action'] in {'visible','refresh','scroll','pointer'}
            if p['action'] not in {'visible','refresh','scroll'}:
                if p.get('consent_same_identity') is not True:raise InvalidRequest()
            result=j['adapter'].respond(p['id'],p['action'],p.get('code',''))
        if view:self.runtime.enqueue('inspection_view',(p['inspection_id'],p['id'],result['view_revision']))
        else:self.runtime.enqueue('inspection_resume',p['inspection_id'])
        return result

    def process_view(self,iid,request_id,revision):
        with self.lock:j=self.jobs.get(iid)
        if j and j['state']=='paused_auth' and j['adapter']:
            return j['adapter'].process_verification_view(iid,request_id,revision)

    def cancel(self,iid):
        with self.lock:
            j=self.jobs.get(iid)
            if j is None:raise InvalidRequest()
            if j['state'] in TERMINAL:return self.status(iid)
            j['cancel'].set();j['stage']='正在取消检查'
        self.runtime.enqueue('inspection_cancel',iid)
        return self.status(iid)

    def finish_cancel(self,iid):
        with self.lock:j=self.jobs.get(iid)
        if j and j['state'] not in TERMINAL:self._finish(j,'cancelled')

    def refresh(self):
        with self.lock:jobs=[j for j in self.jobs.values() if j['state']=='paused_auth']
        for j in jobs:
            requests=j['adapter'].interventions()
            broker_expired=any(type(r.get('expires_at')) in (int,float) and time.time()>=r['expires_at'] for r in requests)
            if broker_expired or time.monotonic()>=j['verification_deadline']:
                self._finish(j,'failed','inspection_verification_expired');continue
            refresh=getattr(j['adapter'],'refresh_interventions',None)
            if refresh:
                try:refresh()
                except Exception:pass

    def request_shutdown(self):
        with self.lock:
            for j in self.jobs.values():
                if j['state'] not in TERMINAL:j['cancel'].set()

    def close(self):
        with self.lock:jobs=list(self.jobs.values())
        for j in jobs:
            if j['state'] not in TERMINAL:self._finish(j,'cancelled')
            elif j.get('cleanup_failed') is True:self._finish(j,j['state'],j.get('error_code'))
