"""Hide local task records without deleting output or submission evidence."""
import json
import hashlib
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.features.publish.history import PublishHistoryRow

TERMINAL={'completed','completed_with_errors','cancelled','published','platform_scheduled','failed'}

def reviewed_removed(session,pid):
    from .publish_review import PublishReview
    _,entries,eligible,_,task=PublishReview(None)._read(session,pid)
    return bool(eligible and entries and entries[-1].get('outcome')=='removed'
                and entries[-1].get('original_state')==task.state)

def remove_task(connection,pid):
    with session_scope(connection) as s:
        if s.get(Setting,'task-hidden:'+pid):return {'id':pid,'deleted':True}
        mix=s.get(MixTask,pid);center=s.get(TaskCenterRow,pid)
        if mix:
            if mix.state in {'paused','interrupted','action_required'}:
                mix.state='cancelled'
                progress=s.get(Setting,'mix.daily.status:'+pid)
                if progress:
                    progress.value_json=json.dumps({**json.loads(progress.value_json),'automatic_stopped':True,'auth_phase':''})
            if mix.state not in TERMINAL:raise ValueError('请先取消或完成制作任务，再删除记录。')
        elif center and center.kind=='download':
            if center.state in {'waiting','running','cancelling'}:
                raise ValueError('下载仍在等待或处理中，请先取消任务再删除记录。')
            s.add(Setting(key='task-hidden:'+pid,value_json='true'))
            return {'id':pid,'deleted':True,'retained_outputs':True}
        elif center:
            histories=list(s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.task_id==pid)))
            items=list(s.scalars(select(TaskItemRow).where(TaskItemRow.task_id==pid)))
            states={center.state}|{h.state for h in histories}|{i.state for i in items}
            if states & {'running','publishing','paused_auth','cancelling'}:
                raise ValueError('任务正在执行，请先结束当前操作再删除。')
            if 'result_unknown' in states:
                # Hide the record only. Retain all reservations, histories and final
                # markers so deletion can never turn uncertainty into a retry.
                s.add(Setting(key='task-hidden:'+pid,value_json='true'))
                return {'id':pid,'deleted':True,'retained_unresolved':True}
            snapshot=json.loads(center.snapshot_json)
            if center.state=='pre_submit_failed':
                cancel_failed_batch(s,pid,center,items,histories,snapshot)
                states={'cancelled'}
            if states & {'paused','waiting','queued','missed_waiting_user'}:
                if snapshot.get('execution_enabled') is not False:raise ValueError('请先核对已启动任务，不能删除。')
                from .publish_control import cancel_local
                cancel_local(connection,pid)
                s.expire_all()
                if s.get(TaskCenterRow,pid).state!='cancelled':raise ValueError('部分条目尚不能安全取消，任务未删除。')
        else:raise KeyError(pid)
        s.add(Setting(key='task-hidden:'+pid,value_json='true'))
    return {'id':pid,'deleted':True}


def cancel_failed_batch(session,pid,center,items,histories,snapshot):
    """Cancel proven-unsubmitted remainder, keeping successful submission evidence."""
    from yijianjifa.features.publish.normal_service import PublishTaskRow
    from yijianjifa.features.publish.submission_journal import SubmissionJournal
    allowed={'pre_submit_failed','paused','waiting','queued','cancelled'}
    confirmed={'platform_scheduled','published','completed'}
    if center.state!='pre_submit_failed' or any(row.state not in allowed|confirmed for row in [*items,*histories]):
        raise ValueError('任务仍有已提交或待核对的条目，不能释放视频。')
    videos={v['id']:v['content_hash'] for v in snapshot['videos']}
    cancellations=[];cancelled_history=set()
    for item in items:
        detail=json.loads(item.detail_json)
        related=[h for h in histories if h.account_id==detail['account_id'] and h.video_hash==videos[detail['video_id']]]
        if not related:raise ValueError('发布历史不完整，不能释放视频。')
        if item.state in confirmed:
            if any(h.state not in confirmed for h in related):raise ValueError('已提交条目的历史不一致，请先核对结果。')
            continue
        if any(h.state not in allowed for h in related):raise ValueError('此视频仍有已提交或待核对的记录，不能释放。')
        cancellations.append(item);cancelled_history.update(h.id for h in related)
        key=SubmissionJournal._key(json.dumps([pid,item.id]))
        final=session.get(Setting,key)
        claim=session.get(Setting,key.replace('publish.final.','publish.claim.',1))
        result=session.get(Setting,key.replace('publish.final.','publish.result.',1))
        if final is not None:raise ValueError('存在提交记录，请先核对结果。')
        if item.state=='pre_submit_failed' or claim is not None or result is not None:
            saved=json.loads(result.value_json) if result else {}
            claimed=json.loads(claim.value_json) if claim else {}
            digest=hashlib.sha256(json.dumps([center.snapshot_json,item.detail_json],ensure_ascii=False).encode()).hexdigest()
            if (saved.get('state')!='pre_submit_failed' or
                saved.get('evidence',{}).get('final_action_triggered') is not False or
                saved.get('request_digest')!=digest or claimed.get('request_digest')!=digest):
                raise ValueError('未提交证据不完整，请先核对结果。')
    task=session.get(PublishTaskRow,pid)
    if task is None or task.snapshot_json!=center.snapshot_json:raise ValueError('任务记录不一致。')
    # Preserve immutable snapshots and submission journals as audit evidence.
    for row in cancellations:row.state='cancelled'
    for row in histories:
        if row.id in cancelled_history:row.state='cancelled'
    remaining={row.state for row in items}|{row.state for row in histories}
    state='platform_scheduled' if 'platform_scheduled' in remaining else 'completed' if remaining & confirmed else 'cancelled'
    task.state=center.state=state
