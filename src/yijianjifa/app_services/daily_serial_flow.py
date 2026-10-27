"""新每日批次的逐条流程和共用只读投影；原bulk不在此执行。"""
import copy
import json

from sqlalchemy import select, text

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.mixing.serial_gate import FLOW, ITEM, SOURCE, STATUS, is_serial, has_flow_marker, valid_snapshot, value, success, inspect_item, can_claim_next
from yijianjifa.features.publish.deliveries import digest, canonical
from yijianjifa.app_services.multi_publish_runtime import HostMultiPublishService, update_authorization


def read_serial_progress(session, task_row):
    snapshot = json.loads(task_row.snapshot_json)
    if not has_flow_marker(snapshot): return None
    if not valid_snapshot(snapshot, task_row.quantity):
        return {'mode': FLOW, 'quantity': task_row.quantity, 'item_index': min(task_row.current_index, task_row.quantity - 1),
            'item_number': min(task_row.current_index + 1, task_row.quantity), 'item_title': '', 'stage': 'needs_attention',
            'target_count': 0, 'item_received': 0, 'items_received': 0, 'targets_received': 0, 'targets_total': 0,
            'reason': '原批次配置尚不能核实，请查看记录；不会继续制作或派发。',
            'waiting_reason': 'configuration', 'needs_action': True, 'waiting_count': 0, 'needs_action_count': 1, 'error_count': 0}
    progress = value(session, STATUS + task_row.id)
    total, count = task_row.quantity, len(snapshot['daily_publish']['targets'])
    from yijianjifa.features.mixing.serial_gate import account_holds
    holds=account_holds(session,task_row)
    items = [inspect_item(session, task_row, i,holds=holds) for i in range(total)]
    current = next((i for i in items if not i['complete']), items[-1])
    index = current['index']
    received = sum(i['received'] for i in items)
    error_count = sum(i['error_count'] for i in items)
    error_count += int(progress.get('serial_error') is True)
    needs_count = sum(i['needs_action_count'] for i in items)
    skipped_count = sum(i['skipped_count'] for i in items)
    deferred_count = sum(i.get('deferred_count',0) for i in items)
    stage, reason, action = 'publishing', current['waiting_reason'], current['needs_action']
    from yijianjifa.features.mixing.serial_gate import all_accounts_unavailable
    all_limited = index>0 and success(session,task_row,index) is None and all_accounts_unavailable(session,task_row)
    if progress.get('automatic_stopped') or task_row.state in {'cancelled', 'cancelling'}:
        stage, reason, action = 'paused', 'configuration' if progress.get('serial_error') else current['waiting_reason'] if current['needs_action'] else 'none', progress.get('serial_error') is True or current['needs_action']
    elif all(i['complete'] for i in items) and task_row.state not in {'paused', 'interrupted'}: stage, reason, action = 'completed', 'none', False
    elif all_limited:stage, reason, action='paused','configuration',True
    elif progress.get('auth_phase'):
        stage, reason, action = 'needs_attention' if progress.get('auth_phase') != 'checking_login' else 'publishing', 'authorization', progress.get('auth_phase') != 'checking_login'
    elif current['needs_action']: stage = 'paused' if current['error_count'] and task_row.state in {'paused', 'interrupted'} else 'needs_attention'
    elif progress.get('reason_code') == 'daily_authorization_required':
        stage, reason, action = 'needs_attention', 'authorization', True
    elif task_row.state in {'paused', 'interrupted'}: stage = 'paused'
    elif not success(session, task_row, index): stage, reason = 'making', 'none'
    elif reason == 'schedule': stage = 'waiting_schedule'
    message = ((progress.get('reason') or current['reason']) if reason == 'authorization' or progress.get('automatic_stopped') else
               '' if stage == 'making' and not action else current['reason'])
    if all_limited or progress.get('reason_code')=='daily_posting_limit_all':
        stage,reason,action='paused','configuration',True
        message='全部账号今日投稿次数已达上限，本批已暂停；未制作的内容保留，避免额外消耗配音额度。'
        if deferred_count:message='全部账号暂不可继续发布，本批已暂停；请先处理登录、验证或待核对结果，未制作内容保留。'
    voice_pause = value(session, 'mix-voice-pause:' + task_row.id)
    if voice_pause and task_row.state in {'paused', 'interrupted'} and not progress.get('automatic_stopped'):
        stage, reason, action = 'needs_attention', 'result' if voice_pause.get('code') == 'job_unknown' else 'configuration', True
        message = voice_pause.get('message') or '等待处理配音后重试本条。'
    return {'mode': FLOW, 'quantity': total, 'item_index': index, 'item_number': index + 1,
        'item_title': snapshot['copies'][index].get('title', ''), 'stage': stage,
        'target_count': count, 'item_received': current['received'],
        'items_received': sum(i['received']==count for i in items), 'targets_received': received,
        'targets_total': total * count, 'reason': message, 'waiting_reason': reason,
        'needs_action': action, 'waiting_count': max(0, total * count - received - error_count - deferred_count),
        'needs_action_count': needs_count or int(action), 'error_count': error_count, 'skipped_count': skipped_count,
        'deferred_count':deferred_count}


