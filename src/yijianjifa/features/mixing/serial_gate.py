"""逐条流程的尝试结束门闩；接收、明确失败及暂缓分别记账。"""
import json
from pathlib import Path

from sqlalchemy import select

from yijianjifa.storage.models import Setting
from yijianjifa.features.finished.service import FinishedVideo
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, DeliveryAttemptRow, PublishBatchReceiptRow, digest
from yijianjifa.features.publish.outcomes import COMPLETED_DELIVERY_STATES
from yijianjifa.features.tasks.service import TaskItemRow, TaskCenterRow
from .output_names import output_filename

FLOW = 'per_item_v1'
ITEM = 'mix.daily.item:'
STATUS = 'mix.daily.status:'
SOURCE = 'multi_publish.daily_source:'
ERRORS = {'pre_submit_failed', 'platform_rejected', 'failed'}


def value(session, key, default=None):
    row = session.get(Setting, key)
    if row is None:
        return {} if default is None else default
    try: return json.loads(row.value_json)
    except (ValueError, TypeError): return {} if default is None else default


def object_json(raw):
    try:
        result = json.loads(raw) if raw else {}
        return result if type(result) is dict else {}
    except (ValueError, TypeError): return {}


def same_time(first, second):
    if first is None or second is None: return first is second
    from datetime import datetime
    try: return datetime.fromisoformat(first) == datetime.fromisoformat(second)
    except (ValueError, TypeError): return False


def is_serial(snapshot):
    return (type(snapshot) is dict and type(snapshot.get('daily_publish')) is dict
            and snapshot['daily_publish'].get('execution_flow') == FLOW)


def has_flow_marker(snapshot):
    """字段不存在才属于旧bulk；未知、空或损坏的字段值必须关闭执行。"""
    return (type(snapshot) is dict and type(snapshot.get('daily_publish')) is dict
            and 'execution_flow' in snapshot['daily_publish'])


def valid_snapshot(snapshot, quantity):
    if not is_serial(snapshot) or type(quantity) is not int or not 1 <= quantity <= 100: return False
    frozen = snapshot['daily_publish']
    targets, copies, slots = frozen.get('targets'), snapshot.get('copies'), frozen.get('slots')
    return (type(targets) is list and 1 <= len(targets) <= 200
        and all(type(t) is dict and all(type(t.get(k)) is str and bool(t[k])
            for k in ('account_id', 'platform', 'platform_user_id', 'session_fingerprint')) for t in targets)
        and type(copies) is list and len(copies) == quantity and all(type(c) is dict for c in copies)
        and type(slots) is list and len(slots) == quantity
        and type(snapshot.get('output_directory')) is str and bool(snapshot['output_directory'])
        and frozen.get('mode') in {'publish_now', 'platform_schedule', 'software_schedule'})


def success(session, task, index):
    """原index、路径及成品内容绑定；不按列表顺序重新配对。"""
    snapshot = json.loads(task.snapshot_json)
    receipt = value(session, f'mix-success:{task.id}:{index}')
    result = value(session, f'mix-result:{task.id}:{index}')
    video = session.get(FinishedVideo, receipt.get('finished_id')) if receipt else None
    expected = str(Path(snapshot['output_directory']) / task.id / output_filename(snapshot, index))
    if (not video or result.get('state') != 'completed' or video.origin_type != 'generated'
            or receipt.get('path') != expected or video.stored_path != expected):
        return None
    return video


def verified_failure(row, attempt, state):
    """明确未提交或明确拒绝只结束本次尝试，不等于平台接收。"""
    if attempt is None or state not in {'pre_submit_failed', 'platform_rejected'}: return False
    evidence = object_json(attempt.result_json)
    if (evidence.get('state') != state or evidence.get('protocol_violation')
            or evidence.get('request_digest') != row.request_digest or attempt.request_digest != row.request_digest
            or digest(object_json(row.snapshot_json)) != row.request_digest
            or digest(object_json(attempt.snapshot_json)) != row.request_digest): return False
    return (attempt.final_json is None if state == 'pre_submit_failed' else
            object_json(attempt.final_json).get('request_digest') == row.request_digest)


