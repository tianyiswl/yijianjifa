"""Bounded, read-only execution-panel projection of existing task evidence."""
from datetime import datetime, timezone
import json
import re

from sqlalchemy import case, func, select

from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.publish.deliveries import (DeliveryAttemptRow, PublishDeliveryRow,
    account_blocker, account_blocker_reason)
from yijianjifa.features.publish.coordinator import ERROR_CODES, ERROR_STAGES, ERROR_CATEGORIES
from yijianjifa.features.tasks.diagnostics import _redact_text
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from .multi_publish_dto import STAGES, REASONS, FAILURE_STAGES, FAILURE_CATEGORIES, observed_account_wait_blocker


_PLATFORMS = {'douyin': '抖音', 'kuaishou': '快手', 'bilibili': 'B站', 'channels': '视频号'}
_PUBLISH_STEPS = {'sms.cooldown': '等待短信验证码60秒间隔，结束后自动继续', 'account.wait': '等待账号空闲，空闲后自动继续', 'execution.start': '开始处理发布目标', 'submission.final_check': '正在核对最终发布资料',
                  'submission.authorized': '本次提交授权已核对', 'execution.error': '发布处理遇到问题',
                  'browser.close': '发布窗口清理结果已记录'}
_PLATFORM_STEPS = {
    'driver_start': '启动发布组件', 'browser_launch': '打开发布窗口',
    'session_restore': '恢复账号登录状态', 'navigation': '打开平台页面',
    'identity': '核对发布账号', 'upload_input': '检查视频上传入口',
    'upload': '上传视频', 'baseline_read': '读取已有作品与安排',
    'baseline_readback': '核对已有作品与安排', 'form_read': '读取发布表单',
    'form_readback': '核对发布表单', 'result_read': '读取平台处理结果',
    'result_readback': '核对平台处理结果', 'final_click': '执行平台提交',
    'baseline': '检查账号与已有作品', 'prepare': '准备上传与发布资料',
    'result': '读取平台处理结果', 'upload_observation': '检查视频上传状态',
    'login_check': '检查账号登录状态', 'login_valid': '账号登录有效',
    'login_expired': '账号登录已失效', 'submit_request': '核对平台提交请求',
    'submit_response': '识别平台提交回执', 'timeout': '发布处理超时',
    'preparation_timeout': '发布准备超时', 'cancelled': '发布处理已取消',
    'original_reminder_check': '检查原创提示', 'original_reminder_guard': '核对原创提示状态',
    'original_reminder_form': '核对原创提示表单', 'original_reminder_dismiss': '关闭原创收益提醒',
    'original_reminder_continue': '按不声明原创继续提交',
}
_PUBLISH_STEPS.update({platform + '.' + step: label
                       for platform in _PLATFORMS for step, label in _PLATFORM_STEPS.items()})
_MIX_STEPS = {'mix.started': '开始制作', 'mix.completed': '制作成功', 'mix.failed': '制作失败'}
_RESULT_STAGES = {**STAGES, 'platform_rejected': '平台已拒绝'}
_SETTLED = {'published', 'platform_scheduled', 'platform_accepted', 'processing', 'reviewing',
            'platform_rejected', 'pre_submit_failed', 'cancelled', 'missed_schedule'}


def _visible_publish_step(step):
    if step in {'execution.start','account.wait','sms.cooldown'}:return step
    if step in {'execution.result','execution.error'}:return 'result'
    if step=='submission.authorized':return 'submit'
    match=re.fullmatch(r'(?:douyin|kuaishou|channels|bilibili)\.([a-z_]+)',step)
    if not match:return None
    return {'upload':'upload','final_click':'submit','timeout':'result','preparation_timeout':'result',
            'login_expired':'login','cancelled':'result'}.get(match[1])


def _object(raw):
    try: value = json.loads(raw)
    except (TypeError, ValueError): return {}
    return value if type(value) is dict else {}


def _text(value):
    if type(value) is not str: return ''
    value = _redact_text(value)
    value = re.sub(r"(?i)[\"']?(?:session(?:_ref|_fingerprint)?|platform_user_id|uid)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", '[已隐藏]', value)
    return re.sub(r'(?<![\w])(?:[A-Za-z]:[\\/]|/)[^\s,;。]+', '[本地路径]', value)[:255]


