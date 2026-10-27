"""A per-click making and V2 publishing intent, separate from reusable defaults.

Only native mix.daily.submit creates a session-bound publication intent. Saving,
reading, restoring settings and replaying an old command never renew that grant.
"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import uuid

from sqlalchemy import select, text

from yijianjifa.webui.session_check_diagnostics import sanitize_diagnostic

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideo
from yijianjifa.features.mixing.desktop_service import DesktopMixService, EmptyCopySelection
from yijianjifa.features.mixing.output_names import output_filename
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, DeliveryAttemptRow, canonical, digest
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from yijianjifa.features.publish.outcomes import COMPLETED_DELIVERY_STATES
from yijianjifa.features.tasks.service import TaskItemRow
from .account_access import public_account
from .library_queries import InvalidRequest, CommandRejected
from .multi_publish_runtime import AUTH, HostMultiPublishService, authorized_targets
from .multi_publish_dto import REASONS
from . import multi_publish_settings

SETTINGS = 'mix.daily.settings.v1'
STATUS = 'mix.daily.status:'
SOURCE = 'multi_publish.daily_source:'
LOCAL = timezone(timedelta(hours=8))
ACCEPTED = COMPLETED_DELIVERY_STATES
ATTENTION = {'paused_auth', 'result_unknown', 'interrupted', 'waiting_adapter', 'waiting_configuration',
             'pre_submit_failed', 'missed_schedule', 'platform_rejected'}
MESSAGES = {
    'daily_flow_invalid': '原批次流程版本尚不能核实，请查看配置；不会继续制作或派发。',
    'daily_authorization_required': '客户端已重新打开，请继续本批检查登录；已完成的视频会保留。',
    'daily_target_changed': '制作期间所选账号身份或登录资料已改变，请核对原账号后重新安排。',
    'daily_success_receipt_invalid': '本批成功成品与制作回执不一致，已停止自动发布，请核对任务记录。',
    'daily_no_successful_items': '本批没有制作成功的成品，请查看制作失败原因。',
    'daily_making_failed': '部分视频制作失败，成功视频保留原序号的发布时间；请查看失败项。',
    'daily_batch_paused': '本批制作已暂停，自动发布尚未开始。',
    'daily_handoff_failed': '制作已完成，但发布安排未完成，请查看任务记录核对；不会自动重复提交。',
    'daily_title_required': '本批存在没有标题的文案，请补齐标题后再制作并发布。',
    'app_closing': '客户端正在退出，本批尚未开始的自动发布已停止。',
    'daily_batch_cancelled': '本批制作已取消，未确认的成品不会自动发布。',
    'daily_schedule_invalid': '本批定时安排无效；每天的最后一条必须在午夜前，请减少每天数量、缩短间隔或提前首条时间，并核对平台允许的定时范围。',
    'daily_start_in_past': '所选开始日期或今天的发布时间已过，请重新选择未来日期，或今天尚未到达的时刻；具体日期不会自动顺延。',
    'daily_schedule_only': '一键制作并发布仅支持平台定时，请选择明天起至第13天内的开始日期。',
    'daily_tomorrow_only': '一键制作并发布只能从明天开始，请重新选择明天的发布时间；不会自动修改本次安排。',
    'daily_start_out_of_range': '开始日期请选择上海日期的明天起至第13天（含）；请重新选择，原日期不会自动修改。',
    'daily_end_out_of_range': '最后一条超过14天，请减少发布天数、每天条数或缩短间隔。',
    'execution_mode_not_verified': '当前平台通道尚未支持立即发布，请选择平台定时。',
    'schedule_requires_whole_minute': '当前平台要求按整分钟安排发布时间，请调整间隔。',
    'daily_login_required': '账号需要重新登录，登录后继续本批。已完成的视频会保留。',
    'daily_login_verification_required': '平台需要验证，请在登录窗口完成验证后继续本批。',
    'daily_login_check_failed': '暂时无法确认账号登录，可重新检查；仍无法确认时，请登录对应账号后继续本批。',
    'daily_login_identity_mismatch': '登录的账号与本批所选账号不一致，请使用原账号登录。',
    'daily_login_checking': '正在检查账号登录，检查通过后继续本批。',
}


def utcnow():
    return datetime.now(timezone.utc)


class DailyMixingError(ValueError):
    def __init__(self, code, *, context=None):
        self.code = code
        self.message = MESSAGES.get(code, REASONS.get(code, '本批设置未通过校验，请检查所选账号和平台发布资料。'))
        if context:
            self.message = context + '：' + self.message
        super().__init__(code)


def _put(session, key, value):
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value_json=canonical(value)))
    else:
        row.value_json = canonical(value)


def _json(session, key, default=None):
    row = session.get(Setting, key)
    return json.loads(row.value_json) if row else default


def _frozen_account_fields(frozen, target):
    # V1 batches predate per-account defaults. Their stored platform choices
    # remain the authority; neither form reads today's mutable preferences.
    by_account=frozen.get('fields_by_account')
    if by_account is not None:
        return copy.deepcopy(by_account[target['account_id']])
    return copy.deepcopy(frozen['fields_by_platform'][target['platform']])


_LOGIN_CHECK_STEPS = {
    'session_read': '读取保存的登录会话', 'profile_read': '读取账号浏览器资料',
    'clone': '准备独立检查资料', 'driver_start': '浏览器驱动启动', 'browser_launch': '浏览器启动',
    'session_restore': '恢复登录会话', 'surface': '检查页面登录状态',
    'identity_first': '读取账号身份', 'identity_second': '再次读取账号身份',
    'context_close': '关闭检查浏览器', 'driver_stop': '关闭浏览器驱动',
    'cleanup': '清理检查资源', 'worker_start': '启动独立检查', 'parent_deadline': '账号登录检查',
}


def _login_step_label(stage, platform):
    name = {'douyin': '抖音', 'kuaishou': '快手', 'channels': '视频号', 'bilibili': 'B站'}.get(platform, '平台')
    if stage in {'navigation_first', 'navigation_second'}:
        return ('打开' if stage == 'navigation_first' else '再次打开') + name + '后台'
    return _LOGIN_CHECK_STEPS.get(stage, '账号登录检查')


def _login_elapsed(elapsed_ms):
    return '累计 ' + format(elapsed_ms / 1000, '.3f').rstrip('0').rstrip('.') + ' 秒'


def _login_step_message(event, platform):
    label = _login_step_label(event['stage'], platform)
    if event['state'] == 'failed' and event.get('error_kind') == 'timeout':
        message = label + '超时'
    else:
        message = label + '：' + {'started': '开始', 'completed': '完成', 'failed': '未完成'}[event['state']]
    return message + '（' + _login_elapsed(event['elapsed_ms']) + '）'


def _login_check_message(state, reason='', diagnostic=None, platform=''):
    if state == 'valid':
        return '检查通过'
    if state == 'login_required':
        return '需要重新登录'
    if state == 'identity_mismatch':
        return '登录账号不一致'
    if reason == 'timeout':
        safe = sanitize_diagnostic(diagnostic)
        if safe is not None:
            return _login_step_label(safe['stage'], platform) + '超时，暂时无法确认登录'
        return '检查超时，暂时无法确认登录'
    if reason == 'network_unavailable':
        return '网络无法确认登录'
    if reason == 'verification_required':
        return '需要平台验证'
    return '暂时无法确认登录'


def _login_account_label(session, account_id):
    from .mix_execution import _text
    account = session.get(AccountRow, account_id) if type(account_id) is str else None
    if account is None:
        return '', ''
    nickname = account.nickname.replace(account.platform_user_id, '[账号]') if account.platform_user_id else account.nickname
    nickname = _text(nickname) or '所选账号'
    platform = {'douyin': '抖音', 'kuaishou': '快手', 'channels': '视频号', 'bilibili': 'B站'}.get(account.platform, '平台')
    return nickname, (platform + ' · ' + nickname)[:255]


def _login_repair_accounts(session, frozen, observations):
    """Optional login actions for the exact saved users, never expiry evidence."""
    from .daily_login_runtime import _PROGRESS_REASONS
    if type(observations) is not list:
        return []
    raw = frozen.get('targets') if type(frozen.get('targets')) is list else []
    targets = {target['account_id']: target for target in raw if type(target) is dict and type(target.get('account_id')) is str}
    result, seen = [], set()
    for observation in observations[:200]:
        if type(observation) is not dict:
            continue
        state, reason = observation.get('state'), observation.get('reason', '')
        if (type(state) is not str or state not in {'unavailable', 'identity_mismatch'}
                or state == 'unavailable' and reason == 'verification_required'):
            continue
        account_id = observation.get('account_id', observation.get('id'))
        if type(account_id) is not str or account_id in seen:
            continue
        target = targets.get(account_id)
        account = session.get(AccountRow, account_id) if target else None
        if (account is None or account.login_state == 'removed'
                or observation.get('platform') != target['platform']
                or (account.platform, account.platform_user_id) != (target['platform'], target['platform_user_id'])):
            continue
        seen.add(account_id)
        result.append({'id': account_id, 'platform': account.platform,
                       'nickname': _login_account_label(session, account_id)[0] or '所选账号', 'state': state,
                       'reason': reason if type(reason) is str and reason in _PROGRESS_REASONS else ''})
    return result


def read_status(engine, task_id):
    """Pure persistent projection for both the daily page and task center."""
    with session_scope(engine) as s:
        task = s.get(MixTask, task_id)
        if task is None:
            raise KeyError(task_id)
        snapshot = json.loads(task.snapshot_json)
        frozen = snapshot.get('daily_publish')
        if not frozen:
            raise KeyError(task_id)
        progress = _json(s, STATUS + task_id, {})
        publish_ids = list(progress.get('publish_task_ids', []))
        from yijianjifa.features.mixing.serial_gate import has_flow_marker, valid_snapshot
        serial_mode = has_flow_marker(snapshot)
        targets = frozen.get('targets') if type(frozen.get('targets')) is list else []
        platforms = len(targets) if serial_mode else len({t['platform'] for t in targets})
        total = task.quantity * platforms
        excluded_count = progress.get('excluded_count', 0)
        result = dict(id=task_id, mix_task_id=task_id, phase='making', publish_task_ids=publish_ids,
                      accepted=0, published=0, pending=total - task.failure_count * platforms,
                      failed=task.failure_count * platforms, total=total, reason='', reason_code='',
                      making_failed=task.failure_count, resolved_start=frozen['resolved_start'], submitted=False)
        result.update(excluded_count=excluded_count, excluded_targets=progress.get('excluded_targets', []))
        children = []
        for pid in publish_ids:
            grants = authorized_targets(s, pid)
            for row in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == pid).order_by(PublishDeliveryRow.position)):
                item = s.get(TaskItemRow, row.item_id)
                attempt = s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == row.id, DeliveryAttemptRow.number == row.attempt))
                evidence = json.loads(attempt.result_json) if attempt and attempt.result_json else {}
                children.append({'id': row.id, 'state': item.state, 'authorized': row.id in grants,
                                 'reason': evidence.get('error_code'), 'submitted': bool(attempt and attempt.final_json)})
        child_failure_reason = ''
        if children:
            result['accepted'] = sum(c['state'] in ACCEPTED for c in children)
            result['published'] = sum(c['state'] == 'published' for c in children)
            actual_errors = {'pre_submit_failed', 'platform_rejected', 'failed'}
            result['failed'] += sum(c['state'] in actual_errors for c in children)
            result['pending'] = excluded_count + sum(c['state'] not in ACCEPTED | actual_errors | {'cancelled'} for c in children)
            result['submitted'] = any(c['submitted'] for c in children)
            if all(c['state'] == 'cancelled' for c in children):
                result['phase'] = 'cancelled'
            elif any(c['state'] in ATTENTION or (c['state'] == 'waiting' and not c['authorized']) for c in children):
                result['phase'] = 'needs_attention'
                blocked = next(c for c in children if c['state'] in ATTENTION or (c['state'] == 'waiting' and not c['authorized']))
                child_failure_reason = blocked['reason'] if type(blocked['reason']) is str else ''
                result['reason_code'] = blocked['reason'] or ('daily_authorization_required' if not blocked['authorized'] else blocked['state'])
            elif result['pending']:
                result['phase'] = 'publishing'
            else:
                result['phase'] = 'completed' if result['published'] == len(children) else 'accepted'
        elif task.state in {'cancelled', 'cancelling'}:
            result.update(phase='cancelled', reason_code='daily_batch_cancelled', pending=0)
        elif task.state in {'paused', 'interrupted'} or task.pause_requested:
            result.update(phase='needs_attention', reason_code='daily_batch_paused')
        elif task.state in {'completed', 'completed_with_errors'}:
            result.update(phase='needs_attention', pending=0, reason_code=progress.get('reason_code') or 'daily_authorization_required')
        # Old hosts saved this authorization reminder even after every target
        # settled. Keep revoking grants, but report the original child failure.
        stale_reopen_reason = (progress.get('reason_code') == 'daily_authorization_required'
            and not progress.get('auth_phase') and not progress.get('automatic_stopped')
            and child_failure_reason and all(c['state'] in ACCEPTED | {'pre_submit_failed', 'platform_rejected', 'cancelled'} for c in children))
        if progress.get('reason_code') and result['phase'] not in {'cancelled', 'completed', 'accepted'} and not stale_reopen_reason:
            result['reason_code'] = progress['reason_code']
        if result['failed'] and result['phase'] in {'completed', 'accepted'}:
            result.update(phase='needs_attention', reason_code='existing_target_unresolved' if excluded_count else 'daily_making_failed')
        if excluded_count and result['phase'] != 'cancelled':
            result.update(phase='needs_attention', reason_code='existing_target_unresolved')
        if progress.get('automatic_stopped'):
            cancelled = progress.get('reason_code') == 'daily_batch_cancelled'
            result.update(phase='cancelled' if cancelled else 'needs_attention', reason_code=progress['reason_code'])
            if cancelled:
                result['pending'] = 0
        auth_phase = progress.get('auth_phase')
        result.update(login_accounts=progress.get('login_accounts', []), login_repair_accounts=[],
                      login_message='', can_continue=False)
        if auth_phase and not progress.get('automatic_stopped') and result['phase'] != 'cancelled':
            result['phase'] = auth_phase
            result['reason_code'] = progress.get('reason_code', '')
            result['login_message'] = MESSAGES.get(result['reason_code'], '')
            result['can_continue'] = auth_phase in {'needs_login', 'needs_attention'}
            if (result['can_continue'] and task.state != 'running' and not progress.get('handoff_authorized')
                    and not publish_ids and not s.get(Setting, 'task-hidden:' + task_id)):
                result['login_repair_accounts'] = _login_repair_accounts(
                    s, frozen, progress.get('login_repair_accounts', []))
                if not result['login_message'] and any(
                        account['state'] == 'unavailable' for account in result['login_repair_accounts']):
                    result['login_message'] = MESSAGES['daily_login_check_failed']
        code = result['reason_code']
        saved_reason = progress.get('reason') if code == progress.get('reason_code') else None
        # Older snapshots may have saved a normalized code as display text.
        # Keep meaningful account diagnostics, but prefer its Chinese mapping.
        result['reason'] = (saved_reason if saved_reason != code else None) or MESSAGES.get(code, REASONS.get(code, code))
        if task.state in {'paused', 'interrupted'} and not auth_phase and not progress.get('automatic_stopped'):
            pause = _json(s, 'mix-voice-pause:' + task_id, {})
            if pause.get('message'):
                result.update(phase='needs_attention', reason_code='cloud_voice_paused', reason=pause['message'], can_continue=True)
        states = {c['state'] for c in children}
        unverified_result = any(c['state'] in {'result_unknown', 'interrupted'} and c['reason'] != 'verification_required' for c in children)
        verification = any(c['state'] == 'paused_auth' or c['state'] in {'result_unknown', 'interrupted'} and c['reason'] == 'verification_required' for c in children)
        waiting_reason = ('result' if unverified_result else 'verification' if verification
                          else 'configuration' if states & {'waiting_adapter', 'waiting_configuration'} else 'receipt' if result['pending'] else 'none')
        result.update(waiting_reason=waiting_reason,
                      needs_action_count=excluded_count + sum(c['state'] in ATTENTION for c in children),
                      needs_action=bool(states & ATTENTION) or bool(excluded_count) or result.get('can_continue', False))
        if serial_mode:
            from .daily_serial_flow import read_serial_progress
            serial = read_serial_progress(s, task)
            result.update(serial=serial, total=serial['targets_total'], accepted=serial['targets_received'],
                pending=serial['waiting_count'], failed=serial['error_count'],
                waiting_reason=serial['waiting_reason'], needs_action=serial['needs_action'],
                needs_action_count=serial['needs_action_count'], reason=serial['reason'])
            result['phase'] = {'making': 'making', 'publishing': 'publishing', 'waiting_schedule': 'publishing',
                               'needs_attention': 'needs_attention', 'paused': 'needs_attention', 'completed': 'accepted'}[serial['stage']]
            if auth_phase and valid_snapshot(snapshot, task.quantity): result['phase'] = auth_phase
            cancelled = task.state in {'cancelled', 'cancelling'} or progress.get('reason_code') == 'daily_batch_cancelled'
            if cancelled: result['phase'] = 'cancelled'
            result['can_continue'] = (not cancelled and valid_snapshot(snapshot, task.quantity) and
                (serial['waiting_reason'] == 'authorization' or serial['stage'] == 'paused'
                 or task.state in {'paused', 'interrupted'} and task.failure_count > 0))
        from .mix_execution import read_execution
        result['execution'] = read_execution(engine, task_id, task=task)
        if result['execution'].get('closed_report'):
            report=result['execution']['report']
            result.update(phase='accepted',serial=None,total=report['total'],accepted=report['accepted'],
                failed=len(report['missed']),pending=0,can_continue=False,reason='',waiting_reason='none',
                needs_action=bool(report['missed']),needs_action_count=len(report['missed']))
        return result


class DailyMixingService:
    def __init__(self, engine, paths, commands):
        self.engine, self.paths, self.commands = engine, paths, commands
        self.runtime = commands.runtime
        self.session_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.auth = None
        self.wake_making = lambda: None
        self.voice_adapter = lambda: None
        self._auth_baselines = {}
        self.closing = threading.Event()
        # Ordinary V2 recovery is unchanged. Only this feature's old per-click
        # grants are withdrawn before the host's publishing worker can start.
        with session_scope(engine) as s:
            finished_serial = set()
            for task in s.scalars(select(MixTask)):
                from yijianjifa.features.mixing.serial_gate import has_flow_marker, can_claim_next
                if (task.state == 'completed' and has_flow_marker(json.loads(task.snapshot_json))
                        and task.current_index >= task.quantity and can_claim_next(s, task)):
                    finished_serial.add(task.id)
                    progress = _json(s, STATUS + task.id, {})
                    if progress.get('reason_code') == 'daily_authorization_required':
                        _put(s, STATUS + task.id, {**progress, 'auth_phase': '', 'reason_code': '', 'reason': ''})
                    continue
                frozen = json.loads(task.snapshot_json).get('daily_publish')
                if (not frozen or frozen.get('host_session') == self.session_id
                        or task.state in {'cancelled', 'cancelling'} or s.get(Setting, 'task-hidden:' + task.id)):
                    continue
                progress = _json(s, STATUS + task.id, {})
                if not progress.get('handoff_authorized') and not progress.get('automatic_stopped'):
                    _put(s, STATUS + task.id, {**progress, 'auth_phase': 'needs_attention',
                        'reason_code': 'daily_authorization_required', 'reason': MESSAGES['daily_authorization_required']})
            for row in s.scalars(select(Setting).where(Setting.key.startswith(STATUS))):
                if row.key[len(STATUS):] in finished_serial: continue
                progress = json.loads(row.value_json)
                if progress.get('auth_phase') and progress.get('auth_host') != self.session_id and not progress.get('automatic_stopped'):
                    progress.update(auth_phase='needs_attention', reason_code='daily_authorization_required',
                                    reason=MESSAGES['daily_authorization_required'])
                    row.value_json = canonical(progress)
            for row in s.scalars(select(Setting).where(Setting.key.startswith(SOURCE))):
                source = json.loads(row.value_json)
                if source.get('host_session') == self.session_id:
                    continue
                authorization = s.get(Setting, AUTH + row.key[len(SOURCE):])
                if authorization is not None:
                    s.delete(authorization)
                if source['mix_task_id'] in finished_serial: continue
                _put(s, STATUS + source['mix_task_id'], {
                    **_json(s, STATUS + source['mix_task_id'], {}),
                    'reason_code': 'daily_authorization_required', 'reason': MESSAGES['daily_authorization_required']})

    @staticmethod
    def validate(params, *, for_submit=True):
        if type(params) is not dict or set(params) != {'mix', 'publishing'} or type(params['mix']) is not dict:
            raise InvalidRequest()
        from .desktop_api import DesktopApi
        if 'publish_after' in params['mix']:
            raise InvalidRequest()
        DesktopApi._validate('mix.submit', params['mix'])
        p = params['publishing']
        base = {'targets', 'mode', 'time', 'interval_hours'}
        starts = ({'day_offset'}, {'start_date'})
        if type(p) is not dict or set(p) not in tuple(base | start | daily for start in starts for daily in (set(), {'days', 'per_day'})):
            raise InvalidRequest()
        if 'days' in p and any(type(p[k]) is not int or not 1 <= p[k] <= 10000 for k in ('days', 'per_day')):
            raise InvalidRequest()
        quantity = p['days'] * p['per_day'] if 'days' in p else params['mix']['quantity']
        if for_submit and quantity > 100:
            raise InvalidRequest()
        if p['mode'] not in {'publish_now', 'platform_schedule'}:
            raise InvalidRequest()
        if type(p['time']) is not str or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', p['time']):
            raise InvalidRequest()
        if 'day_offset' in p:
            if type(p['day_offset']) is not int or p['day_offset'] not in {0, 1}:
                raise InvalidRequest()
        else:
            if type(p['start_date']) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', p['start_date']):
                raise InvalidRequest()
            try:
                datetime.fromisoformat(p['start_date'])
            except ValueError:
                raise InvalidRequest() from None
        if type(p['interval_hours']) not in {int, float} or not math.isfinite(p['interval_hours']) or not 0 < p['interval_hours'] <= 336:
            raise InvalidRequest()
        if p['interval_hours'] * 60 % 1:
            raise InvalidRequest()
        if type(p['targets']) is not list or not (1 if for_submit else 0) <= len(p['targets']) <= 200:
            raise InvalidRequest()
        seen = set()
        for target in p['targets']:
            if type(target) is not dict or set(target) != {'account_id', 'platform'}:
                raise InvalidRequest()
            aid = target['account_id']
            if type(aid) is not str or not 1 <= len(aid) <= 64 or aid in seen:
                raise InvalidRequest()
            PlatformCatalog().get(target['platform'])
            seen.add(aid)

    def read(self):
        with session_scope(self.engine) as s:
            settings = _json(s, SETTINGS)
        supported = {p['platform'] for p in self.runtime.catalog.list()}
        accounts = [public_account(a) for a in AccountService(self.engine).list() if a.platform in supported]
        capabilities = {}
        for platform in supported:
            try: adapter = self.runtime.catalog.transport_for(platform)
            except ValueError: adapter = None
            capabilities[platform] = sorted(getattr(adapter, 'verified_capabilities', ()))
        with session_scope(self.engine) as s:
            candidates = list(s.scalars(select(MixTask).order_by(MixTask.created_at.desc())))
            visible = [t for t in candidates if json.loads(t.snapshot_json).get('daily_publish')
                and not s.get(Setting, 'task-hidden:' + t.id)
                and t.state not in {'cancelled', 'cancelling'}]
            active_id = next((t.id for t in visible
                if _json(s, STATUS + t.id, {}).get('auth_phase') not in {'needs_login','needs_attention'}
                and (t.state in {'waiting', 'running'} or
                     t.state == 'waiting_publish' and _json(s, STATUS + t.id, {}).get('auth_host') == self.session_id or
                     _json(s, STATUS + t.id, {}).get('auth_phase') == 'checking_login')), None)
            # 清掉最近一批后保持空白，不用更早的历史补位。
            latest = next((t for t in candidates if json.loads(t.snapshot_json).get('daily_publish')), None)
            recent_id = (latest.id if latest and not active_id
                and latest.state not in {'cancelled', 'cancelling'}
                and not s.get(Setting, 'task-hidden:' + latest.id) else None)
            recent_ended = bool(recent_id and latest.state in {'completed', 'completed_with_errors'})
        active = self.status(active_id) if active_id else None
        recent = self.status(recent_id) if recent_id else None
        # A history result is display-only. Never turn it into an active batch
        # or expose a continue action through the recent-results fallback.
        # 结果未知的发布批次只恢复进度显示，不恢复原会话授权或账号操作。
        unresolved_recent = (recent_ended and recent and recent.get('phase') == 'needs_attention'
            and not recent.get('can_continue')
            and any(target.get('state') in {'result_unknown', 'interrupted'} or target.get('blocked_by')
                    for target in recent.get('execution', {}).get('publishing', {}).get('targets', [])))
        if recent and recent.get('phase') not in {'accepted', 'completed'} and not unresolved_recent and not recent.get('serial'):
            recent = None
        return {'settings': settings, 'accounts': accounts, 'has_saved_settings': settings is not None,
                'capabilities': capabilities, 'active_batch': active, 'recent_batch': recent}

    @staticmethod
    def resolved(publishing, *, for_submit=False, now=None):
        if for_submit and publishing['mode'] != 'platform_schedule':
            raise DailyMixingError('daily_schedule_only')
        if publishing['mode'] == 'publish_now':
            return None
        now = (now or utcnow()).astimezone(LOCAL)
        if for_submit:
            offset = (publishing['day_offset'] if 'day_offset' in publishing else
                      (datetime.fromisoformat(publishing['start_date']).date() - now.date()).days)
            # The selectable first day is independent of later daily slots;
            # each slot still goes through the real platform's own time window.
            if not 1 <= offset <= 13:
                raise DailyMixingError('daily_start_out_of_range')
        hour, minute = map(int, publishing['time'].split(':'))
        if 'start_date' in publishing:
            start = datetime.fromisoformat(publishing['start_date']).replace(hour=hour, minute=minute, tzinfo=LOCAL)
            if for_submit and start <= now:
                raise DailyMixingError('daily_start_in_past')
            return start.isoformat()
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=publishing['day_offset'])
        if start <= now:
            start += timedelta(days=1)
        return start.isoformat()

    def _targets(self, engine, targets, *, require_login=False):
        captured = []
        for target in targets:
            try:
                a = AccountService(engine).get(target['account_id'])
            except KeyError:
                raise DailyMixingError('daily_target_changed') from None
            if a.platform != target['platform']:
                raise DailyMixingError('daily_target_changed')
            if require_login and (a.login_state != 'valid' or not a.session_ref):
                raise DailyMixingError('account_verification_required')
            captured.append({**target, 'platform_user_id': a.platform_user_id,
                             'session_fingerprint': hashlib.sha256((a.session_ref or '').encode()).hexdigest()})
        return captured

    def save(self, connection, params):
        self.validate(params, for_submit=False)
        with session_scope(connection) as s:
            _put(s, SETTINGS, copy.deepcopy(params))
        return {'settings': copy.deepcopy(params), 'saved': True, 'resolved_start': self.resolved(params['publishing'])}

    def submit(self, connection, params):
        self.validate(params)
        if self.runtime.closed.is_set() or self.closing.is_set():
            raise CommandRejected('app_closing')
        submit_now = utcnow()
        p = params['publishing']
        targets = self._targets(connection, p['targets'])
        defaults = multi_publish_settings.read(connection)['settings']['fields_by_platform']
        start = self.resolved(p, for_submit=True, now=submit_now)
        mix = copy.deepcopy(params['mix'])
        if 'days' in p:
            mix['quantity'] = p['days'] * p['per_day']
        if start is None:
            slots = [None] * mix['quantity']
        elif 'days' in p:
            from yijianjifa.features.publish.schedule import ScheduleRule, build_schedule
            first = datetime.fromisoformat(start)
            last_today = first + timedelta(hours=(p['per_day'] - 1) * p['interval_hours'])
            if last_today.date() != first.date():
                raise DailyMixingError('daily_schedule_invalid')
            rule = ScheduleRule.daily(start=first, daily_count=p['per_day'], days=p['days'], interval_hours=p['interval_hours'])
            slots = [item.local_time.isoformat() for item in build_schedule(rule, count=mix['quantity'], now=submit_now)]
        else:
            slots = [(datetime.fromisoformat(start) + timedelta(hours=i * p['interval_hours'])).isoformat()
                     for i in range(mix['quantity'])]
        deadline = submit_now + timedelta(days=14)
        if any(slot is not None and datetime.fromisoformat(slot) > deadline for slot in slots):
            raise DailyMixingError('daily_end_out_of_range')
        frozen = {'version': 1, 'execution_flow': 'per_item_v1', 'host_session': self.session_id, 'targets': targets,
                  'fields_by_platform': {t['platform']: {k: copy.deepcopy(v) for k, v in defaults.get(t['platform'], {}).items()
                                                       if k not in {'title', 'body', 'tags'}} for t in targets},
                  'mode': p['mode'], 'resolved_start': start,
                  'slots': slots}
        from .account_publish_settings import AccountPublishSettings
        configured=getattr(self.runtime.service,'account_publish_settings',None)
        account_defaults=configured.for_engine(connection) if configured is not None else AccountPublishSettings(connection,catalog=self.runtime.catalog)
        frozen['fields_by_account']={t['account_id']:account_defaults.resolve_fields(t['account_id']) for t in targets}
        if 'days' in p:
            frozen['daily_rule'] = {k: p[k] for k in ('days', 'per_day', 'time', 'interval_hours')}
        try:
            task = DesktopMixService(connection, self.paths, tts=self.voice_adapter()).submit(**mix, daily_publish=frozen)
        except EmptyCopySelection:
            raise CommandRejected('empty_copy_selection') from None
        self._check_frozen_fields(connection, task.snapshot, frozen, allow_expired=True)
        # Persist the original batch/selected copies once, but never make an
        # item until the native online check has verified every selected user.
        with session_scope(connection) as s:
            s.get(MixTask, task.id).state = 'paused'
            _put(s, STATUS + task.id, {'auth_phase': 'checking_login', 'auth_stage': 'before_make',
                'auth_host': self.session_id, 'reason_code': 'daily_login_checking',
                'reason': MESSAGES['daily_login_checking'], 'login_accounts': [], 'login_repair_accounts': []})
        self.save(connection, params)
        return {'id': task.id, 'mix_task_id': task.id, 'state': 'paused', 'quantity': task.quantity,
                'resolved_start': start, 'phase': 'checking_login', 'publish_task_ids': [], 'submitted': False}

    def _check_frozen_fields(self, connection, snapshot, frozen, *, allow_expired=False):
        # This is a pure local/schema check over the actual frozen copy choices;
        # no finished-video rows or browser observations are fabricated.
        service = HostMultiPublishService(connection, self.paths, catalog=self.runtime.catalog, workers=self.runtime.registry)
        for i, item in enumerate(snapshot['copies']):
            for account_index, target in enumerate(frozen['targets'], 1):
                label = self.runtime.catalog.get(target['platform'])['label']
                context = f'{label}（所选账号{account_index}）第{i + 1}份文案'
                if frozen['slots'][i]:
                    clock = datetime.fromisoformat(frozen['slots'][i]).astimezone(LOCAL).strftime('%H:%M')
                    context += f'，定时{clock}'
                fields = {k: item.get(k, '') for k in ('title', 'body', 'tags')}
                fields['cover'] = {'kind': 'first_frame', 'frame_ms': 0}
                fields.update(_frozen_account_fields(frozen,target))
                if target['platform'] == 'bilibili':
                    try: schema = self.runtime.catalog.transport_for('bilibili').public_field_schema
                    except (ValueError, AttributeError): schema = {}
                    if schema.get('category_choice', {}).get('choices_source') == 'builtin':
                        from .bilibili_static_catalog import with_defaults
                        fields = with_defaults(fields)
                if not fields.get('title', '').strip():
                    raise DailyMixingError('daily_title_required', context=context)
                payload = {'version': 2, **target, 'fields': fields,
                           'execution_mode': frozen['mode'], 'scheduled_at': frozen['slots'][i]}
                state, reason = service._hold(payload)
                if state != 'waiting' and not (allow_expired and reason == 'account_verification_required'):
                    raise DailyMixingError(reason or state, context=context)

    def status(self, task_id):
        return read_status(self.engine, task_id)

    def _mix_task(self, task_id):
        return DesktopMixService(self.engine, self.paths).queue.get(task_id)

    _serial_fields = staticmethod(_frozen_account_fields)

    @staticmethod
    def _notify_serial_completed(session, task):
        from yijianjifa.features.mixing.queue import MixQueue
        MixQueue._notify_completed(session, task)

    def recheck_serial(self):
        """仅已授权运行时定期补偿丢通知；不从任何只读接口调用。"""
        from yijianjifa.features.mixing.serial_gate import is_serial
        with session_scope(self.engine) as s:
            ids = [t.id for t in s.scalars(select(MixTask).where(MixTask.state == 'waiting_publish')).all()
                   if is_serial(json.loads(t.snapshot_json)) and _json(s, STATUS + t.id, {}).get('auth_host') == self.session_id
                   and not _json(s, STATUS + t.id, {}).get('automatic_stopped')][:100]
        assertion = False
        for identity in ids:
            if self.closing.is_set() or self.runtime.closed.is_set(): break
            try: self.reconcile(identity)
            except Exception as error:
                self._stop_serial_error(identity, error, step='daily.serial_recheck_failed')
                assertion = assertion or isinstance(error, AssertionError)
        if assertion:
            raise AssertionError('逐条流程不变量检查失败，后续制作已停止；详见本地执行记录。')

    def _stop_serial_error(self, task_id, error, *, step='daily.serial_dispatch_failed'):
        """原始异常文字不出本机边界；每批只保存有限、可见的错误类型和固定说明。"""
        name = type(error).__name__
        name = name if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', name) else 'InternalError'
        reason = f'逐条流程检查异常（{name}），已停止后续制作，请查看本条任务记录。'
        with self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            with session_scope(c) as s:
                row = s.get(MixTask, task_id)
                if row is None: return
                history = _json(s, 'mix.daily.diagnostics:' + task_id, [])
                history = history if type(history) is list else []
                event = {'time': utcnow().isoformat(), 'step': step, 'error_type': name, 'reason': reason}
                _put(s, 'mix.daily.diagnostics:' + task_id, [*history[-19:], event])
                proof = _json(s, STATUS + task_id, {})
                if not proof.get('automatic_stopped') and row.state not in {'cancelled', 'cancelling'}:
                    _put(s, STATUS + task_id, {**proof, 'automatic_stopped': True, 'serial_error': True,
                        'auth_phase': '', 'reason_code': 'daily_serial_runtime_error', 'reason': reason})
                    row.state = 'paused'
                    row.last_error = reason
            c.commit()

    def record_serial_runtime_error(self, error):
        """发布runtime本身的检查失败同样落到有关批次，不静默重试。"""
        from yijianjifa.features.mixing.serial_gate import is_serial
        with session_scope(self.engine) as s:
            ids = [t.id for t in s.scalars(select(MixTask))
                   if is_serial(json.loads(t.snapshot_json)) and _json(s, STATUS + t.id, {}).get('auth_host') == self.session_id
                   and t.state in {'waiting_publish', 'waiting', 'running'}][:100]
        for identity in ids:
            self._stop_serial_error(identity, error, step='daily.serial_runtime_failed')

    def auth_current(self, task_id):
        if self.runtime.closed.is_set():
            return False
        with session_scope(self.engine) as s:
            return self._auth_guard(s, task_id) is not None

    def _auth_guard(self, s, task_id):
        task = s.get(MixTask, task_id)
        from yijianjifa.features.mixing.serial_gate import has_flow_marker, is_serial
        if task is not None:
            snapshot = json.loads(task.snapshot_json)
            if has_flow_marker(snapshot) and not is_serial(snapshot): return None
        progress = _json(s, STATUS + task_id, {})
        if (task is None or task.state in {'cancelled', 'cancelling', 'running'}
                or s.get(Setting, 'task-hidden:' + task_id) or progress.get('automatic_stopped')
                or progress.get('auth_host') != self.session_id
                or progress.get('auth_phase') != 'checking_login' or self.runtime.closed.is_set() or self.closing.is_set()):
            return None
        return task, progress

    def start_check(self, task_id):
        """Only pending checks are started; replaying a submit never renews work."""
        if self.auth is not None and self.auth_current(task_id):
            try:
                self.auth.start(task_id)
            except ValueError:
                self.auth_update(task_id, phase='needs_attention', login_accounts=[])

    def auth_begin(self, task_id):
        with self.lock, session_scope(self.engine) as s:
            guarded = self._auth_guard(s, task_id)
            if guarded is None:
                return []
            task, _ = guarded
            frozen = json.loads(task.snapshot_json)['daily_publish']
            targets = self._targets(self.engine, [{k: t[k] for k in ('account_id', 'platform')} for t in frozen['targets']])
            if [(t['account_id'], t['platform'], t['platform_user_id']) for t in targets] != [
                    (t['account_id'], t['platform'], t['platform_user_id']) for t in frozen['targets']]:
                raise DailyMixingError('daily_target_changed')
            self._auth_baselines[task_id] = copy.deepcopy(targets)
            from yijianjifa.features.mixing.serial_gate import account_holds, is_serial
            holds=account_holds(s,task) if is_serial(json.loads(task.snapshot_json)) else {}
            return [{**t, 'nickname': AccountService(self.engine).get(t['account_id']).nickname,
                     **({'deferred_reason':holds[t['account_id']]['reason']} if t['account_id'] in holds else {})} for t in targets]

    def auth_progress(self, task_id, *, index, total, account_id, stage, state='', reason='', diagnostic=None, guard):
        """A short guarded write of actual check boundaries, without credentials."""
        if (type(index) is not int or type(total) is not int or not 1 <= index <= total <= 200
                or stage not in {'started', 'finished'} or not callable(guard) or not guard()
                or stage == 'finished' and state not in {'valid', 'login_required', 'identity_mismatch', 'unavailable'}):
            return
        with self.lock, self.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(connection) as session:
                    guarded = self._auth_guard(session, task_id)
                    baseline = self._auth_baselines.get(task_id)
                    if guarded is None or not guard() or not baseline or len(baseline) != total:
                        return
                    task, progress = guarded
                    expected = baseline[index - 1]
                    account = session.get(AccountRow, account_id)
                    actual = ({'account_id': account.id, 'platform': account.platform,
                               'platform_user_id': account.platform_user_id,
                               'session_fingerprint': hashlib.sha256((account.session_ref or '').encode()).hexdigest()}
                              if account else None)
                    frozen = json.loads(task.snapshot_json)['daily_publish']['targets']
                    if (actual != expected or expected['account_id'] != account_id or len(frozen) != total
                            or any(frozen[index - 1][k] != expected[k] for k in ('account_id', 'platform', 'platform_user_id'))):
                        return
                    _, label = _login_account_label(session, account_id)
                    safe = sanitize_diagnostic(diagnostic) if stage == 'finished' else None
                    message = ('开始检查登录' if stage == 'started' else
                               _login_check_message(state, reason, safe, account.platform))
                    if safe is not None:
                        message += '（' + _login_elapsed(safe['elapsed_ms']) + '）'
                    now = utcnow().isoformat()
                    current = {'index': index, 'total': total, 'account_label': label,
                               'state': 'checking' if stage == 'started' else state, 'message': message, 'time': now}
                    previous = progress.get('login_log', [])
                    events = previous[-100:] if type(previous) is list else []
                    # Recorder times are monotonic offsets. Persist receipt UTC
                    # plus their real offsets rather than inventing wall times.
                    for event in safe['events'] if safe is not None else []:
                        events.append({'time': now, 'step': 'daily.login_check_step',
                                       'stage': progress['auth_stage'], 'accounts': total, 'index': index,
                                       'account_label': label, 'message': _login_step_message(event, account.platform),
                                       'check_stage': event['stage'], 'check_state': event['state'],
                                       'elapsed_ms': event['elapsed_ms']})
                    events.append({'time': now, 'step': 'daily.login_check_' + stage,
                                   'stage': progress['auth_stage'], 'accounts': total, 'index': index,
                                   'account_label': label, 'message': message})
                    if not guard():
                        return
                    _put(session, STATUS + task_id, {**progress, 'login_current': current, 'login_log': events[-100:]})
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def auth_update(self, task_id, *, phase, login_accounts):
        """Store only finite diagnostics, never raw platform errors or credentials."""
        from .daily_login_runtime import _PROGRESS_REASONS
        if phase == 'checking_login':
            return
        if (any(t.get('state')=='valid' for t in login_accounts)
                and all(t.get('state') in {'valid','deferred','login_required','unavailable','identity_mismatch'} for t in login_accounts)):
            with session_scope(self.engine) as s:
                guarded=self._auth_guard(s,task_id)
                from yijianjifa.features.mixing.serial_gate import is_serial
                partial_allowed=guarded is not None and is_serial(json.loads(guarded[0].snapshot_json))
            if partial_allowed:return self.auth_accept(task_id,login_accounts)
        interactive = [t for t in login_accounts if t.get('state') == 'login_required' or
                       t.get('state') == 'unavailable' and t.get('reason') == 'verification_required']
        if interactive and all(t.get('state') == 'valid' or t in interactive for t in login_accounts):
            phase = 'needs_login'
        code = ('daily_login_required' if phase == 'needs_login' else
                'daily_login_identity_mismatch' if any(t.get('state') == 'identity_mismatch' for t in login_accounts)
                else 'daily_login_check_failed')
        if any(t.get('reason') == 'verification_required' for t in interactive):
            code = 'daily_login_verification_required'
        # A shared publishing label must not replace the login-check action
        # already normalized above with its raw observation reason.
        precise = next((t.get('reason') for t in login_accounts if type(t.get('reason')) is str and
                        t['reason'] != 'verification_required' and
                        t['reason'] in set(MESSAGES) | set(REASONS)), None)
        if precise:
            code = precise
        with self.lock, self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(c) as s:
                    guarded = self._auth_guard(s, task_id)
                    if guarded is not None:
                        _, progress = guarded
                        safe = [{'id': t['account_id'], 'platform': t['platform'],
                                 'nickname': _login_account_label(s, t['account_id'])[0] or '所选账号', 'reason': code}
                                for t in interactive]
                        frozen = json.loads(guarded[0].snapshot_json)['daily_publish']
                        repairs = _login_repair_accounts(s, frozen, login_accounts)
                        details, failures = [], []
                        observed_at = utcnow().isoformat()
                        for index, target in enumerate(login_accounts[:200], 1):
                            if target.get('state') in {'valid', None}:
                                continue
                            _, label = _login_account_label(s, target.get('account_id'))
                            if label:
                                account = s.get(AccountRow, target.get('account_id'))
                                diagnostic = sanitize_diagnostic(target.get('diagnostic'))
                                message = _login_check_message(target['state'], target.get('reason'), diagnostic,
                                                               account.platform if account else '')
                                # Leave room for the cause in the bounded UI reason.
                                details.append(label[:80] + '：' + message)
                                if diagnostic is not None:
                                    message += '（' + _login_elapsed(diagnostic['elapsed_ms']) + '）'
                                summary = {'time': observed_at, 'stage': progress['auth_stage'],
                                           'index': index, 'accounts': min(len(login_accounts), 200),
                                           'account_label': label, 'message': message,
                                           'reason': target.get('reason') if type(target.get('reason')) is str
                                           and target['reason'] in _PROGRESS_REASONS else ''}
                                if diagnostic is not None:
                                    summary.update(check_stage=diagnostic['stage'], elapsed_ms=diagnostic['elapsed_ms'])
                                failures.append(summary)
                        message = MESSAGES.get(code, REASONS.get(code, '本批需要处理，请查看任务记录。'))
                        if details:
                            message = ('；'.join(details[:5]) + '。')[:255]
                        _put(s, STATUS + task_id, {**progress, 'auth_phase': phase, 'login_accounts': safe,
                            'login_repair_accounts': repairs, 'login_failures': failures,
                            'reason_code': code, 'reason': message})
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def auth_accept(self, task_id, observations):
        try:
            return self._accept_checked_accounts(task_id, observations)
        except (DailyMixingError, CommandRejected) as error:
            self.auth_update(task_id, phase='needs_attention', login_accounts=[{'reason': getattr(error, 'code', str(error))}])

    def _accept_checked_accounts(self, task_id, observations):
        """Verified same-user login may refresh sessions, never copies or slots."""
        ready = False
        with self.lock, self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(c) as s:
                    guarded = self._auth_guard(s, task_id)
                    if guarded is None:
                        return
                    task, progress = guarded
                    snapshot = json.loads(task.snapshot_json)
                    frozen = copy.deepcopy(snapshot['daily_publish'])
                    from yijianjifa.features.mixing.serial_gate import account_holds, is_serial
                    holds=account_holds(s,task) if is_serial(snapshot) else {}
                    actual = self._targets(c, [{k: t[k] for k in ('account_id', 'platform')} for t in frozen['targets']])
                    baseline = self._auth_baselines.get(task_id)
                    partial=is_serial(snapshot) and any(o.get('state')=='valid' for o in observations)
                    login_holds=[held for held in progress.get('login_holds',[]) if any(o.get('account_id')==held.get('account_id') and o.get('state')=='deferred' for o in observations)]
                    login_failures=list(progress.get('login_failures',[])) if login_holds else []
                    if (not baseline or actual != baseline or len(observations) != len(actual)
                            or any((o.get('state') != 'valid' and not(o.get('state')=='deferred' and t['account_id'] in holds) and not(partial and o.get('state') in {'login_required','unavailable','identity_mismatch'})) or o.get('state') in {'valid','deferred'} and o.get('platform_user_id') != t['platform_user_id']
                                   or o.get('account_id') != t['account_id'] for o, t in zip(observations, actual))):
                        raise DailyMixingError('daily_login_check_failed')
                    for account_index,(observation,target) in enumerate(zip(observations,actual),1):
                        if partial and observation.get('state') in {'login_required','unavailable','identity_mismatch'}:
                            diagnostic=sanitize_diagnostic(observation.get('diagnostic'))
                            message=_login_check_message(observation['state'],observation.get('reason'),diagnostic,target['platform'])
                            held={**target,'reason':message}
                            login_holds.append(held);holds[target['account_id']]={'reason':message}
                            _,label=_login_account_label(s,target['account_id'])
                            summary={'time':utcnow().isoformat(),'stage':progress['auth_stage'],'index':account_index,
                                'accounts':len(actual),'account_label':label,'message':message,'reason':observation.get('reason') or 'login_check_failed'}
                            if diagnostic is not None:
                                summary.update(check_stage=diagnostic['stage'],elapsed_ms=diagnostic['elapsed_ms'])
                                summary['message']+='（'+_login_elapsed(diagnostic['elapsed_ms'])+'）'
                            login_failures.append(summary)
                    for target in actual:
                        account=s.get(AccountRow,target['account_id'])
                        if target['account_id'] not in holds and (account is None or account.login_state!='valid' or not account.session_ref):
                            raise DailyMixingError('account_verification_required')
                    if [(t['account_id'], t['platform'], t['platform_user_id']) for t in actual] != [
                            (t['account_id'], t['platform'], t['platform_user_id']) for t in frozen['targets']]:
                        raise DailyMixingError('daily_login_identity_mismatch')
                    # A delayed login never moves the original frozen plan to
                    # another day or turns it into an immediate publication.
                    serial = frozen.get('execution_flow') == 'per_item_v1'
                    serial_index = progress.get('auth_item_index', task.current_index)
                    remaining = frozen['slots'][serial_index:] if serial else frozen['slots'] if task.completed_count >= task.quantity else frozen['slots'][task.current_index:]
                    if any(slot and datetime.fromisoformat(slot) <= utcnow() for slot in remaining):
                        raise DailyMixingError('daily_schedule_invalid')
                    frozen['targets'] = actual
                    frozen['host_session'] = self.session_id
                    self._check_frozen_fields(c, snapshot, frozen,allow_expired=bool(holds))
                    ready = progress['auth_stage'] == 'before_publish' if serial else task.completed_count >= task.quantity
                    if ready:
                        task.state = 'waiting_publish' if serial else 'completed_with_errors' if task.failure_count else 'completed'
                        task.pause_requested = task.auto_resume = False
                    else:
                        task.state = 'waiting'
                        task.pause_requested = task.auto_resume = False
                    from yijianjifa.features.mixing.queue import MixQueue
                    MixQueue.clear_voice_pause(s, task_id)
                    events = progress.get('login_log', [])[-99:]
                    events.append({'time': utcnow().isoformat(), 'step': 'daily.login_verified',
                                   'stage': progress['auth_stage'], 'accounts': len(actual)})
                    _put(s, STATUS + task_id, {**progress, 'auth_phase': '', 'auth_verified': progress['auth_stage'],
                        'auth_targets': actual, 'auth_host': self.session_id,
                        'automatic_stopped': False, 'login_accounts': [], 'login_repair_accounts': [], 'login_failures': login_failures,'login_holds':login_holds,
                        'reason_code': '', 'reason': '', 'login_log': events})
                c.commit()
            except BaseException:
                c.rollback()
                raise
        if self.closing.is_set() or self.runtime.closed.is_set():
            return
        if ready:
            self.reconcile(task_id)
        else:
            self.wake_making()

    def prepare_continue(self, connection, task_id, *, automatic=False):
        """Renew only this original batch after an explicit continue action."""
        with session_scope(connection) as s:
            candidate = s.get(MixTask, task_id)
            from yijianjifa.features.mixing.serial_gate import has_flow_marker, is_serial
            snapshot = json.loads(candidate.snapshot_json) if candidate else {}
            if has_flow_marker(snapshot) and not is_serial(snapshot): raise DailyMixingError('daily_flow_invalid')
            serial = has_flow_marker(snapshot)
        if serial:
            from .daily_serial_flow import prepare_serial_continue
            return prepare_serial_continue(self, connection, task_id, automatic=automatic)
        with session_scope(connection) as s:
            task = s.get(MixTask, task_id)
            if task is None or not json.loads(task.snapshot_json).get('daily_publish'):
                raise KeyError(task_id)
            progress = _json(s, STATUS + task_id, {})
            if task.state in {'cancelled', 'cancelling'} or s.get(Setting, 'task-hidden:' + task_id):
                raise DailyMixingError('daily_batch_cancelled')
            from yijianjifa.features.mixing.queue import MixQueue
            MixQueue.authorize_voice_retry(s, task_id)
            # Original live browser/after-final targets have their own exact
            # attempt continuation. This operation cannot create another one.
            if progress.get('handoff_authorized') or task.state in {'running', 'waiting'}:
                return
            if progress.get('auth_phase') == 'checking_login' and progress.get('auth_host') == self.session_id:
                return
            stage = 'before_publish' if task.completed_count >= task.quantity else 'before_make'
            _put(s, STATUS + task_id, {**progress, 'auth_phase': 'checking_login', 'auth_stage': stage,
                'auth_verified': '', 'auth_targets': [],
                'auth_host': self.session_id, 'automatic_stopped': False, 'login_accounts': [], 'login_repair_accounts': [],
                'login_current': {}, 'login_failures': [],
                'reason_code': 'daily_login_checking', 'reason': MESSAGES['daily_login_checking']})

    def stop_automatic(self, connection, task_id, *, cancelled=False):
        with session_scope(connection) as s:
            previous = _json(s, STATUS + task_id, {})
            # Once children exist they are independently controlled in the
            # task center. A making control never changes their platform state.
            task = s.get(MixTask, task_id)
            from yijianjifa.features.mixing.serial_gate import has_flow_marker
            serial = has_flow_marker(json.loads(task.snapshot_json))
            if previous.get('handoff_authorized') and not serial:
                return
            code = 'daily_batch_cancelled' if cancelled else 'daily_batch_paused'
            _put(s, STATUS + task_id, {**previous, 'automatic_stopped': True, 'auth_phase': '',
                                      'reason_code': code, 'reason': MESSAGES[code]})
            if task.state in {'completed', 'completed_with_errors', 'waiting_publish'}:
                task.state = 'cancelled' if cancelled else 'paused'

    def resume_automatic(self, connection, task_id):
        with session_scope(connection) as s:
            task = s.get(MixTask, task_id)
            frozen = json.loads(task.snapshot_json)['daily_publish']
            previous = _json(s, STATUS + task_id, {})
            if frozen['host_session'] == self.session_id and previous.get('reason_code') == 'daily_batch_paused':
                _put(s, STATUS + task_id, {**previous, 'automatic_stopped': False, 'reason_code': '', 'reason': ''})

    def release_cancelled_serial_sessions(self, task_id):
        """取消回执提交后，停止本批发布子目标并在所属线程释放页面，保留提交证据。"""
        from sqlalchemy import select
        from yijianjifa.features.mixing.serial_gate import has_flow_marker, SOURCE
        from yijianjifa.features.publish.deliveries import PublishDeliveryRow
        with session_scope(self.engine) as s:
            task=s.get(MixTask,task_id)
            if task is None or not has_flow_marker(json.loads(task.snapshot_json)):return
            proof=_json(s,STATUS+task_id,{})
            if not proof.get('automatic_stopped') or proof.get('reason_code')!='daily_batch_cancelled':return
            ids=[]
            for child in proof.get('publish_task_ids',[]):
                if _json(s,SOURCE+child,{}).get('mix_task_id')!=task_id:continue
                ids.extend(s.scalars(select(PublishDeliveryRow.id).where(PublishDeliveryRow.task_id==child)))
        for delivery_id in ids:
            self.runtime.service.cancel_delivery(delivery_id)
            self.runtime.cancel_session(delivery_id)

    def _record(self, task_id, **values):
        with self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(c) as s:
                    previous = _json(s, STATUS + task_id, {})
                    if previous.get('automatic_stopped'):
                        values.update(reason_code=previous['reason_code'], reason=previous['reason'])
                    _put(s, STATUS + task_id, {**previous, **values})
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _assert_identity(self, engine, frozen):
        actual = self._targets(engine, [{'account_id': t['account_id'], 'platform': t['platform']} for t in frozen['targets']])
        if actual != frozen['targets']:
            raise DailyMixingError('daily_target_changed')

    def _assert_automatic(self, engine, task_id, frozen):
        if 'execution_flow' in frozen and frozen['execution_flow'] != 'per_item_v1':
            raise DailyMixingError('daily_flow_invalid')
        if frozen['host_session'] != self.session_id:
            raise DailyMixingError('daily_authorization_required')
        if self.runtime.closed.is_set():
            raise DailyMixingError('app_closing')
        with session_scope(engine) as s:
            current = s.get(MixTask, task_id)
            progress = _json(s, STATUS + task_id, {})
            if progress.get('automatic_stopped'):
                raise DailyMixingError(progress['reason_code'])
            allowed = {'completed', 'completed_with_errors'}
            if frozen.get('execution_flow') == 'per_item_v1': allowed.add('waiting_publish')
            if current.state not in allowed:
                raise DailyMixingError('daily_batch_cancelled' if current.state in {'cancelled', 'cancelling'} else 'daily_batch_paused')

    def _successes(self, task):
        snapshot = task.snapshot
        results = []
        with session_scope(self.engine) as s:
            for i in range(task.quantity):
                receipt = _json(s, f'mix-success:{task.id}:{i}')
                result = _json(s, f'mix-result:{task.id}:{i}', {})
                if result.get('state') != 'completed':
                    continue
                expected = str(Path(snapshot['output_directory']) / task.id / output_filename(snapshot, i))
                finished = s.get(FinishedVideo, receipt.get('finished_id')) if receipt else None
                if (not receipt or finished is None or finished.origin_type != 'generated'
                        or receipt.get('path') != expected or finished.stored_path != expected):
                    raise DailyMixingError('daily_success_receipt_invalid')
                results.append((i, finished.id))
        return results

    def reconcile(self, task_id, *, now=None):
        with self.lock:
            try:
                return self._reconcile(task_id)
            except Exception as error:
                task = self._mix_task(task_id)
                if task.snapshot.get('daily_publish', {}).get('execution_flow') == 'per_item_v1' and not isinstance(error, (DailyMixingError, CommandRejected)):
                    self._stop_serial_error(task_id, error)
                    if isinstance(error, AssertionError): raise
                    return self.status(task_id)
                if isinstance(error, DailyMixingError):
                    code = error.code
                elif isinstance(error, CommandRejected):
                    code = str(error)
                elif isinstance(error, ValueError) and any(word in str(error) for word in ('target identity', 'target session')):
                    code = 'daily_target_changed'
                elif isinstance(error, ValueError) and any(word in str(error) for word in ('schedule', 'scheduled', 'time')):
                    code = 'daily_schedule_invalid'
                elif isinstance(error, ValueError) and 'history' in str(error):
                    code = 'existing_target_unresolved'
                elif isinstance(error, ValueError) and 'video' in str(error):
                    code = 'daily_success_receipt_invalid'
                else:
                    code = 'daily_handoff_failed'
                self._record(task_id, reason_code=code, reason=MESSAGES.get(code, REASONS.get(code, '本批发布安排未通过校验，请检查任务记录。')),
                             failure_type=type(error).__name__)
                return self.status(task_id)

    def _reconcile(self, task_id):
        task = DesktopMixService(self.engine, self.paths).queue.get(task_id)
        frozen = task.snapshot['daily_publish']
        if 'execution_flow' in frozen and frozen['execution_flow'] != 'per_item_v1':
            raise DailyMixingError('daily_flow_invalid')
        if frozen.get('execution_flow') == 'per_item_v1':
            from .daily_serial_flow import reconcile_serial
            return reconcile_serial(self, task_id)
        if task.state not in {'completed', 'completed_with_errors'}:
            return self.status(task_id)
        with self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(c) as s:
                    progress = _json(s, STATUS + task_id, {})
                    if progress.get('automatic_stopped'):
                        current = s.get(MixTask, task_id)
                        if current.state in {'completed', 'completed_with_errors'}:
                            current.state = 'cancelled' if progress['reason_code'] == 'daily_batch_cancelled' else 'paused'
                c.commit()
            except BaseException:
                c.rollback()
                raise
        if progress.get('automatic_stopped'):
            return self.status(task_id)
        if progress.get('handoff_authorized'):
            return self.status(task_id)
        if progress.get('auth_phase'):
            return self.status(task_id)
        # Rendering checkpoints hash the entire immutable MixTask snapshot.
        # Login renewal belongs to a separate native proof, never that snapshot.
        if progress.get('auth_verified') and progress.get('auth_host') == self.session_id:
            verified = progress.get('auth_targets', [])
            identity = lambda rows: [(t['account_id'], t['platform'], t['platform_user_id']) for t in rows]
            if identity(verified) != identity(frozen['targets']):
                raise DailyMixingError('daily_target_changed')
            frozen = {**frozen, 'targets': copy.deepcopy(verified), 'host_session': self.session_id}
        self._assert_automatic(self.engine, task_id, frozen)
        if self.auth is not None and progress.get('auth_verified') != 'before_publish':
            with self.engine.connect() as c:
                c.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    self.prepare_continue(c, task_id)
                    c.commit()
                except BaseException:
                    c.rollback()
                    raise
            self.start_check(task_id)
            return self.status(task_id)
        self._assert_identity(self.engine, frozen)
        successes = self._successes(task)
        if not successes:
            raise DailyMixingError('daily_no_successful_items')
        intent_id = 'daily-' + task_id
        receipt = self.runtime.service.receipt(intent_id)
        if receipt is None:
            times = {vid: frozen['slots'][index] for index, vid in successes} if frozen['mode'] == 'platform_schedule' else None
            prepared = self.runtime.service.prepare(request_id=intent_id, video_ids=[vid for _, vid in successes],
                targets=[{'account_id': t['account_id'], 'platform': t['platform'], 'fields': _frozen_account_fields(frozen,t)} for t in frozen['targets']],
                common_fields={}, fields_by_video={vid: {k: task.snapshot['copies'][index].get(k, '') for k in ('title', 'body', 'tags')}
                                                  for index, vid in successes},
                execution_mode=frozen['mode'], scheduled_times=times, allow_new_request=True,inherit_account_settings=False)
            self._assert_automatic(self.engine, task_id, frozen)
            receipt = self.runtime.service.save(prepared['prepared_id'], request_id=intent_id, consent_partial=True)
        pid = receipt['id']
        with self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(c) as s:
                    _put(s, SOURCE + pid, {'mix_task_id': task_id, 'host_session': self.session_id})
                    previous = _json(s, STATUS + task_id, {})
                    _put(s, STATUS + task_id, {**previous, 'publish_task_ids': [pid],
                        'excluded_count': receipt.get('excluded_count', 0),
                        'excluded_targets': [{k: e[k] for k in ('video_id', 'account_id', 'platform', 'reason', 'states') if k in e}
                                             for e in receipt.get('excluded_targets', [])]})
                c.commit()
            except BaseException:
                c.rollback()
                raise
        # Re-entry uses a stable native execution receipt. The normal V2 check,
        # exact-attempt authorization, and receipt share its existing transaction.
        rid = 'daily-start-' + task_id
        with self.commands.lock:
            with self.engine.connect() as c:
                c.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    previous = self.commands._receipt(c, rid)
                    if previous is None:
                        self._assert_automatic(c, task_id, frozen)
                        self._assert_identity(c, frozen)
                        if self.auth is not None:
                            with session_scope(c) as s:
                                proof = _json(s, STATUS + task_id, {})
                            if (proof.get('auth_phase') or proof.get('auth_verified') != 'before_publish'
                                    or proof.get('auth_host') != self.session_id
                                    or proof.get('auth_targets') != frozen['targets']):
                                raise DailyMixingError('daily_login_checking')
                        service = HostMultiPublishService(c, self.paths, catalog=self.runtime.catalog, workers=self.runtime.registry)
                        detail = service.get_task(pid)
                        ready = [d['id'] for d in detail['deliveries'] if d['can_submit']]
                        if not ready:
                            reason = next((d['readiness_reason'] for d in detail['deliveries'] if d['readiness_reason']), 'daily_handoff_failed')
                            raise DailyMixingError(reason)
                        check = self.commands.check({'id': pid, 'delivery_ids': ready}, service=service)
                        params = {'id': pid, 'delivery_ids': ready, 'digest': check['digest'], 'consent': True, 'consent_partial': True}
                        data = self.commands.apply(c, 'execution.start', params)
                        result = {'v': 1, 'request_id': rid, 'ok': True, 'data': data}
                        request_hash = digest({'operation': 'multi_publish.execution.start', 'params': params})
                        c.execute(text('INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES (:id,:hash,:response)'),
                                  {'id': rid, 'hash': request_hash, 'response': canonical(result)})
                    with session_scope(c) as s:
                        _put(s, STATUS + task_id, {**_json(s, STATUS + task_id, {}), 'handoff_authorized': True,
                                                  'reason_code': '', 'reason': ''})
                    c.commit()
                except BaseException:
                    c.rollback()
                    raise
        if not self.runtime.closed.is_set():
            self.runtime.wake()
        return self.status(task_id)