def account_holds(session, task):
    """仅原批次中已绑定的未决/验证目标暂停对应账号，不扩大到其他批次。"""
    snapshot = object_json(task.snapshot_json)
    frozen = snapshot.get('daily_publish', {})
    expected = {(t['account_id'], t['platform'], t['platform_user_id']) for t in frozen.get('targets', [])}
    proof = value(session, STATUS + task.id)
    ids = proof.get('publish_task_ids', [])
    owned = []
    for pid in ids[:100] if type(ids) is list else []:
        source = value(session, SOURCE + str(pid))
        index = source.get('item_index')
        if (type(pid) is str and source.get('mix_task_id') == task.id
                and source.get('snapshot_digest') == digest(snapshot) and type(index) is int
                and value(session, ITEM + task.id + ':' + str(index)).get('child_id') == pid):
            owned.append(pid)
    holds = {}
    for held in proof.get('login_holds',[]):
        if (type(held) is dict and (held.get('account_id'),held.get('platform'),held.get('platform_user_id')) in expected
                and any(t['account_id']==held['account_id'] and t['session_fingerprint']==held.get('session_fingerprint') for t in frozen.get('targets',[]))):
            from yijianjifa.features.accounts.service import AccountRow
            from yijianjifa.features.accounts.session_rotation import fingerprint
            account=session.get(AccountRow,held['account_id'])
            if account and fingerprint(account.session_ref)==held.get('session_fingerprint'):
                holds[held['account_id']]={'state':'login_required','reason':held.get('reason') or '账号需要登录或检查，暂缓此账号后续视频。'}
    for row in session.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id.in_(owned))):
        if row.hidden or row.cancel_requested or (row.account_id,row.platform,row.platform_user_id) not in expected: continue
        item = session.get(TaskItemRow,row.item_id)
        attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id,DeliveryAttemptRow.number==row.attempt))
        evidence = object_json(attempt.result_json) if attempt else {}
        if (not item or item.state not in {'result_unknown','paused_auth'} or evidence.get('state') != item.state
                or evidence.get('protocol_violation') or evidence.get('request_digest') != row.request_digest
                or attempt.request_digest != row.request_digest or digest(object_json(row.snapshot_json)) != row.request_digest
                or digest(object_json(attempt.snapshot_json)) != row.request_digest): continue
        if item.state == 'result_unknown' and object_json(attempt.final_json).get('request_digest') != row.request_digest: continue
        holds[row.account_id] = {'delivery_id':row.id,'state':item.state,
            'reason':'原投稿结果待核对，暂缓此账号后续视频。' if item.state=='result_unknown' else '账号需要登录或验证，暂缓此账号后续视频。'}
    return holds


def all_accounts_unavailable(session, task):
    from yijianjifa.features.publish.posting_limits import active_limits
    snapshot = object_json(task.snapshot_json)
    targets = snapshot.get('daily_publish',{}).get('targets',[])
    ids = value(session,STATUS+task.id).get('publish_task_ids',[])
    owned = [pid for pid in ids[:100] if value(session,SOURCE+str(pid)).get('snapshot_digest')==digest(snapshot)] if type(ids) is list else []
    limited = {a for a,_,_ in active_limits(session,owned)}
    return bool(targets) and {t['account_id'] for t in targets} <= set(account_holds(session,task)) | limited