def _stamp(value):
    if type(value) is not str or len(value) > 64: return None
    try: parsed = datetime.fromisoformat(value)
    except ValueError: return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _event(source, message, *, public_step=None, **context):
    if type(source) is not dict or _stamp(source.get('time')) is None: return None
    step = source.get('step')
    if type(step) is not str or not re.fullmatch(r'[A-Za-z_.]{1,80}', step): return None
    return {'time': source['time'], 'step': public_step or step, 'message': message, **context}


def _task_stamp(value):
    return _stamp(value.isoformat()) if isinstance(value, datetime) else _stamp(value)


def _owned_publish_ids(session, task_id, progress, frozen):
    """Follow a bounded native parent/child binding, never arbitrary forward links."""
    if not frozen: return [], True
    values = progress.get('publish_task_ids', [])
    if type(values) is not list: return [], False
    complete = len(values) <= 100
    candidates, seen = [], {task_id}
    for value in values[:100]:
        if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', value) or value in seen:
            complete = False
            continue
        seen.add(value); candidates.append(value)
    keys = ['multi_publish.daily_source:' + value for value in candidates] + ['task-hidden:' + value for value in candidates]
    settings = {row.key: row for row in session.scalars(select(Setting).where(Setting.key.in_(keys)))} if keys else {}
    centers = {row.id: row for row in session.scalars(select(TaskCenterRow).where(TaskCenterRow.id.in_(candidates)))} if candidates else {}
    owned = []
    for value in candidates:
        source = settings.get('multi_publish.daily_source:' + value)
        center = centers.get(value)
        if (source is not None and _object(source.value_json).get('mix_task_id') == task_id
                and center is not None and center.kind == 'multi_publish' and 'task-hidden:' + value not in settings):
            owned.append(value)
        else: complete = False
    return owned, complete


def _publish_reason(state, attempt, *, needs_verification=False):
    evidence = _object(attempt.result_json)
    final = bool(attempt.final_json)
    if needs_verification:
        return ('需完成平台验证；验证通过后再核对原发布结果，请勿重复投稿。' if final else
                '需完成平台验证，通过后继续本条任务。')
    from yijianjifa.features.publish.platform_feedback import failure_message
    platform_reason = failure_message(evidence, state)
    if platform_reason: return platform_reason
    if state in {'result_unknown', 'interrupted'}: return REASONS[state]
    if state == 'unknown': return '状态尚未确认，请核对原结果，勿重复投稿。'
    code = evidence.get('error_code')
    code = code if type(code) is str and code in ERROR_CODES else None
    if code == 'verification_required':
        # A stale verification code on a settled/cancelled target cannot turn
        # it back into required user work; the guarded flag already handled it.
        code = None
    if code == 'timeout' and state == 'pre_submit_failed' and not final:
        return '提交前处理超时，尚未提交平台。'
    phase, category = evidence.get('error_stage'), evidence.get('error_category')
    if (code == 'adapter_failed' and type(phase) is str and phase in ERROR_STAGES
            and type(category) is str and category in ERROR_CATEGORIES):
        impact = '已进入提交阶段，结果仍待核对，请勿重复投稿。' if final else '此目标尚未提交，不会自动重发。'
        return FAILURE_STAGES[phase] + '失败：' + FAILURE_CATEGORIES[category] + impact
    if state in {'published', 'platform_scheduled', 'platform_accepted', 'processing', 'reviewing'}:
        return ''
    if code in {'douyin_daily_post_limit','platform_daily_post_limit','posting_limit_skipped'}:
        return REASONS[code]
    if state == 'platform_rejected': return '平台未通过本目标，请核对平台原结果。'
    # A final boundary always wins over a contradictory pre-submit label/code.
    if final: return '已进入提交阶段，原结果仍待核对，请勿重复投稿。'
    return REASONS.get(code or state, '')