def _reuse_batch_login(service, task_id, index):
    """Reuse this host's online proof; every adapter still checks the live target.

    Explicit resume, a changed identity/session without SDK proof, and a new
    process must pass the original online check again.
    """
    from .daily_mixing import _put
    from yijianjifa.features.mixing.serial_gate import render_authorized
    with service.engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        with session_scope(connection) as session:
            task=session.get(MixTask,task_id)
            proof=value(session,STATUS+task_id)
            if (task is None or task.state!='waiting_publish' or task.pause_requested
                    or proof.get('auth_verified') not in {'before_make','before_publish'}
                    or not render_authorized(session,task,service.session_id)):
                return None
            targets=json.loads(task.snapshot_json)['daily_publish']['targets']
            actual=service._targets(connection,[{'account_id':t['account_id'],'platform':t['platform']} for t in targets])
            updated={**proof,'auth_stage':'before_publish','auth_verified':'before_publish',
                     'auth_item_index':index,'auth_targets':actual}
            if updated!=proof:_put(session,STATUS+task_id,updated)
            proof=updated
        connection.commit()
    return proof


def reconcile_serial(service, task_id):
    """只由已获授权的本机运行时调用；状态查询绝不进入此路径。"""
    from .daily_mixing import DailyMixingError, _put
    task = service._mix_task(task_id)
    snapshot, frozen = task.snapshot, task.snapshot['daily_publish']
    with session_scope(service.engine) as s:
        row = s.get(MixTask, task_id)
        progress = value(s, STATUS + task_id)
        serial = read_serial_progress(s, row)
        index = serial['item_index']
        video = success(s, row, index)
        item = inspect_item(s, row, index)
    if (service.closing.is_set() or service.runtime.closed.is_set() or progress.get('automatic_stopped')
            or task.state in {'paused', 'interrupted', 'cancelled', 'cancelling'}):
        return service.status(task_id)
    if serial['stage']=='paused' and serial['needs_action'] and progress.get('auth_host')==service.session_id:
        with service.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            with session_scope(c) as s:
                from yijianjifa.features.mixing.serial_gate import all_accounts_unavailable
                row=s.get(MixTask,task_id);proof=value(s,STATUS+task_id)
                if all_accounts_unavailable(s,row) and not proof.get('automatic_stopped'):
                    row.state='paused'
                    _put(s,STATUS+task_id,{**proof,'automatic_stopped':True,
                        'reason_code':'daily_posting_limit_all','reason':serial['reason']})
            c.commit()
        return service.status(task_id)
    if serial['stage'] == 'completed':
        with service.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            with session_scope(c) as s:
                row = s.get(MixTask, task_id)
                proof = value(s, STATUS + task_id)
                if (row.state == 'waiting_publish' and not row.pause_requested and not proof.get('automatic_stopped')
                        and not service.closing.is_set() and not service.runtime.closed.is_set()
                        and proof.get('auth_host') == service.session_id and can_claim_next(s, row)):
                    row.state = 'completed'
                    service._notify_serial_completed(s, row)
            c.commit()
        return service.status(task_id)
    if (video is None and task.state == 'waiting_publish' and task.current_index == index
            and not item['needs_action'] and progress.get('auth_host') == service.session_id):
        with service.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            with session_scope(c) as s:
                row = s.get(MixTask, task_id)
                proof = value(s, STATUS + task_id)
                if (row.state == 'waiting_publish' and not proof.get('automatic_stopped')
                        and not proof.get('auth_phase') and proof.get('auth_host') == service.session_id
                        and not service.closing.is_set() and not service.runtime.closed.is_set()
                        and can_claim_next(s, row)):
                    row.state = 'waiting'
            c.commit()
        service.wake_making()
        return service.status(task_id)
    if video is None or item['needs_action']:
        return service.status(task_id)
    if progress.get('auth_phase'): return service.status(task_id)
    progress=_reuse_batch_login(service,task_id,index)
    if progress is None:
        with service.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            service.prepare_continue(c, task_id, automatic=True)
            c.commit()
        service.start_check(task_id)
        return service.status(task_id)
    verified = progress.get('auth_targets', [])
    identity = lambda rows: [(t['account_id'], t['platform'], t['platform_user_id']) for t in rows]
    if identity(verified) != identity(frozen['targets']): raise DailyMixingError('daily_target_changed')
    frozen = {**frozen, 'targets': copy.deepcopy(verified), 'host_session': service.session_id}
    service._assert_identity(service.engine, frozen)
    intent = f'daily-{task_id}-i{index}'
    receipt = service.runtime.service.receipt(intent)
    if receipt is None:
        prepared = service.runtime.service.prepare(request_id=intent, video_ids=[video.id],
            targets=[{'account_id': t['account_id'], 'platform': t['platform'], 'fields': service._serial_fields(frozen, t)} for t in frozen['targets']],
            common_fields={}, fields_by_video={video.id: {k: snapshot['copies'][index].get(k, '') for k in ('title', 'body', 'tags')}},
            execution_mode=frozen['mode'], scheduled_times={video.id: frozen['slots'][index]} if frozen['mode'] != 'publish_now' else None,
            distribution_by_platform={t['platform']: 'whole_batch' for t in frozen['targets']},
            allow_new_request=True, inherit_account_settings=False)
        receipt = service.runtime.service.save(prepared['prepared_id'], request_id=intent, consent_partial=False)
    pid = receipt['id']
    with service.engine.connect() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE')
        with session_scope(c) as s:
            service._assert_automatic(c, task_id, frozen)
            service._assert_identity(c, frozen)
            binding = {'child_id': pid, 'index': index, 'finished_id': video.id, 'content_hash': video.content_hash,
                       'snapshot_digest': digest(snapshot)}
            previous = value(s, ITEM + task_id + ':' + str(index))
            if previous and previous != binding: raise DailyMixingError('daily_success_receipt_invalid')
            _put(s, ITEM + task_id + ':' + str(index), binding)
            _put(s, SOURCE + pid, {'mix_task_id': task_id, 'item_index': index, 'finished_id': video.id,
                                 'content_hash': video.content_hash, 'snapshot_digest': digest(snapshot), 'host_session': service.session_id})
            proof = value(s, STATUS + task_id)
            ids = list(dict.fromkeys([*proof.get('publish_task_ids', []), pid]))
            _put(s, STATUS + task_id, {**proof, 'publish_task_ids': ids})
        c.commit()
    awaiting_authorization = False
    with service.commands.lock, service.engine.connect() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE')
        with session_scope(c) as s:
            service._assert_automatic(c, task_id, frozen)
            service._assert_identity(c, frozen)
            proof = value(s, STATUS + task_id)
            host_service = HostMultiPublishService(c, service.paths, catalog=service.runtime.catalog, workers=service.runtime.registry)
            from yijianjifa.features.mixing.serial_gate import account_holds
            from yijianjifa.features.publish.deliveries import PublishDeliveryRow
            holds=account_holds(s,s.get(MixTask,task_id))
            for delivery in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==pid)):
                if delivery.account_id in holds and s.get(Setting,'multi_publish.target_pause:'+delivery.id) is None:
                    _put(s,'multi_publish.target_pause:'+delivery.id,{'daily_parent':task_id,
                        'request_digest':delivery.request_digest,'snapshot_digest':digest(snapshot),**holds[delivery.account_id]})
            detail = host_service.get_task(pid)
            current_item = inspect_item(s, s.get(MixTask, task_id), index)
            deferred_ids={t['id'] for t in current_item['targets'] if t.get('deferred')}
            ready = [d['id'] for d in detail['deliveries'] if d['state'] == 'waiting' and d['can_submit'] and d['id'] not in deferred_ids]
            awaiting_authorization = current_item['waiting_reason'] == 'authorization'
            rid = f'ds-{task_id}-{index}-{proof.get("serial_generation", 0)}'
            if awaiting_authorization:
                _put(s, STATUS + task_id, {**proof, 'handoff_authorized': False,
                    'auth_phase': 'needs_login', 'auth_verified': '', 'auth_targets': [],
                    'reason_code': 'daily_login_required', 'reason': current_item['reason']})
            elif ready and service.commands._receipt(c, rid) is None:
                received_ids = {target['id'] for target in current_item['targets'] if target['received'] or target.get('skipped') or target.get('failed') or target.get('deferred')}
                omitted = {d['id'] for d in detail['deliveries']} - set(ready)
                if omitted - received_ids:
                    raise DailyMixingError('existing_target_unresolved')
                check = service.commands.check({'id': pid, 'delivery_ids': ready}, service=host_service)
                params = {'id': pid, 'delivery_ids': ready, 'digest': check['digest'], 'consent': True,
                          'consent_partial': bool(omitted)}
                data = service.commands.apply(c, 'execution.start', params)
                update_authorization(s, pid, remove=received_ids)
                result = {'v': 1, 'request_id': rid, 'ok': True, 'data': data}
                c.execute(text('INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES (:id,:hash,:response)'),
                          {'id': rid, 'hash': digest({'operation': 'multi_publish.execution.start', 'params': params}), 'response': canonical(result)})
            if not awaiting_authorization:
                _put(s, STATUS + task_id, {**proof, 'handoff_authorized': True})
        c.commit()
    if not awaiting_authorization and not service.runtime.closed.is_set(): service.runtime.wake()
    return service.status(task_id)