def inspect_item(session, task, index, *, holds=None):
    from yijianjifa.features.accounts.service import AccountRow
    from yijianjifa.features.accounts.session_rotation import allows_rotation, fingerprint
    snapshot = json.loads(task.snapshot_json)
    frozen = snapshot['daily_publish']
    targets = frozen['targets']
    result = {'index': index, 'complete': False, 'received': 0, 'targets': [],
              'skipped_count': 0,
              'waiting_reason': 'dispatch', 'needs_action': False, 'error_count': 0,
              'needs_action_count': 0, 'reason': '等待派发本条发布任务。'}
    video = success(session, task, index)
    binding = value(session, ITEM + task.id + ':' + str(index))
    if not binding:
        if video is None:
            item = value(session, f'mix-result:{task.id}:{index}')
            if item.get('state') == 'failed':
                result.update(waiting_reason='configuration', needs_action=True, error_count=1,
                              needs_action_count=1, reason='本条制作失败，请处理后重试本条；后续制作尚未开始。')
        return result
    pid = binding.get('child_id')
    source = value(session, SOURCE + str(pid))
    center = session.get(TaskCenterRow, pid) if type(pid) is str else None
    receipt = session.get(PublishBatchReceiptRow, f'daily-{task.id}-i{index}')
    valid = (video is not None and binding.get('index') == index and binding.get('finished_id') == video.id
        and binding.get('content_hash') == video.content_hash and binding.get('snapshot_digest') == digest(snapshot)
        and source.get('mix_task_id') == task.id and source.get('item_index') == index
        and source.get('finished_id') == video.id and source.get('content_hash') == video.content_hash
        and source.get('snapshot_digest') == digest(snapshot)
        and center is not None and center.kind == 'multi_publish' and receipt is not None and receipt.task_id == pid
        and session.get(Setting, 'task-hidden:' + pid) is None)
    rows = list(session.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == pid))) if valid else []
    expected = {(t['account_id'], t['platform'], t['platform_user_id']) for t in targets}
    frozen_targets = {t['account_id']: t for t in targets}
    proof = value(session, STATUS + task.id)
    holds = account_holds(session,task) if holds is None else holds
    approved = proof.get('auth_targets')
    # 明确继续经过原账号在线核验后可更新会话，不改变最初文案/成品快照。
    if (proof.get('auth_verified') == 'before_publish' and proof.get('auth_item_index') == index
            and type(proof.get('auth_host')) is str and proof['auth_host'] and not proof.get('auth_phase')
            and type(approved) is list and len(approved) == len(targets)
            and all(type(t) is dict and type(t.get('session_fingerprint')) is str for t in approved)
            and {(t.get('account_id'), t.get('platform'), t.get('platform_user_id')) for t in approved} == expected):
        frozen_targets = {t['account_id']: t for t in approved}
    actual = {(r.account_id, r.platform, r.platform_user_id) for r in rows}
    valid = valid and len(rows) == len(targets) and actual == expected and all(
        not r.hidden and not r.cancel_requested and r.video_id == video.id and r.video_hash == video.content_hash
        and r.execution_mode == frozen['mode'] and same_time(r.scheduled_at, frozen['slots'][index]) for r in rows)
    if not valid:
        result.update(waiting_reason='configuration', needs_action=True, needs_action_count=1,
                      reason='本条发布关联或目标记录尚不能核实，请查看原任务；后续制作尚未开始。')
        return result
    result['child_id'] = pid
    receipt_problem = False
    for row in rows:
        item = session.get(TaskItemRow, row.item_id)
        attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == row.id,
                                                                DeliveryAttemptRow.number == row.attempt))
        evidence = object_json(attempt.result_json) if attempt else {}
        final = object_json(attempt.final_json) if attempt else {}
        state = item.state if item else 'unknown'
        accepted = (state in COMPLETED_DELIVERY_STATES and evidence.get('state') == state
                    and digest(object_json(row.snapshot_json)) == row.request_digest
                    and attempt.request_digest == evidence.get('request_digest') == row.request_digest
                    and digest(object_json(attempt.snapshot_json)) == row.request_digest
                    and final.get('request_digest') == row.request_digest and not evidence.get('protocol_violation'))
        contradictory = state in COMPLETED_DELIVERY_STATES and not accepted
        receipt_problem = receipt_problem or contradictory
        account = session.get(AccountRow, row.account_id) if state == 'waiting' else None
        target = frozen_targets[row.account_id]
        authorization = (state == 'waiting' and (account is None or account.login_state != 'valid'
            or not account.session_ref or not allows_rotation(session, row.account_id, target['platform'],
                target['platform_user_id'], target['session_fingerprint'], fingerprint(account.session_ref))))
        entry = {'id': row.id, 'state': 'result_unknown' if contradictory else state, 'received': accepted,
                 'authorization': authorization,
                 'verification': state in {'paused_auth', 'result_unknown', 'interrupted'} and evidence.get('error_code') == 'verification_required'}
        from yijianjifa.features.publish.posting_limits import verified_limit
        entry['skipped']=not contradictory and verified_limit(row,attempt,state)
        entry['failed']=not contradictory and verified_failure(row,attempt,state)
        pause = value(session,'multi_publish.target_pause:'+row.id)
        saved_hold = (pause.get('daily_parent')==task.id and pause.get('request_digest')==row.request_digest
                      and pause.get('snapshot_digest')==digest(snapshot))
        entry['deferred'] = not contradictory and (holds.get(row.account_id,{}).get('delivery_id')==row.id or
            state in {'waiting','waiting_configuration','waiting_adapter'} and saved_hold and attempt is not None and not attempt.final_json and not attempt.result_json)
        result['targets'].append(entry)
        result['received'] += int(accepted)
        result['skipped_count'] += int(entry['skipped'])
    result['error_count'] = sum(r['state'] in ERRORS for r in result['targets'])
    failed = sum(r['failed'] and not r['skipped'] for r in result['targets'])
    deferred = sum(r['deferred'] for r in result['targets'])
    result['deferred_count'] = deferred
    result['complete'] = all(r['received'] or r['skipped'] or r['failed'] or r['deferred'] for r in result['targets'])
    if result['complete']:
        result['needs_action_count'] = failed + deferred
        received,skipped=result['received'],result['skipped_count']
        reason='本条全部所选账号平台已接收。'
        if failed or deferred:
            reason=f'本条发布尝试已结束：接收{received}项，失败{failed}项'+(f'，暂缓{deferred}项' if deferred else '')+(f'，上限跳过{skipped}项' if skipped else '')+'；未完成项保留待处理。'
        elif skipped and received:
            reason=f'本条已被{received}个账号接收；另{skipped}个账号因投稿上限已跳过。'
        elif skipped and len(targets)==1:
            account=session.get(AccountRow,rows[0].account_id)
            name=(account.remark or account.nickname) if account else '所选账号'
            platform={'douyin':'抖音','bilibili':'B站','channels':'视频号','kuaishou':'快手'}.get(rows[0].platform,'平台')
            reason=f'{platform} · {name}：今日投稿次数已达上限，本条未发布，已跳过。'
        elif skipped:
            reason=f'本条{skipped}个账号均已达投稿上限，均未发布，已跳过。'
        result.update(waiting_reason='none',reason=reason)
        return result
    unfinished=[r for r in result['targets'] if not (r['received'] or r['skipped'] or r['failed'] or r['deferred'])]
    states = {r['state'] for r in unfinished}
    result['needs_action_count'] = failed+deferred+sum(r['authorization'] or r['state'] in ERRORS | {'paused_auth', 'result_unknown', 'interrupted', 'waiting_adapter', 'waiting_configuration', 'missed_schedule', 'cancelled', 'unknown'} for r in unfinished)
    if states & ERRORS:
        result.update(waiting_reason='configuration', needs_action=True,
                      reason='本条出现提交前错误或平台明确拒绝，请查看对应账号原因；后续制作尚未开始。')
    elif any(r['state'] in {'result_unknown', 'interrupted', 'unknown'} and not r['verification'] for r in unfinished):
        result.update(waiting_reason='result', needs_action=True,
                      reason='等待核对本条接收回执；回执记录缺失或证据不一致，后续制作尚未开始。' if receipt_problem else
                             '等待核对本条原发布结果；后续制作尚未开始，请勿重复投稿。')
    elif 'paused_auth' in states or any(r['verification'] for r in unfinished):
        result.update(waiting_reason='verification', needs_action=True, reason='等待完成本条平台验证；后续制作尚未开始。')
    elif states & {'waiting_adapter', 'waiting_configuration', 'missed_schedule', 'cancelled'}:
        result.update(waiting_reason='configuration', needs_action=True, reason='本条发布需要处理，请查看具体账号原因；后续制作尚未开始。')
    elif any(r['authorization'] for r in unfinished):
        result.update(waiting_reason='authorization', needs_action=True,
                      reason='所选账号需要重新检查登录，等待登录后继续本批；已完成的视频会保留，不会自动重复投稿。')
    else:
        reason = 'receipt'
        for row in rows:
            if row.execution_mode == 'software_schedule' and row.scheduled_at:
                from datetime import datetime, timezone
                if datetime.fromisoformat(row.scheduled_at) > datetime.now(timezone.utc): reason = 'schedule'
            events = value(session, f'publish.execution:{row.id}:{row.attempt}', [])
            if events and events[-1].get('step') == 'account.wait' and events[-1].get('checks', {}).get('busy') is True:
                reason = 'account'
        result.update(waiting_reason=reason, reason={'receipt': '等待本条平台确认接收。', 'account': '等待本条所选账号空闲。', 'schedule': '等待本条软件定时到点后提交平台。'}[reason])
    return result


