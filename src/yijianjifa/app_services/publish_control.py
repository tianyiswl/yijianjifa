"""Control locally held tasks without calling or altering platform submissions."""
import json
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.commerce.service import CommerceTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow


def cancellable_items(rows, history, snapshot):
    videos={v['id']:v['content_hash'] for v in snapshot['videos']}
    for row in rows:
        if row.state not in {'paused','waiting','queued','missed_waiting_user'}:continue
        item=json.loads(row.detail_json)
        related=[h for h in history if h.account_id==item['account_id'] and h.video_hash==videos[item['video_id']]]
        # Unknown or conflicting history must never be treated as unsubmitted.
        if related and all(h.state in {'queued','cancelled'} for h in related):
            yield row,item,related


def cancel_local(connection, task_id, account_id=None):
    with session_scope(connection) as s:
        task=s.get(PublishTaskRow,task_id)
        expected_kind='normal_publish'
        if task is None:
            task=s.get(CommerceTaskRow,task_id)
            expected_kind='commerce_publish'
        center=s.get(TaskCenterRow,task_id)
        if task is None or center is None:raise KeyError(task_id)
        snapshot=json.loads(task.snapshot_json)
        if snapshot.get('kind')!=expected_kind or snapshot.get('execution_enabled') is not False:
            raise ValueError('not a locally held publish task')
        rows=list(s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id)))
        history=list(s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.task_id==task_id)))
        if account_id is not None:
            if expected_kind!='normal_publish' or account_id not in {i['account_id'] for i in snapshot['items']}:
                raise ValueError('account is not part of this ordinary publish task')
        cancelled=0
        for row,item,related in cancellable_items(rows,history,snapshot):
            if account_id is not None and item['account_id']!=account_id:continue
            for h in related:
                if h.state=='queued':h.state='cancelled'
            row.state='cancelled';cancelled+=1
        states={r.state for r in rows}|{h.state for h in history}
        if states<= {'cancelled'}:state='cancelled'
        elif 'result_unknown' in states:state='result_unknown'
        elif states & {'running','publishing'}:state='running'
        elif 'missed_waiting_user' in states:state='missed_waiting_user'
        elif 'platform_scheduled' in states:state='platform_scheduled'
        elif states<= {'published','completed','cancelled'}:state='completed'
        else:state='paused'
        task.state=center.state=state
        return {'id':task_id,'state':state,'cancelled':cancelled}


def recover_local_software(engine, *, now=None):
    """Mark expired held software schedules for user action; never submit or retry."""
    from datetime import datetime, timezone
    from yijianjifa.features.publish.software_scheduler import SoftwareScheduler
    now=now or datetime.now(timezone.utc)
    SoftwareScheduler._require_aware(now)
    changed=[]
    with engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            with session_scope(connection) as s:
                tasks=[]
                for model,expected_kind in ((PublishTaskRow,'normal_publish'),(CommerceTaskRow,'commerce_publish')):
                    tasks.extend((row,expected_kind) for row in s.scalars(select(model).where(model.state.in_(['paused','waiting','missed_waiting_user']))))
                for task,expected_kind in tasks:
                    snapshot=json.loads(task.snapshot_json)
                    center=s.get(TaskCenterRow,task.id)
                    if center is None or snapshot.get('kind')!=expected_kind or snapshot.get('execution_enabled') is not False:continue
                    if center.state not in {'paused','waiting','missed_waiting_user'}:continue
                    histories=list(s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.task_id==task.id)))
                    # Keep evidence of potentially submitted work untouched.
                    if any(h.state not in {'queued','cancelled'} for h in histories):continue
                    videos={v['id']:v['content_hash'] for v in snapshot.get('videos',[])}
                    touched=False
                    for row in s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task.id)):
                        if row.state not in {'paused','waiting','queued'}:continue
                        item=json.loads(row.detail_json)
                        if item.get('execution_mode')!='software_schedule':continue
                        try:
                            when=datetime.fromisoformat(item['time'])
                            SoftwareScheduler._require_aware(when)
                        except (KeyError,TypeError,ValueError):continue
                        related=[h for h in histories if h.account_id==item.get('account_id') and h.video_hash==videos.get(item.get('video_id'))]
                        if when<now and related and all(h.state=='queued' for h in related):
                            row.state='missed_waiting_user';touched=True
                    if touched:
                        task.state=center.state='missed_waiting_user';changed.append(task.id)
                        import uuid
                        from yijianjifa.features.notifications.service import NotificationRow
                        notice_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'p011:software-missed:'+task.id))
                        if s.get(NotificationRow,notice_id) is None:
                            s.add(NotificationRow(id=notice_id,task_id=task.id,status='action_required',
                                message='此批次有软件定时已错过，等待处理；未自动补发。',deep_link='task:'+task.id))
            connection.commit()
        except BaseException:
            connection.rollback();raise
    return changed
