"""只读发布报告：成品文件名供补发，取消/隐藏本地任务不抹去平台回执。"""
import json
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import select
from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.features.mixing.serial_gate import success,valid_snapshot,value,ITEM,SOURCE,object_json,verified_failure,same_time
from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow,PublishBatchReceiptRow,digest
from yijianjifa.features.publish.outcomes import COMPLETED_DELIVERY_STATES
from yijianjifa.features.tasks.service import TaskItemRow


def _report_video(session,task,snapshot,index):
    video=success(session,task,index)
    if video is not None:return video,False
    # Read-only historical projection. A deliberate finished-file deletion must
    # not turn real platform receipts into "never generated" or authorize work.
    from yijianjifa.features.finished.service import FinishedVideo
    from yijianjifa.features.mixing.output_names import output_filename
    receipt=value(session,f'mix-success:{task.id}:{index}')
    if type(receipt) is not dict:return None,False
    deletion=value(session,'finished-delete:'+str(receipt.get('finished_id')))
    result=value(session,f'mix-result:{task.id}:{index}')
    if not all(type(record) is dict for record in (receipt,deletion,result)):return None,False
    expected=str(Path(snapshot['output_directory'])/task.id/output_filename(snapshot,index))
    if (result.get('state')=='completed' and deletion.get('state') in {'committed','completed'}
            and deletion.get('owned') is True and deletion.get('id')==receipt.get('finished_id')
            and receipt.get('path')==deletion.get('original')==expected
            and type(deletion.get('content_hash')) is str and len(deletion['content_hash'])==64
            and session.get(FinishedVideo,receipt['finished_id']) is None):
        return SimpleNamespace(id=receipt['finished_id'],content_hash=deletion['content_hash'],
            display_name=deletion.get('display_name') or Path(expected).name),True
    return None,False


def build_report(session,task,serial):
    from .mix_execution import _text,_publish_reason
    snapshot=json.loads(task.snapshot_json)
    if not valid_snapshot(snapshot,task.quantity):return None
    ready=serial['stage'] in {'completed','paused'} or task.state in {'completed','completed_with_errors','cancelled','paused','interrupted'}
    if not ready:return {'ready':False}
    missed=[];accepted=0;unmade=0;removed=0
    frozen=snapshot['daily_publish'];expected={(t['account_id'],t['platform'],t['platform_user_id']) for t in frozen['targets']}
    for index in range(task.quantity):
        video,deleted=_report_video(session,task,snapshot,index)
        if video is None:unmade+=1;continue
        removed+=int(deleted)
        binding=value(session,ITEM+task.id+':'+str(index));pid=binding.get('child_id')
        source=value(session,SOURCE+str(pid));receipt=session.get(PublishBatchReceiptRow,f'daily-{task.id}-i{index}')
        scope=(binding.get('index')==index and binding.get('finished_id')==video.id and binding.get('content_hash')==video.content_hash
            and binding.get('snapshot_digest')==digest(snapshot) and source.get('mix_task_id')==task.id and source.get('item_index')==index
            and source.get('snapshot_digest')==digest(snapshot) and source.get('finished_id')==video.id
            and source.get('content_hash')==video.content_hash and receipt is not None and receipt.task_id==pid)
        rows=list(session.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==pid))) if scope else []
        rows=[r for r in rows if (r.account_id,r.platform,r.platform_user_id) in expected and r.video_id==video.id
            and r.video_hash==video.content_hash and r.execution_mode==frozen['mode'] and same_time(r.scheduled_at,frozen['slots'][index])]
        observed={r.account_id:r for r in rows}
        for target in frozen['targets']:
            row=observed.get(target['account_id']);attempt=None;evidence={};state='unknown';bound=False;binding_ok=False
            if row:
                attempt=session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id,DeliveryAttemptRow.number==row.attempt))
                saved=session.get(TaskItemRow,row.item_id);state=saved.state if saved else 'unknown'
                evidence=object_json(attempt.result_json) if attempt else {}
                binding_ok=bool(attempt and attempt.request_digest==row.request_digest and digest(object_json(row.snapshot_json))==row.request_digest
                    and digest(object_json(attempt.snapshot_json))==row.request_digest)
                bound=binding_ok and not evidence.get('protocol_violation') and evidence.get('request_digest')==row.request_digest
            final=bool(attempt and object_json(attempt.final_json).get('request_digest')==row.request_digest)
            if bound and final and evidence.get('state') in COMPLETED_DELIVERY_STATES:accepted+=1;continue
            failed=bool(row and bound and verified_failure(row,attempt,evidence.get('state')))
            unknown=not binding_ok or bool(evidence.get('protocol_violation')) or bool(attempt and attempt.result_json and not bound) or (bool(attempt and attempt.final_json) and not failed)
            pause=value(session,'multi_publish.target_pause:'+row.id) if row else {}
            deferred=bool(row and pause.get('daily_parent')==task.id and pause.get('request_digest')==row.request_digest)
            label='结果待核对' if unknown else '账号暂缓，尚未提交' if deferred else '平台已拒绝' if evidence.get('state')=='platform_rejected' else '尚未提交'
            account=session.get(AccountRow,target['account_id']);name=(account.remark or account.nickname) if account else '原账号'
            account_label={'douyin':'抖音','bilibili':'B站','channels':'视频号','kuaishou':'快手'}.get(target['platform'],'平台')+' · '+_text(name)
            reason=_publish_reason(evidence.get('state') or state,attempt) if attempt else '原发布关联记录待核实，请先核对平台后台。'
            if unknown:reason='已经尝试投稿或记录尚未核实；补发前请先核对平台后台，避免重复发布。'
            elif evidence.get('error_category')=='cover_unconfirmed':reason='封面保存尚未确认，本条尚未提交。'
            elif evidence.get('error_code') in {'douyin_daily_post_limit','platform_daily_post_limit','posting_limit_skipped'}:reason='今日投稿次数已达上限，本条未发布。'
            elif deferred:reason='该账号的原任务需要处理，本条尚未投稿。'
            if deleted:reason+=' 此成品已从成品库删除，无法直接补发。'
            missed.append({'video_number':index+1,'filename':_text(video.display_name),
                'title':_text(json.loads(row.snapshot_json).get('fields',{}).get('title','')) if row else '',
                'account_label':account_label,'platform':target['platform'],'state':'unknown' if unknown else 'rejected' if evidence.get('state')=='platform_rejected' else 'not_submitted',
                'result':label,'reason':reason})
    total=task.quantity*len(frozen['targets'])
    lines=['一剪即发 · 发布报告',f'视频已制作：{task.success_count}/{task.quantity} 条',f'平台已接收：{accepted} 项',f'已制作部分待处理：{len(missed)} 项']
    if unmade:lines.append(f'尚未生成视频：{unmade} 条')
    if removed:lines.append(f'已从成品库删除：{removed} 条；发布历史和平台回执仍保留。')
    lines.extend(['','文件名与成品库一致。未提交项可按文件名查找；结果待核对项请先核对平台后台，避免重复投稿。'])
    if not missed:lines.append('已制作的视频没有记录到漏发项。' if unmade else '本批全部发布目标均已由平台接收。')
    for issue in missed:
        lines.extend(['',f"第{issue['video_number']}个视频：{issue['filename']}",f"{issue['account_label']}｜{issue['result']}",issue['reason']])
        if issue['title']:lines.append('投稿标题：'+issue['title'])
    return {'ready':True,'accepted':accepted,'total':total,'unmade':unmade,'removed':removed,'missed':missed,'text':'\n'.join(lines)}