def can_claim_next(session, task):
    """调用者必须在领取写事务中使用，最后一条也经过相同门闩。"""
    if not valid_snapshot(json.loads(task.snapshot_json), task.quantity): return False
    if task.current_index == 0:
        return True
    # 投稿上限只阻止继续制作，最后一条已有明确结果时仍须结束批次。
    if task.current_index < task.quantity and all_accounts_unavailable(session,task):return False
    holds=account_holds(session,task)
    return all(inspect_item(session, task, index,holds=holds)['complete'] for index in range(task.current_index))


def render_authorized(session, task, host_session):
    """新制作领取还须持有本机会话授权及原身份/可信SDK轮换证明。"""
    from yijianjifa.features.accounts.service import AccountRow
    from yijianjifa.features.accounts.session_rotation import allows_rotation, fingerprint
    snapshot = json.loads(task.snapshot_json)
    if not valid_snapshot(snapshot, task.quantity): return False
    proof = value(session, STATUS + task.id)
    if (not host_session or proof.get('auth_host') != host_session or proof.get('auth_phase')
            or proof.get('automatic_stopped') or session.get(Setting, 'task-hidden:' + task.id)):
        return False
    targets = snapshot['daily_publish']['targets']
    approved = proof.get('auth_targets', [])
    if [(t.get('account_id'), t.get('platform'), t.get('platform_user_id')) for t in targets] != [
            (t.get('account_id'), t.get('platform'), t.get('platform_user_id')) for t in approved]: return False
    holds=account_holds(session,task)
    for target in approved:
        account = session.get(AccountRow, target['account_id'])
        if target['account_id'] in holds: continue
        if (account is None or account.login_state != 'valid' or not allows_rotation(session, account.id,
                target['platform'], target['platform_user_id'], target['session_fingerprint'], fingerprint(account.session_ref))):
            return False
    return True
