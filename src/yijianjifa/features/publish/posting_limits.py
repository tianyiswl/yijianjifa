"""An explicit daily limit skips only the same account in the same batch."""
import json
import re
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting

LIMIT_CODES = {'douyin_daily_post_limit', 'platform_daily_post_limit'}
SKIP_CODE = 'posting_limit_skipped'
SKIP_REASON = '该账号今日投稿次数已达上限，本条未提交，已跳过。'


def today():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def explicit_daily_limit(message):
    if type(message) is str and re.search(r'未达到|未达上限|没有达到|还剩|剩余|尚未',message):return False
    return (type(message) is str and bool(re.search(r'今日|今天|每日|每天', message))
        and bool(re.search(r'发布|投稿|发表|提交', message))
        and bool(re.search(r'(?:达到|已达|超过|超出).{0,8}上限|(?:次数|数量|额度).{0,8}(?:用完|用尽|已满)', message)))


def is_limited_result(evidence, state):
    return ((state=='platform_rejected' and type(evidence.get('error_code')) is str and evidence.get('error_code') in LIMIT_CODES)
        or (state=='pre_submit_failed' and evidence.get('error_code')==SKIP_CODE)) and evidence.get('state')==state


def _data(raw):
    try:value=json.loads(raw) if raw else {}
    except (ValueError,TypeError):return {}
    return value if type(value) is dict else {}


def verified_limit(row, attempt, state):
    from .deliveries import digest
    if attempt is None or not attempt.result_json:return False
    evidence=_data(attempt.result_json)
    if (not is_limited_result(evidence,state) or evidence.get('protocol_violation')
            or attempt.request_digest!=row.request_digest or evidence.get('request_digest')!=row.request_digest
            or digest(_data(row.snapshot_json))!=row.request_digest
            or digest(_data(attempt.snapshot_json))!=row.request_digest):return False
    if state=='pre_submit_failed':return attempt.final_json is None
    return _data(attempt.final_json).get('request_digest')==row.request_digest


def _object(session,key):
    row=session.get(Setting,key)
    return _data(row.value_json) if row else {}


def active_limits(session, task_ids):
    from .deliveries import PublishDeliveryRow,DeliveryAttemptRow
    from yijianjifa.features.tasks.service import TaskItemRow
    from sqlalchemy import and_
    pairs=session.execute(select(PublishDeliveryRow,DeliveryAttemptRow).join(DeliveryAttemptRow,and_(
        DeliveryAttemptRow.delivery_id==PublishDeliveryRow.id,DeliveryAttemptRow.number==PublishDeliveryRow.attempt))
        .join(TaskItemRow,TaskItemRow.id==PublishDeliveryRow.item_id)
        .where(PublishDeliveryRow.task_id.in_(task_ids),TaskItemRow.state=='platform_rejected'))
    return {(row.account_id,row.platform,row.platform_user_id) for row,attempt in pairs
        if verified_limit(row,attempt,'platform_rejected') and _data(attempt.result_json).get('posting_limit_day')==today()}


def all_accounts_limited(session, task):
    from .deliveries import digest
    snapshot=_data(task.snapshot_json);targets=snapshot.get('daily_publish',{}).get('targets',[])
    progress=_object(session,'mix.daily.status:'+task.id);ids=progress.get('publish_task_ids',[])
    if not targets or type(ids) is not list:return False
    expected={(t['account_id'],t['platform'],t['platform_user_id']) for t in targets}
    owned=[]
    for identity in ids[:100]:
        if type(identity) is not str:continue
        source=_object(session,'multi_publish.daily_source:'+identity)
        if source.get('mix_task_id')==task.id and source.get('snapshot_digest')==digest(snapshot):owned.append(identity)
    return expected<=active_limits(session,owned)


def batch_limit_reason(engine, target):
    from .deliveries import PublishDeliveryRow, DeliveryAttemptRow
    with session_scope(engine) as db:
        row=db.get(PublishDeliveryRow,target['id'])
        if row is None:return None
        task_ids=[row.task_id]
        source=_object(db,'multi_publish.daily_source:'+row.task_id)
        parent=source.get('mix_task_id')
        if type(parent) is str and source.get('snapshot_digest'):
            candidates=_object(db,'mix.daily.status:'+parent).get('publish_task_ids',[])
            if type(candidates) is list:
                for candidate in candidates[:100]:
                    if type(candidate) is not str:continue
                    sibling=_object(db,'multi_publish.daily_source:'+candidate)
                    if sibling.get('mix_task_id')==parent and sibling.get('snapshot_digest')==source['snapshot_digest']:
                        task_ids.append(candidate)
        if (row.account_id,row.platform,row.platform_user_id) in active_limits(db,task_ids):return SKIP_REASON
    return None
