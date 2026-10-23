"""Bounded, read-only execution-panel projection of existing task evidence."""
from datetime import datetime, timezone
import json
import re

from sqlalchemy import case, func, select

from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.features.publish.coordinator import ERROR_CODES, ERROR_STAGES, ERROR_CATEGORIES
from yijianjifa.features.tasks.diagnostics import _redact_text
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from .multi_publish_dto import STAGES, REASONS, FAILURE_STAGES, FAILURE_CATEGORIES


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
            for entry in (saved[-500:] if type(saved) is list else []):
                step = entry.get('step') if type(entry) is dict else None
                if type(step) is not str: continue
                label = _MIX_STEPS.get(step, '制作步骤已记录')
                if step == 'mix.failed' and short_reason: label += '：' + short_reason
                event = _event(entry, f'第{index + 1}条{label}', public_step=step if step in _MIX_STEPS else 'mix.step',
                               title=title(index), stage='making')
                if event: events.append(event)
        login_log = progress.get('login_log', [])
        for entry in (login_log[-100:] if type(login_log) is list else []):
            if type(entry) is not dict or entry.get('step') not in {'daily.login_verified', 'daily.login_check_started', 'daily.login_check_step', 'daily.login_check_finished'}: continue
            stage = entry.get('stage')
            if type(stage) is not str or stage not in {'before_make', 'before_publish'}: continue
            label = '制作前' if stage == 'before_make' else '制作后、提交前'
            if entry['step'] == 'daily.login_verified':
                event = _event(entry, label + '账号登录检查已通过', stage=stage)
            else:
                index, total = entry.get('index'), entry.get('accounts')
                if type(index) is not int or type(total) is not int or not 1 <= index <= total <= 200: continue
                message = f'第{index}/{total}个账号：' + _text(entry.get('message'))
                event = _event(entry, message, stage=stage, account_label=_text(entry.get('account_label')))
            if event: events.append(event)
        publish_ids, complete = _owned_publish_ids(session, task_id, progress, frozen)
        publishing = {'targets': [], 'complete': complete, 'omitted': 0}
        publish_active = False
        if publish_ids:
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
            grants = {}
            for row, item, attempt in deliveries:
                if item.state == 'running':
                    publish_active = True
                elif item.state == 'waiting' and not row.cancel_requested:
                    center = session.get(TaskCenterRow, row.task_id)
                    if center is not None and center.state != 'paused' and session.get(Setting, 'multi_publish.target_pause:' + row.id) is None:
                        # Exact-attempt native authority only; no runtime calls.
                        from .multi_publish_runtime import authorized_targets
                        if row.task_id not in grants:
                            grants[row.task_id] = authorized_targets(session, row.task_id)
                        publish_active = publish_active or row.id in grants[row.task_id]
                account = accounts.get(row.account_id)
                nickname = account.nickname.replace(account.platform_user_id, '[账号]') if account and account.platform_user_id else '所选账号'
                label = _PLATFORMS.get(row.platform, '平台') + ' · ' + (_text(nickname) or '所选账号')
                fields = _object(row.snapshot_json).get('fields', {})
                public_title = _text(fields.get('title')) if type(fields) is dict else ''
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
                publishing['targets'].append(dict(id=row.id, account_id=row.account_id,
                    platform=row.platform if row.platform in _PLATFORMS else 'unknown',
                    platform_label=_PLATFORMS.get(row.platform, '平台'), account_label=label,
                    title=public_title, state=state, state_label=_RESULT_STAGES.get(state, '状态待核对'),
                    needs_verification=needs_verification,
                    reason=_publish_reason(state, attempt, needs_verification=needs_verification),
                    scheduled_at=scheduled.isoformat() if scheduled else None))
                log_context[f'publish.execution:{row.id}:{row.attempt}'] = dict(account_label=label, title=public_title, stage='publishing')
                if item.state in {'running', 'paused_auth'} and 'account_label' not in current:
                    current = dict(label=STAGES.get(item.state, '发布处理中'), account_label=label,
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
                    # Adapter observations can repeat every polling cycle. This
                    # panel lists steps once per target/attempt; the original
                    # diagnostic log remains intact, including changed checks.
                    if step in step_events and step != 'execution.result': continue
                    message = _PUBLISH_STEPS.get(step, '发布步骤已记录')
                    if step == 'execution.result':
                        checks = entry.get('checks', {})
                        state = checks.get('state') if type(checks) is dict else None
                        message = _RESULT_STAGES.get(state, '发布处理结果已记录') if type(state) is str else '发布处理结果已记录'
                    public_step = step if step in _PUBLISH_STEPS or step == 'execution.result' else 'publish.step'
                    event = _event(entry, message, public_step=public_step, **log_context[log.key])
                    if event:
                        # Reconciliation may update the final result within the
                        # same attempt. Keep its newest saved result in one row.
                        step_events[step] = event
                events.extend(step_events.values())
            if (publishing['targets'] and not publish_active and not progress.get('auth_phase')
                    and task.state in {'completed', 'completed_with_errors'}):
                states = {target['state'] for target in publishing['targets']}
                unresolved = [target for target in publishing['targets'] if target['state'] not in {
                    'published', 'platform_scheduled', 'platform_accepted', 'processing', 'reviewing', 'cancelled'}]
                if (publishing['complete'] and unresolved and all(target['needs_verification'] for target in unresolved)
                        and not task.failure_count and not progress.get('automatic_stopped')):
                    current = {'label': '待完成平台验证'}
                elif states & {'result_unknown', 'interrupted', 'unknown'}:
                    current = {'label': '发布结果待核对'}
                elif publishing['complete'] and states <= _SETTLED:
                    current = {'label': '发布处理结束'}
                elif not states & {'running', 'paused_auth'}:
                    current = {'label': '等待发布处理'}
        events.sort(key=lambda event: _stamp(event['time']))
        auth_phase = progress.get('auth_phase')
        if task.state == 'cancelled' or progress.get('automatic_stopped'):
            active = False
        elif auth_phase:
            active = auth_phase == 'checking_login'
        elif task.state in {'paused', 'interrupted'}:
            active = False
        else:
            active = publish_active if publish_ids else task.state in {'waiting', 'running', 'cancelling'}
        started = _task_stamp(getattr(task, 'created_at', None))
        activity = [_task_stamp(getattr(task, 'updated_at', None)), *(_stamp(event['time']) for event in events)]
        last_activity = max((value for value in activity if value is not None), default=None)
        # Elapsed batch time includes queue/login waits. Setting.updated_at is
        # not work evidence: startup may rewrite SOURCE/authorization records.
        timing = {'started_at': started.isoformat() if started else None,
                  'last_activity_at': last_activity.isoformat() if last_activity else None, 'active': active}
        result = {'current': current, 'events': events[-100:], 'making': making, 'timing': timing, 'publishing': publishing}
        if failure_reason: result['reason'] = failure_reason
        return result
