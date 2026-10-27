"""Idempotent local handoff from settled mix receipts to paused publish tasks."""
from datetime import datetime,timezone
import hashlib
import json
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from .publish_preparation import PublishPreparation
from .publish_confirmation import PublishConfirmation


def read_source(engine,task_id):
    with session_scope(engine) as session:
        task=session.get(MixTask,task_id)
        if task is None:raise ValueError('制作任务不存在。')
        snapshot=json.loads(task.snapshot_json)
        results={}
        for index in range(task.quantity):
            success=session.get(Setting,f'mix-success:{task_id}:{index}')
            receipt=session.get(Setting,f'mix-result:{task_id}:{index}')
            if success and receipt and json.loads(receipt.value_json)['state']=='completed':
                results[index]=json.loads(success.value_json)['finished_id']
        return task.state,snapshot.get('publish_after'),results


class HandoffPreparation(PublishPreparation):
    def __init__(self,engine,paths,task_id,indices,token):
        super().__init__(engine,paths,inherit_account_settings=False)
        self.task_id,self.indices,self.token=task_id,indices,token

    def _token(self):return self.token

    def _preview(self,params,now):
        state,plan,results=read_source(self.engine,self.task_id)
        if not plan or state in {'paused','interrupted','cancelled','cancelling'}:
            raise ValueError('制作任务已暂停或取消，等待处理。')
        if plan['linkage']=='whole_batch' and state not in {'completed','completed_with_errors'}:
            raise ValueError('本批制作尚未结束。')
        if [results.get(i) for i in self.indices]!=params['video_ids']:
            raise ValueError('制作成品回执已变化。')
        owner=plan['account'];account=AccountService(self.engine).get(owner['id'])
        if (account.platform_user_id!=owner['platform_user_id'] or
                hashlib.sha256((account.session_ref or '').encode()).hexdigest()!=owner['session_fingerprint']):
            raise ValueError('账号身份或会话已变化，请重新确认。')
        # Minimal mix publishing has no declaration/location editor. These paused
        # tasks must still undergo live policy checks before any future execution.
        return super()._preview(params,now)

    def _snapshot_fields(self,preview):
        return dict(kind='normal_publish',source_mix=dict(task_id=self.task_id,
            indices=self.indices,policy_recheck_required=True))


class HandoffConfirmation(PublishConfirmation):
    task_name='制作后发布 · 本地待执行'

    def _capture_locked(self,connection,params,now):
        p=self.preparation
        return HandoffPreparation(connection,self.paths,p.task_id,p.indices,p.token)._capture(params,now)


class MixPublishHandoff:
    def __init__(self,engine,paths):self.engine,self.paths=engine,paths

    def reconcile(self,task_id,*,now=None):
        now=now or datetime.now(timezone.utc)
        state,plan,results=read_source(self.engine,task_id)
        response={'tasks':[],'blocked':[]}
        if not plan:return response
        groups=([[i] for i in sorted(results)] if plan['linkage']=='per_item'
                else [sorted(results)] if results and state in {'completed','completed_with_errors'} else [])
        for indices in groups:
            identity=f"p011-mix-publish:{task_id}:"+('batch' if plan['linkage']=='whole_batch' else str(indices[0]))
            token=hashlib.sha256(identity.encode()).hexdigest()[:48]
            key=PublishConfirmation.receipt_prefix+hashlib.sha256(token.encode()).hexdigest()
            previous=PublishConfirmation._receipt(self.engine,key)
            if previous:
                response['tasks'].append(previous);continue
            if state in {'paused','interrupted','cancelled','cancelling'}:continue
            aid=plan['account']['id']
            params=dict(account_ids=[aid],video_ids=[results[i] for i in indices],distribution='split_across_accounts',
                mode='exact',start=plan['slots'][indices[0]]['time'],interval_hours=3,daily_count=1,days=1,
                item_schedules={aid+':'+results[i]:{'mode':'exact','start':plan['slots'][i]['time']} for i in indices})
            preparation=HandoffPreparation(self.engine,self.paths,task_id,indices,token)
            try:
                ready=preparation.prepare(params,now=now)
                if not ready['token']:raise ValueError('发布前检查未通过，请处理账号、时间或成品状态。')
                response['tasks'].append(HandoffConfirmation(self.engine,self.paths,preparation).confirm(token,now=now))
            except Exception:
                # No raw paths, account identifiers or provider errors escape this status.
                previous=PublishConfirmation._receipt(self.engine,key)
                if previous:response['tasks'].append(previous)
                else:response['blocked'].append({'indices':indices,'code':'post_mix_review_required'})
            finally:preparation.close()
        return response
