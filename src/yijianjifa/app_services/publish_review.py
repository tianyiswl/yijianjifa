"""Append-only human observations; never rewrite submission evidence; new arrangements need separate consent."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select, text
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow

LABELS={'scheduled':'人工核对：平台已接收定时','published':'人工核对：已公开发布',
        'removed':'人工核对：已撤销或删除','not_found':'人工核对：暂未找到，仍待确认'}

class PublishReview:
    def __init__(self,engine):self.engine=engine

    def _read(self,s,pid):
        task=s.get(TaskCenterRow,pid)
        if task is None:raise ValueError('任务不存在')
        items=list(s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==pid).order_by(TaskItemRow.id)))
        row=s.get(Setting,'publish-review:'+pid)
        entries=json.loads(row.value_json) if row else []
        eligible=task.kind in {'publish','normal_publish'} and len(items)==1 and task.state in {'result_unknown','platform_scheduled','published'} and items[0].state==task.state
        raw=json.dumps([task.snapshot_json,task.state,[(i.id,i.state,i.detail_json) for i in items],entries],ensure_ascii=False,sort_keys=True)
        return row,entries,eligible,hashlib.sha256(raw.encode()).hexdigest(),task

    def read(self,pid):
        with session_scope(self.engine) as s:
            _,entries,eligible,digest,_=self._read(s,pid)
            history = self._history(s,pid)
            permission = s.get(Setting,'publish-rearrange:'+history.id) if history else None
            grant = json.loads(permission.value_json) if permission else None
            return {'available':eligible,'digest':digest,'entries':entries,
                    'rearrange_available':bool(eligible and history and entries and entries[-1]['outcome']=='removed'
                                              and (not grant or grant.get('review_digest')!=digest)),
                    'rearrange':grant,
                    'rearrange_active':bool(grant and not grant.get('consumed_by') and grant.get('review_digest')==digest and eligible and entries and entries[-1]['outcome']=='removed')}

    def save(self,pid,digest,outcome,note,consent,request_id):
        if type(outcome) is not str or outcome not in LABELS or type(note) is not str or not 1<=len(note.strip())<=1000 or consent is not True:
            raise ValueError('请选择核对结果、填写依据并确认已核对后台')
        if any(type(v) is not str or not 1<=len(v)<=128 for v in [pid,digest,request_id]):raise ValueError('无效核对请求')
        with session_scope(self.engine) as s:
            s.execute(text('BEGIN IMMEDIATE'))
            row,entries,eligible,current,task=self._read(s,pid)
            for entry in entries:
                if entry['request_id']==request_id:
                    if entry['outcome']!=outcome or entry['note']!=note.strip():raise ValueError('核对请求已被使用')
                    return entry
            if not eligible or digest!=current:raise ValueError('任务或核对记录已变化，请刷新后重新核对')
            entry={'request_id':request_id,'outcome':outcome,'label':LABELS[outcome],'note':note.strip(),
                   'recorded_at':datetime.now(timezone.utc).isoformat(),'source':'user_observation','original_state':task.state}
            entries.append(entry)
            if row:row.value_json=json.dumps(entries,ensure_ascii=False)
            else:s.add(Setting(key='publish-review:'+pid,value_json=json.dumps(entries,ensure_ascii=False)))
            return entry

    @staticmethod
    def _history(s,pid):
        from yijianjifa.features.publish.history import PublishHistoryRow
        rows=list(s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.task_id==pid)))
        task=s.get(TaskCenterRow,pid)
        if len(rows)!=1 or task is None or rows[0].state!=task.state:return None
        snapshot=json.loads(task.snapshot_json)
        items=snapshot.get('items',[])
        if len(items)!=1 or items[0].get('account_id')!=rows[0].account_id:return None
        videos=[v for v in snapshot.get('videos',[]) if v.get('id')==items[0].get('video_id')]
        if len(videos)!=1 or videos[0].get('content_hash')!=rows[0].video_hash:return None
        return rows[0]

    def allow_rearrange(self,pid,digest,consent,request_id):
        if consent is not True or any(type(v) is not str or not 1<=len(v)<=128 for v in [pid,digest,request_id]):
            raise ValueError('请明确确认允许重新安排')
        with session_scope(self.engine) as s:
            s.execute(text('BEGIN IMMEDIATE'))
            _,entries,eligible,current,_=self._read(s,pid)
            history=self._history(s,pid)
            if history is None:raise ValueError('发布历史不完整，不能允许重新安排')
            key='publish-rearrange:'+history.id
            saved=s.get(Setting,key)
            previous=json.loads(saved.value_json) if saved else None
            if previous and previous['request_id']==request_id:
                if previous['review_digest']!=digest:raise ValueError('确认请求已被使用')
                return previous
            if not eligible or current!=digest or not entries or entries[-1]['outcome']!='removed':
                raise ValueError('请先核实并记录已撤销或删除，再刷新任务详情')
            if previous and previous['review_digest']==digest:raise ValueError('此核对记录已经确认过重新安排，请勿重复操作')
            grant={'request_id':request_id,'review_digest':digest,'review_request_id':entries[-1]['request_id'],
                   'history_state':history.state,'account_id':history.account_id,'video_hash':history.video_hash,
                   'allowed_at':datetime.now(timezone.utc).isoformat(),'consumed_by':None,
                   'prior_permissions':([previous] if previous else [])}
            if saved:saved.value_json=json.dumps(grant,ensure_ascii=False)
            else:s.add(Setting(key=key,value_json=json.dumps(grant,ensure_ascii=False)))
            return grant
