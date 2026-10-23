"""Sequential publication of explicitly confirmed tasks; never resumes on restart."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import threading
from collections import deque

from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow, PublishHistoryRepository
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.submission_runner import SingleItemSubmissionRunner


class ScheduledPublish:
    def __init__(self,engine,paths,transport=None):
        self.engine,self.paths,self.transport=engine,paths,transport
        self.lock=threading.Lock();self.cancel=threading.Event();self.worker=None
        self.pending=deque();self.draining=False;self.cleanup_blocked=False

    def _inspect(self,session,task_id,*,active=False,now=None,item_id=None):
        task=session.get(PublishTaskRow,task_id);center=session.get(TaskCenterRow,task_id)
        if task is None or center is None or center.kind not in {'publish','normal_publish'}:
            raise ValueError('找不到普通发布任务。')
        if task.snapshot_json!=center.snapshot_json:raise ValueError('任务记录不一致，请重新检查。')
        snapshot=json.loads(task.snapshot_json)
        items=list(session.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id)))
        if snapshot.get('kind')!='normal_publish' or not items or len(items)!=len(snapshot.get('items',[])):
            raise ValueError('没有可执行的定时视频或任务条目不完整。')
        item=next((i for i in items if i.id==item_id),None) if item_id else items[0]
        if item is None:raise ValueError('任务条目不存在')
        detail=json.loads(item.detail_json)
        if detail not in snapshot['items']:raise ValueError('任务内容已变化。')
        expected='running' if active else 'paused'
        if any(r.state!=expected for r in (task,center,item)) or snapshot.get('execution_enabled') is not active:
            raise ValueError('任务已经执行或需要核对结果，不能重复提交。')
        if detail.get('execution_mode')!='platform_schedule':raise ValueError('当前真实提交只支持平台定时，请修改为定时安排。')
        when=datetime.fromisoformat(detail['time']);current=now or datetime.now(timezone.utc)
        if when.tzinfo is None or when.second or when.microsecond or not current+timedelta(hours=2.5)<=when<=current+timedelta(days=13):
            raise ValueError('发布时间须为当前2.5小时后至13天内的整分钟。')
        if detail.get('blockers') or detail.get('required_choice') or snapshot.get('issues'):raise ValueError('任务还有未处理的检查项。')
        unsupported=[]
        if detail.get('location') and not getattr(self.transport,'supports_location',False):unsupported.append('发布地点')
        if str(detail.get('tags') or '').strip() and not getattr(self.transport,'supports_topics',False):unsupported.append('视频话题标签（当前提交器未接通）')
        if unsupported:
            raise ValueError('此视频包含'+ '、'.join(unsupported)+'，当前客户端尚未接通这些内容的真实提交；不能自动忽略。')
        videos=[v for v in snapshot['videos'] if v['id']==detail['video_id']]
        accounts=[a for a in snapshot['accounts'] if a['id']==detail['account_id']]
        if len(videos)!=1 or len(accounts)!=1:raise ValueError('任务账号或视频不一致。')
        video,account=videos[0],accounts[0]
        cover=detail.get('cover') or video.get('cover') or {}
        if cover.get('kind') not in {None,'first_frame'}:raise ValueError('自定义封面的真实提交尚待验证，不能忽略已选封面。')
        allowed_uid=getattr(self.transport,'expected_uid',None)
        if allowed_uid and account['platform_user_id']!=allowed_uid:raise ValueError('当前客户端未配置此账号的真实提交。')
        row=session.get(AccountRow,account['id'])
        if row is None or row.login_state!='valid' or not row.session_ref or row.platform!='douyin' or row.platform_user_id!=account['platform_user_id']:
            raise ValueError('账号登录状态或身份已改变，请重新核验。')
        if hashlib.sha256(row.session_ref.encode()).hexdigest()!=account['session_fingerprint']:
            raise ValueError('账号会话已改变，请重新准备任务。')
        path=Path(video['path']).resolve();root=(self.paths.data_dir/'publish_resources').resolve()
        if not path.is_relative_to(root) or not path.is_file() or sha256_file(path)!=video['content_hash']:
            raise ValueError('冻结的视频缺失或已改变。')
        from yijianjifa.webui.douyin_publish_form import validate_form_request
        form=validate_form_request(expected_uid=account['platform_user_id'],title=detail['title'],body=detail['body'],tags=detail.get('tags') or '',
            declaration=(detail.get('declaration') or {}).get('label',''),location=detail.get('location'),
            scheduled_time_text=when.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M'),now=current)
        histories=list(session.scalars(select(PublishHistoryRow).where(PublishHistoryRow.task_id==task_id,PublishHistoryRow.account_id==account['id'],PublishHistoryRow.video_hash==video['content_hash'])))
        if len(histories)!=1 or histories[0].state not in {'queued','running'} or histories[0].account_id!=account['id'] or histories[0].video_hash!=video['content_hash']:
            raise ValueError('发布历史不一致或存在未决结果。')
        PublishHistoryRepository.assert_no_other_target_history(session, video['content_hash'], account['id'],
            history_id=histories[0].id, identity=('douyin',account['platform_user_id']),
            allow_repeat_success=detail.get('repeat_confirmed') is True, legacy=True,allow_new_request=detail.get('allow_new_request') is True)
        digest=hashlib.sha256(task.snapshot_json.encode()).hexdigest()
        return task,center,item,snapshot,form,digest

    def _inspect_all(self,session,task_id):
        items=list(session.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id)))
        if not items:raise ValueError('任务没有视频')
        result=None
        for item in items:result=self._inspect(session,task_id,item_id=item.id)
        return result

    def check(self,task_id):
        if self.transport is None:return dict(available=False,reason='当前客户端未配置真实定时提交。')
        try:
            with session_scope(self.engine) as s:
                *_,digest=self._inspect_all(s,task_id)
            return dict(available=True,digest=digest,reason='将上传此任务的视频并提交平台定时。')
        except (ValueError,KeyError,TypeError) as e:return dict(available=False,reason=str(e))

    def reset_proven_unsubmitted(self,task_id,*,consent):
        """Explicit retry preparation for one stopped attempt, with durable negative evidence.

        Archive its claim/result before resetting; never erase a final marker.
        This does not upload or start a worker. The normal check/start still apply.
        """
        if consent is not True:raise ValueError('需要明确同意重试未提交任务。')
        from yijianjifa.storage.models import Setting
        from yijianjifa.features.publish.submission_journal import SubmissionJournal
        import uuid
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('提交窗口尚未结束。')
            with self.engine.connect() as c:
                c.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    with session_scope(c) as s:
                        task=s.get(PublishTaskRow,task_id);center=s.get(TaskCenterRow,task_id)
                        rows=list(s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id)))
                        histories=list(s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.task_id==task_id)))
                        if task is None or center is None or len(rows)!=1 or len(histories)!=1:raise ValueError('仅支持已核实未提交的单条任务。')
                        item=rows[0];history=histories[0]
                        if any(r.state!='pre_submit_failed' for r in (task,center,item,history)):raise ValueError('任务并非已核实的提交前失败。')
                        snapshot=json.loads(task.snapshot_json);detail=json.loads(item.detail_json)
                        videos=[v for v in snapshot.get('videos',[]) if v['id']==detail.get('video_id')]
                        if task.snapshot_json!=center.snapshot_json or snapshot.get('kind')!='normal_publish' or snapshot.get('items')!=[detail] or len(videos)!=1 or history.account_id!=detail.get('account_id') or history.video_hash!=videos[0]['content_hash']:raise ValueError('任务快照不一致。')
                        key=SubmissionJournal._key(json.dumps([task_id,item.id]))
                        if s.get(Setting,key) is not None:raise ValueError('存在最终提交记录，必须核对后台，不能重试。')
                        claim=s.get(Setting,key.replace('publish.final.','publish.claim.',1));result=s.get(Setting,key.replace('publish.final.','publish.result.',1))
                        if claim is None or result is None:raise ValueError('没有完整的未提交证据。')
                        claimed=json.loads(claim.value_json);saved=json.loads(result.value_json)
                        digest=hashlib.sha256(json.dumps([task.snapshot_json,item.detail_json],ensure_ascii=False).encode()).hexdigest()
                        if saved.get('state')!='pre_submit_failed' or saved.get('evidence',{}).get('final_action_triggered') is not False or saved.get('request_digest')!=digest or claimed.get('request_digest')!=digest:raise ValueError('未提交证据与任务不符。')
                        s.add(Setting(key='publish.retry.'+str(uuid.uuid4()),value_json=json.dumps(dict(task_id=task_id,item_id=item.id,claim=claimed,result=saved))))
                        s.delete(claim);s.delete(result)
                        snapshot.update(execution_enabled=False,hold_reason='explicit_retry_pre_submit_failed')
                        task.snapshot_json=center.snapshot_json=json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
                        task.state=center.state=item.state='paused';history.state='queued'
                    c.commit()
                except BaseException:c.rollback();raise
        return self.check(task_id)

    def start(self,task_id,digest,*,consent):
        allowed=getattr(self,'can_start',None)
        if allowed and not allowed():raise ValueError('当前不能开始发布，请检查任务和运行状态。')
        if consent is not True or self.transport is None:raise ValueError('需要明确点击提交定时发布。')
        with self.lock:
            if self.cancel.is_set():raise ValueError('客户端正在关闭。')
            if self.cleanup_blocked:raise ValueError('前一发布窗口未能关闭，请退出软件后重新打开。')
            with self.engine.connect() as c:
                c.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    with session_scope(c) as s:
                        task,center,item,snapshot,_,actual=self._inspect_all(s,task_id)
                        if actual!=digest:raise ValueError('安排已变化，请重新检查。')
                        snapshot.update(execution_enabled=True,hold_reason=None)
                        text=json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
                        task.snapshot_json=center.snapshot_json=text
                        center.kind='normal_publish';center.name='普通发布 · 平台定时'
                        task.state=center.state='running'
                        for row in s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id)):row.state='running'
                        item_id=item.id
                    c.commit()
                except BaseException:c.rollback();raise
            self.pending.append(task_id)
            if not self.draining:
                self.draining=True
                self.worker=threading.Thread(target=self._drain,name='p011-scheduled-publish',daemon=False)
                self.worker.start()
        return dict(id=task_id,state='running',submitted=False)

    def _pause_pending(self,reason='publish_window_cleanup_required'):
        with self.lock:
            ids=list(self.pending);self.pending.clear()
        with session_scope(self.engine) as s:
            for task_id in ids:
                task=s.get(PublishTaskRow,task_id);center=s.get(TaskCenterRow,task_id)
                if task is None or center is None or task.state!='running' or center.state!='running':continue
                snapshot=json.loads(task.snapshot_json)
                snapshot.update(execution_enabled=False,hold_reason=reason)
                task.snapshot_json=center.snapshot_json=json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
                task.state=center.state='paused'
                for item in s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id)):item.state='paused'

    def _finish_sessions(self,task_id):
        if not callable(getattr(self.transport,'release_sessions',None)):return
        try:self.transport.flush_sessions()
        except Exception:
            self.cleanup_blocked=True
            from yijianjifa.features.notifications.service import NotificationService
            NotificationService(self.engine).emit_action_required(task_id,'发布窗口或登录资料清理未完成，请退出软件后重新打开。')

    def _drain(self):
        task_id=None
        while True:
            if self.cancel.is_set():
                self._pause_pending('client_closing')
                with self.lock:
                    self._finish_sessions(task_id)
                    self.draining=False
                return
            with self.lock:
                if not self.pending:
                    self._finish_sessions(task_id)
                    self.draining=False
                    return
                task_id=self.pending.popleft()
            from yijianjifa.webui.publish_progress import progress
            try:
                with session_scope(self.engine) as s:
                    total=len(json.loads(s.get(PublishTaskRow,task_id).snapshot_json)['items'])
                progress.start(task_id,total)
                clean=self._run(task_id)
            except Exception:clean=False
            if not clean:
                with self.lock:self.cleanup_blocked=True
                self._pause_pending()
                with self.lock:self.draining=False
                return

    def _run(self,task_id):
        clean=True
        try:self._run_batch(task_id)
        finally:
            from yijianjifa.webui.publish_progress import progress
            with session_scope(self.engine) as session:
                center=session.get(TaskCenterRow,task_id)
                progress.finish(task_id,center.state if center else 'failed')
            flush=getattr(self.transport,'release_sessions',None) or getattr(self.transport,'flush_sessions',None)
            if flush:
                try:flush()
                except Exception:
                    clean=False
                    from yijianjifa.features.notifications.service import NotificationService
                    NotificationService(self.engine).emit_action_required(task_id,'前一发布窗口未能完成清理，后续任务已暂停。请退出软件后重新打开，再检查任务记录。')

        return clean

    def _run_batch(self,task_id):
        with session_scope(self.engine) as s:
            rows=list(s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id)))
            item_ids=[r.id for r in sorted(rows,key=lambda r:json.loads(r.detail_json).get('time',''))]
        for position,item_id in enumerate(item_ids,1):
            from yijianjifa.webui.publish_progress import progress
            progress.update(task_id,stage='正在准备本条视频',index=position)
            if self.cancel.is_set():break
            allowed=getattr(self,'can_start',None)
            if allowed and not allowed():
                from yijianjifa.features.notifications.service import NotificationService
                NotificationService(self.engine).emit_action_required(task_id,'当前不能继续发布，剩余视频已暂停且尚未提交。请检查任务和运行状态。')
                break
            state=self._run_item(task_id,item_id)
            if state!='platform_scheduled':break
        with session_scope(self.engine) as s:
            pending=list(s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id,TaskItemRow.state=='running')))
            if pending:
                for item in pending:item.state='paused'
                for model in (PublishTaskRow,TaskCenterRow):
                    row=s.get(model,task_id)
                    if row.state=='running':row.state='paused'

    def _run_item(self,task_id,item_id):
        def submit(snapshot,detail,*,before_final):
            with session_scope(self.engine) as s:
                _,_,_,_,form,_=self._inspect(s,task_id,active=True,item_id=item_id)
            video=next(v for v in snapshot['videos'] if v['id']==detail['video_id'])
            account=next(a for a in snapshot['accounts'] if a['id']==detail['account_id'])
            def final():
                if self.cancel.is_set():raise ValueError('execution cancelled')
                allowed=getattr(self,'can_start',None)
                if allowed and not allowed():raise ValueError('当前不能开始发布，请检查任务和运行状态。')
                with session_scope(self.engine) as s:self._inspect(s,task_id,active=True,item_id=item_id)
                before_final()
            form={**form,'_task_id':task_id}
            return self.transport(account,video,form,detail['time'],before_final=final,cancel=self.cancel)
        try:return SingleItemSubmissionRunner(self.engine).run(task_id,item_id,submit)
        except Exception:
            # Preserve any final marker/claim. Never automatically retry after a
            # worker or database failure; expose a durable action-needed notice.
            from yijianjifa.features.notifications.service import NotificationService
            NotificationService(self.engine).emit_action_required(task_id,'提交执行中断，请核对后台结果；不会自动重发。')

    def close(self,timeout=0):
        self.cancel.set()
        if self.worker:self.worker.join(timeout)
        return self.worker is None or not self.worker.is_alive()