def read_execution(engine, task_id, *, task=None):
    """No browser/runtime/service calls, writes, new timestamps or inferred logs."""
    with session_scope(engine) as session:
        task = task or session.get(MixTask, task_id)
        if task is None: raise KeyError(task_id)
        snapshot = _object(task.snapshot_json)
        copies = snapshot.get('copies', [])
        copies = copies if type(copies) is list else []
        title = lambda index: _text(copies[index].get('title')) if 0 <= index < len(copies) and type(copies[index]) is dict else ''
        making = dict(succeeded=task.success_count, failed=task.failure_count,
                      completed=task.completed_count, total=task.quantity)
        current = {'label': {'waiting': '等待制作', 'paused': '制作已暂停', 'interrupted': '制作已中断',
                            'cancelled': '制作已取消', 'cancelling': '正在取消制作',
                            'completed': '制作完成', 'completed_with_errors': '制作结束，部分失败'}.get(task.state, '制作中')}
        if task.state == 'running' and task.current_index < task.quantity:
            current.update(index=task.current_index + 1, total=task.quantity, title=title(task.current_index))
        progress_row = session.get(Setting, 'mix.daily.status:' + task_id)
        progress = _object(progress_row.value_json) if progress_row else {}
        frozen = snapshot.get('daily_publish', {})
        frozen = frozen if type(frozen) is dict else {}
        if progress.get('auth_phase') == 'checking_login':
            targets = frozen.get('targets', [])
            count = len(targets) if type(targets) is list else 0
            current = {'label': f'正在检查本批{count}个账号' if count else '账号检查中', 'total': count}
            observed = progress.get('login_current')
            if (type(observed) is dict and type(observed.get('index')) is int
                    and type(observed.get('total')) is int and 1 <= observed['index'] <= observed['total'] <= 200
                    and _stamp(observed.get('time')) is not None):
                index, total = observed['index'], observed['total']
                label = f'正在检查第{index}/{total}个账号' if observed.get('state') == 'checking' else f'第{index}/{total}个账号：' + _text(observed.get('message'))
                current = {'label': label, 'account_label': _text(observed.get('account_label')), 'total': total}
        elif progress.get('auth_phase') in {'needs_login', 'needs_attention'}:
            current = {'label': '等待账号处理后继续'}
        events = []
        failure_reason = ''
        voice_pause = session.get(Setting, 'mix-voice-pause:' + task_id)
        if voice_pause is not None and task.state in {'paused', 'interrupted'}:
            pause = _object(voice_pause.value_json)
            failure_reason = _text(pause.get('message'))
            if failure_reason:
                current = {'label': '配音已暂停，等待处理后继续'}
        # Completed/failed receipts hold the only existing local-making events.
        first = max(0, task.current_index - 50)
        keys = {f'mix-result:{task_id}:{index}': index for index in range(first, min(task.quantity, task.current_index + 1))}
        receipts = session.scalars(select(Setting).where(Setting.key.in_(keys))).all() if keys else []
        for receipt in sorted(receipts, key=lambda row: keys[row.key]):
            index = keys[receipt.key]
            result = _object(receipt.value_json)
            diagnostic = result.get('diagnostic', {})
            short_reason = ''
            if result.get('state') == 'failed':
                short_reason = _text(diagnostic.get('reason') if type(diagnostic) is dict else '') or _text(result.get('error'))
                if short_reason: failure_reason = short_reason
            saved = result.get('execution_log', [])
            making_event=None
            for entry in (saved[-500:] if type(saved) is list else []):
                step = entry.get('step') if type(entry) is dict else None
                if type(step) is not str: continue
                label = _MIX_STEPS.get(step, '制作步骤已记录')
                if step == 'mix.failed' and short_reason: label += '：' + short_reason
                event = _event(entry, f'第{index + 1}条{label}', public_step=step if step in _MIX_STEPS else 'mix.step',
                               stage='making')
                if event and step in _MIX_STEPS:making_event=event
            if making_event:events.append(making_event)
        login_log = progress.get('login_log', [])
        login_events={};verified_events={}
        for entry in (login_log[-100:] if type(login_log) is list else []):
            if type(entry) is not dict or entry.get('step') not in {'daily.login_verified', 'daily.login_check_started', 'daily.login_check_step', 'daily.login_check_finished'}: continue
            stage = entry.get('stage')
            if type(stage) is not str or stage not in {'before_make', 'before_publish'}: continue
            label = '制作前' if stage == 'before_make' else '制作后、提交前'
            if entry['step'] == 'daily.login_verified':
                event = _event(entry, label + '账号登录检查已通过', stage=stage)
                if event:verified_events[stage]=event
            else:
                if entry['step']=='daily.login_check_step':continue
                index, total = entry.get('index'), entry.get('accounts')
                if type(index) is not int or type(total) is not int or not 1 <= index <= total <= 200: continue
                message = re.sub(r'（(?:累计|耗时)[^（）]{0,40}）$','',_text(entry.get('message'))).strip()
                event = _event(entry, message, stage=stage, account_label=_text(entry.get('account_label')))
                if event:login_events[index]=event
        events.extend(login_events.values() if login_events else verified_events.values())
        diagnostic = session.get(Setting, 'mix.daily.diagnostics:' + task_id)
        try: saved_diagnostics = json.loads(diagnostic.value_json) if diagnostic else []
        except (TypeError, ValueError): saved_diagnostics = []
        diagnostic_labels = {'daily.serial_recheck_failed': '逐条发布进度检查异常',
            'daily.serial_dispatch_failed': '当前条发布安排异常', 'daily.serial_runtime_failed': '逐条发布运行异常'}
        for entry in saved_diagnostics[-20:] if type(saved_diagnostics) is list else []:
            if type(entry) is not dict or type(entry.get('step')) is not str or entry['step'] not in diagnostic_labels: continue
            error_type = entry.get('error_type')
            suffix = '（' + error_type + '）' if type(error_type) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', error_type) else ''
            event = _event(entry, diagnostic_labels[entry['step']] + suffix + '，后续制作已停止。', stage='publishing')
            if event: events.append(event)
        publish_ids, complete = _owned_publish_ids(session, task_id, progress, frozen)
        publishing = {'targets': [], 'complete': complete, 'omitted': 0}
        publish_active = False
        if publish_ids:
            # 同一成品发给多个账号仍用同一个制作序号，不按发布目标位置编号。
            video_labels = {}
            receipt_prefix = 'mix-success:' + task_id + ':'
            for receipt in session.scalars(select(Setting).where(Setting.key.startswith(receipt_prefix))):
                suffix = receipt.key[len(receipt_prefix):]
                finished_id = _object(receipt.value_json).get('finished_id')
                if suffix.isdigit() and 0 <= int(suffix) < task.quantity and type(finished_id) is str:
                    video_labels[finished_id] = f'第{int(suffix) + 1}个视频'
            totals = {identity: (count, visible) for identity, count, visible in session.execute(select(
                PublishDeliveryRow.task_id, func.count(), func.sum(case((PublishDeliveryRow.hidden.is_(False), 1), else_=0))).where(
                PublishDeliveryRow.task_id.in_(publish_ids)).group_by(PublishDeliveryRow.task_id))}
            query = select(PublishDeliveryRow, TaskItemRow, DeliveryAttemptRow).join(
                TaskItemRow, (TaskItemRow.id == PublishDeliveryRow.item_id) & (TaskItemRow.task_id == PublishDeliveryRow.task_id)).join(
                DeliveryAttemptRow, (DeliveryAttemptRow.delivery_id == PublishDeliveryRow.id) & (DeliveryAttemptRow.number == PublishDeliveryRow.attempt)).where(
                PublishDeliveryRow.task_id.in_(publish_ids), PublishDeliveryRow.hidden.is_(False))
            count = session.scalar(select(func.count()).select_from(query.subquery()))
            if (any(identity not in totals or totals[identity][0] != totals[identity][1] for identity in publish_ids)
                    or count != sum(visible for _, visible in totals.values())):
                publishing['complete'] = False
            publishing['omitted'] = max(0, count - 400)
            if publishing['omitted']: publishing['complete'] = False
            deliveries = session.execute(query.order_by(case({value: index for index, value in enumerate(publish_ids)},
                value=PublishDeliveryRow.task_id), PublishDeliveryRow.position, PublishDeliveryRow.id).limit(400)).all()
            accounts = {a.id: a for a in session.scalars(select(AccountRow).where(
                AccountRow.id.in_({row.account_id for row, _, _ in deliveries})))}
            log_context = {}
            for row, item, attempt in deliveries:
                final_reading = bool(item.state == 'result_unknown' and attempt.owner_hash
                                     and attempt.final_json and not attempt.result_json)
                if final_reading:
                    owner = session.get(Setting, 'multi_publish.owner:' + attempt.owner_hash)
                    final_reading = not (owner and _object(owner.value_json).get('stopped') is True)
                execution_active = item.state == 'running' or final_reading
                if execution_active:
                    publish_active = True
                # 保存的执行授权只说明已排队，不能证明浏览器正在处理本目标。
                center = session.get(TaskCenterRow, row.task_id)
                queueable = (item.state == 'waiting' and not row.cancel_requested and center is not None
                             and center.state != 'paused' and session.get(Setting, 'multi_publish.target_pause:' + row.id) is None)
                blocker = account_blocker(session, row) if queueable else None
                observed_blocker = observed_account_wait_blocker(session,row,attempt) if queueable and not blocker else None
                blocker = blocker or observed_blocker
                account = accounts.get(row.account_id)
                nickname = account.nickname.replace(account.platform_user_id, '[账号]') if account and account.platform_user_id else '所选账号'
                label = _PLATFORMS.get(row.platform, '平台') + ' · ' + (_text(nickname) or '所选账号')
                fields = _object(row.snapshot_json).get('fields', {})
                public_title = _text(fields.get('title')) if type(fields) is dict else ''
                video_label = video_labels.get(row.video_id, '')
                state = item.state if item.state in _RESULT_STAGES else 'unknown'
                # Historical contradictions cannot make a final-started target
                # look safe to submit again. This is a read-only display guard.
                if attempt.final_json and state in {'pre_submit_failed', 'paused_auth'}:
                    state = 'result_unknown'
                if state == 'unknown': publishing['complete'] = False
                needs_verification = (state in {'paused_auth', 'result_unknown', 'interrupted'}
                    and not row.cancel_requested
                    and _object(attempt.result_json).get('error_code') == 'verification_required')
                scheduled = _stamp(row.scheduled_at)
                from yijianjifa.features.publish.posting_limits import verified_limit
                skipped=verified_limit(row,attempt,state)
                publishing['targets'].append(dict(id=row.id, account_id=row.account_id,
                    platform=row.platform if row.platform in _PLATFORMS else 'unknown',
                    platform_label=_PLATFORMS.get(row.platform, '平台'), account_label=label,
                    title=public_title, state=state, state_label='投稿上限，已跳过' if skipped else _RESULT_STAGES.get(state, '状态待核对'),
                    skipped=skipped,
                    needs_verification=needs_verification,
                    execution_active=execution_active,
                    reason=(REASONS['observed_account_result_unresolved'] if observed_blocker else REASONS[account_blocker_reason(blocker)]) if blocker else _publish_reason(state, attempt, needs_verification=needs_verification),
                    **({'blocked_by': blocker} if blocker else {}),
                    scheduled_at=scheduled.isoformat() if scheduled else None))
                log_context[f'publish.execution:{row.id}:{row.attempt}'] = dict(account_label=label, title=video_label, stage='publishing')
                if (execution_active or item.state == 'paused_auth') and ('account_label' not in current or execution_active and not current.get('execution_active')):
                    current = dict(label='正在核对提交结果' if final_reading else STAGES.get(item.state, '发布处理中'), account_label=label,
                                   execution_active=execution_active,
                                   title=public_title, index=row.position + 1, total=len(deliveries))
            logs = session.scalars(select(Setting).where(Setting.key.in_(log_context)).order_by(
                Setting.updated_at.desc(), Setting.key).limit(20))
            for log in logs:
                try: saved = json.loads(log.value_json)
                except (ValueError, TypeError): continue
                step_events = {}
                for entry in (saved[-500:] if type(saved) is list else []):
                    step = entry.get('step') if type(entry) is dict else None
                    if type(step) is not str: continue
                    group=_visible_publish_step(step)
                    if group is None:continue
                    # Adapter observations can repeat every polling cycle. This
                    # panel lists steps once per target/attempt; the original
                    # diagnostic log remains intact, including changed checks.
                    if group in step_events and group!='result':continue
                    message = _PUBLISH_STEPS.get(step, '发布步骤已记录')
                    if group=='submit':message='提交发布'
                    if step == 'execution.result':
                        checks = entry.get('checks', {})
                        state = checks.get('state') if type(checks) is dict else None
                        message = _RESULT_STAGES.get(state, '发布处理结果已记录') if type(state) is str else '发布处理结果已记录'
                    public_step = step if step in _PUBLISH_STEPS or step == 'execution.result' else 'publish.step'
                    event = _event(entry, message, public_step=public_step, **log_context[log.key])
                    if event:
                        # Reconciliation may update the final result within the
                        # same attempt. Keep its newest saved result in one row.
                        step_events[group] = event
                events.extend(step_events.values())
            if (publishing['targets'] and not publish_active and not progress.get('auth_phase')
                    and task.state in {'completed', 'completed_with_errors'}):
                states = {target['state'] for target in publishing['targets']}
                unresolved = [target for target in publishing['targets'] if target['state'] not in {
                    'published', 'platform_scheduled', 'platform_accepted', 'processing', 'reviewing', 'cancelled'}]
                if (publishing['complete'] and unresolved and all(target['needs_verification'] for target in unresolved)
                        and not task.failure_count and not progress.get('automatic_stopped')):
                    current = {'label': '等待完成平台验证'}
                elif states & {'result_unknown', 'interrupted', 'unknown'}:
                    deferred = sum(target.get('blocked_by', {}).get('state') in {'result_unknown', 'interrupted'}
                                   for target in publishing['targets'])
                    current = {'label': f'等待核对原结果，{deferred}项暂缓' if deferred else '等待核对原结果'}
                elif publishing['complete'] and states <= _SETTLED:
                    current = {'label': '发布处理结束'}
                elif not states & {'running', 'paused_auth'}:
                    current = {'label': '等待发布处理'}
        from .daily_serial_flow import read_serial_progress
        serial = read_serial_progress(session, task)
        if serial is not None:
            serial = {**serial, 'item_title': _text(serial.get('item_title')), 'reason': _text(serial.get('reason'))}
            if progress.get('auth_phase') != 'checking_login':
                waits = {'dispatch': '等待提交当前视频', 'account': '等待账号空闲',
                    'receipt': '等待平台确认接收', 'schedule': '等待定时时间',
                    'verification': '等待完成平台验证', 'result': '等待核对原结果',
                    'authorization': '等待确认后继续', 'configuration': '等待完善发布设置'}
                label = ('本条处理遇到错误，后续等待处理' if serial['error_count']>serial.get('skipped_count',0) and serial['stage'] in {'paused','needs_attention'} else
                    '已暂停，等待继续' if serial['stage'] == 'paused' else
                    f"本批处理完成，{serial['needs_action_count']}项待处理" if serial['stage']=='completed' and serial['needs_action_count'] else
                    f"本批处理完成，{serial['skipped_count']}项因投稿上限已跳过" if serial['stage']=='completed' and serial.get('skipped_count') else
                    '本批全部视频已被平台接收' if serial['stage'] == 'completed' else
                    '正在制作' if serial['stage'] == 'making' else
                    waits.get(serial['waiting_reason'], '等待平台确认接收'))
                current = dict(label=label, index=serial['item_number'], total=serial['quantity'],
                    title=serial['item_title'])
        events.sort(key=lambda event: _stamp(event['time']))
        auth_phase = progress.get('auth_phase')
        if task.state == 'cancelled' or progress.get('automatic_stopped'):
            active = False
        elif auth_phase:
            active = auth_phase == 'checking_login'
        elif task.state in {'paused', 'interrupted'}:
            active = False
        else:
            active = (publish_active or serial['stage'] == 'making' and task.state == 'running'
                if serial is not None else publish_active if publish_ids else task.state in {'waiting', 'running', 'cancelling'})
        started = _task_stamp(getattr(task, 'created_at', None))
        activity = [_stamp(event['time']) for event in events]
        # 已全部结算的逐条批次以真实执行回执计时，重开后的记账不算继续执行。
        if serial is None or serial['stage'] != 'completed' or not activity:
            activity.append(_task_stamp(getattr(task, 'updated_at', None)))
        last_activity = max((value for value in activity if value is not None), default=None)
        # Elapsed batch time includes queue/login waits. Setting.updated_at is
        # not work evidence: startup may rewrite SOURCE/authorization records.
        timing = {'started_at': started.isoformat() if started else None,
                  'last_activity_at': last_activity.isoformat() if last_activity else None, 'active': active}
        result = {'current': current, 'events': events[-100:], 'making': making, 'timing': timing, 'publishing': publishing}
        if serial is not None: result['serial'] = serial
        if serial is not None:
            from .mix_publish_report import build_report
            result['report'] = build_report(session,task,serial)
            if (task.state in {'completed','completed_with_errors'} and not active
                    and serial['stage']!='completed' and result['report'].get('ready')):
                # 已关闭批次的历史回执用于展示；不让本地隐藏记录重新授予执行权限。
                result.update(closed_report=True,serial=None,current={})
        if failure_reason: result['reason'] = failure_reason
        return result