def prepare_serial_continue(service, connection, task_id, *, automatic=False):
    """明确继续才能恢复用户停止或失败；内部回调只发起当前条认证。"""
    from .daily_mixing import DailyMixingError, MESSAGES, _put
    with session_scope(connection) as s:
        row = s.get(MixTask, task_id)
        if row.state in {'cancelled', 'cancelling'} or s.get(Setting, 'task-hidden:' + task_id):
            raise DailyMixingError('daily_batch_cancelled')
        progress = value(s, STATUS + task_id)
        if progress.get('reason_code') == 'daily_batch_cancelled':
            raise DailyMixingError('daily_batch_cancelled')
        if automatic and progress.get('automatic_stopped'): return
        if progress.get('auth_phase') == 'checking_login' and progress.get('auth_host') == service.session_id: return
        serial = read_serial_progress(s, row)
        index = serial['item_index']
        observed = inspect_item(s, row, index)
        if observed['waiting_reason'] in {'result', 'verification'}:
            if not automatic and progress.get('automatic_stopped'):
                _put(s, STATUS + task_id, {**progress, 'automatic_stopped': False, 'reason_code': '', 'reason': ''})
                row.state = 'waiting_publish'
            return
        saved = value(s, f'mix-result:{task_id}:{index}')
        video = success(s, row, index)
        if saved.get('state') == 'failed' and not video:
            if automatic or value(s, ITEM + task_id + ':' + str(index)):
                return
            history = value(s, f'mix-history:{task_id}:{index}', [])
            _put(s, f'mix-history:{task_id}:{index}', [*history, saved])
            s.delete(s.get(Setting, f'mix-result:{task_id}:{index}'))
            row.completed_count = max(0, row.completed_count - 1)
            row.failure_count = max(0, row.failure_count - 1)
            row.last_error = None
        if not video:
            if not automatic:
                from yijianjifa.features.mixing.queue import MixQueue
                MixQueue.authorize_voice_retry(s, task_id)
            from yijianjifa.features.mixing.serial_claim import SerialMixClaims
            claims = getattr(service, '_serial_claims', None)
            if claims is None: service._serial_claims = claims = SerialMixClaims()
            claims.recover(s, task_id, index, controlled_recovery=True)
        _put(s, STATUS + task_id, {**progress, 'auth_phase': 'checking_login',
            'auth_stage': 'before_publish' if video else 'before_make', 'auth_item_index': index,
            'auth_verified': '', 'auth_targets': [], 'auth_host': service.session_id,
            'automatic_stopped': False, 'serial_error': False, 'serial_generation': progress.get('serial_generation', 0) + int(not automatic),
            'login_accounts': [], 'login_repair_accounts': [], 'login_current': {}, 'login_failures': [],
            'login_holds':progress.get('login_holds',[]) if automatic else [],
            'reason_code': 'daily_login_checking', 'reason': MESSAGES['daily_login_checking']})
