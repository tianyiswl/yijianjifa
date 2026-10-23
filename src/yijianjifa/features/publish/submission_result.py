"""Persist trusted adapter evidence for a claimed single-item submission.

No enablement or browser entry point. The final-action marker is never removed;
an ambiguous result requires reconciliation, not another submission.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import secrets

from sqlalchemy import select

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow, TaskIssueRow
from yijianjifa.features.notifications.service import NotificationRow
from .normal_service import PublishTaskRow
from .history import PublishHistoryRow
from .executor import PublishExecutor, SubmissionEvidence
from .submission_journal import SubmissionJournal


class SubmissionResultStore:
    def __init__(self, engine):
        self.engine = engine

    def record(self, task_id, item_id, *, owner_token, expected_digest, evidence):
        if not isinstance(evidence, SubmissionEvidence):
            raise ValueError('trusted submission evidence required')
        if not isinstance(owner_token, str) or len(owner_token) != 64:
            raise ValueError('submission owner required')
        key = SubmissionJournal._key(json.dumps([task_id, item_id]))
        result_key = key.replace('publish.final.', 'publish.result.', 1)
        payload = asdict(evidence)
        with self.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(connection) as session:
                    claim = session.get(Setting, key.replace('publish.final.', 'publish.claim.', 1))
                    if claim is None:
                        raise ValueError('submission not claimed')
                    claim_data = json.loads(claim.value_json)
                    if not secrets.compare_digest(claim_data['owner_hash'], hashlib.sha256(owner_token.encode()).hexdigest()):
                        raise ValueError('submission belongs to another worker')
                    task = session.get(PublishTaskRow, task_id)
                    center = session.get(TaskCenterRow, task_id)
                    item = session.get(TaskItemRow, item_id)
                    if task is None or center is None or item is None or item.task_id != task_id:
                        raise ValueError('submission unavailable')
                    if center.kind != 'normal_publish' or task.snapshot_json != center.snapshot_json:
                        raise ValueError('submission snapshot mismatch')
                    digest = hashlib.sha256(json.dumps([task.snapshot_json, item.detail_json], ensure_ascii=False).encode()).hexdigest()
                    if digest != expected_digest or claim_data['request_digest'] != digest:
                        raise ValueError('submission changed')
                    snapshot, detail = json.loads(task.snapshot_json), json.loads(item.detail_json)
                    items = list(session.scalars(select(TaskItemRow).where(TaskItemRow.task_id == task_id)))
                    if snapshot.get('execution_enabled') is not True or snapshot.get('kind') != 'normal_publish':
                        raise ValueError('execution disabled')
                    if len(items) != len(snapshot.get('items',[])) or detail not in snapshot['items']:
                        raise ValueError('valid item execution required')
                    previous = session.get(Setting, result_key)
                    if previous is not None:
                        saved = json.loads(previous.value_json)
                        if saved['evidence'] != payload or saved['request_digest'] != digest:
                            raise ValueError('conflicting result requires reconciliation')
                        return saved['state']
                    marker = session.get(Setting, key)
                    if marker is not None:
                        if json.loads(marker.value_json)['request_digest'] != digest:
                            raise ValueError('final marker mismatch')
                        # A persisted final boundary always wins over a late
                        # adapter claim that nothing was submitted.
                        state = PublishExecutor().classify(evidence) if evidence.final_action_triggered else 'result_unknown'
                        expected_state = 'result_unknown'
                    else:
                        if evidence.final_action_triggered:
                            raise ValueError('final action has no durable marker')
                        state = PublishExecutor().classify(evidence)
                        expected_state = 'running'
                    if state == 'platform_scheduled' and (
                        detail.get('execution_mode') != 'platform_schedule' or
                        evidence.platform_schedule_confirmation != detail.get('time')
                    ):
                        state = 'result_unknown'
                    videos = [v for v in snapshot['videos'] if v['id'] == detail['video_id']]
                    if len(videos) != 1:
                        raise ValueError('submission video unavailable')
                    histories = list(session.scalars(select(PublishHistoryRow).where(
                        PublishHistoryRow.task_id == task_id,
                        PublishHistoryRow.account_id == detail['account_id'],
                        PublishHistoryRow.video_hash == videos[0]['content_hash'])))
                    allowed_history = {'queued', 'running'} if marker is None else {'result_unknown'}
                    if len(histories) != 1 or histories[0].state not in allowed_history:
                        raise ValueError('submission history changed')
                    if any(row.state != expected_state for row in (task, center, item)):
                        raise ValueError('submission state changed')
                    item.state = histories[0].state = state
                    task.state = center.state = ('running' if state=='platform_scheduled' and
                        any(i.id!=item.id and i.state=='running' for i in items) else state)
                    session.add(Setting(key=result_key, value_json=json.dumps(dict(
                        state=state, request_digest=digest, evidence=payload))))
                    messages = {
                        'published': '已核实此条视频发布成功。',
                        'platform_scheduled': '平台已接收定时安排；审核及到点发布状态请以平台为准。',
                        'result_unknown': '发布结果待核实，请检查账号后台；不会自动重发。',
                        'paused_auth': '登录状态需要处理，此条视频尚未提交。',
                        'pre_submit_failed': '提交前检查或页面准备失败，此条视频尚未提交。',
                        'platform_rejected': '平台未接受此条发布，请查看处理信息。',
                        'not_submitted': '此条视频尚未提交。',
                    }
                    reasons={'submission_location_unavailable':'未找到名称和地址完全匹配的发布地点，或出现重复候选，请刷新地点后重新选择。','submission_location_mismatch':'页面实际选择的地点与任务安排不一致，已停止提交，请重新选择地点。','submission_verification_cancelled':'账号验证已取消或超时，请在任务记录中重新处理。','submission_verification_pending':'提交后出现验证，结果尚待核对，不会重复提交。','submission_account_busy':'该账号正在登录或执行其他发布，请等待完成。','submission_timeout':'等待发布页面响应超过5分钟。可能停在账号读取、上传、话题选择或结果回读；请检查发布窗口状态和网络。','submission_topic_unavailable':'抖音话题候选未能匹配；已重新搜索3次仍未确认，或编辑光标无法安全恢复。请检查或修改话题，必要时选择显示浏览器后重试。此条尚未提交。','submission_identity_unavailable':'账号身份尚未读取完整，无法核对所选账号。','submission_account_mismatch':'浏览器中的账号与本次选择的发布账号不一致。','submission_topic_mismatch':'抖音页面未正确识别或回读本次视频话题。','submission_form_mismatch':'抖音页面回读的标题、正文、声明或定时设置与任务不一致。','submission_existing_work':'后台存在相同标题、文案和定时时刻的作品，需要核对是否重复安排。','submission_schedule_invalid':'定时时间已不在当前时间2.5小时后至13天后的范围内。'}
                    if state in {'pre_submit_failed','result_unknown'} and payload.get('error_code') in reasons:
                        messages[state]=reasons[payload['error_code']]+(' 此条视频尚未提交。' if state=='pre_submit_failed' else ' 请核对后台结果，不能重复提交。')
                    session.add(NotificationRow(task_id=task_id,
                        status=state if state in {'published', 'platform_scheduled', 'result_unknown'} else 'action_required',
                        message=messages[state], deep_link='task:' + task_id))
                    if state not in {'published', 'platform_scheduled'}:
                        session.add(TaskIssueRow(task_id=task_id,
                            title='发布需要处理', object_name=detail.get('title') or '本条视频',
                            stage='提交结果核验' if marker is not None else '提交前检查',
                            reason=messages[state], impact='任务暂停，不自动重发。',
                            next_action='核对账号后台中的视频和发布时间，再处理任务。' if marker is not None else '检查登录状态和发布资料后再处理任务。',
                            occurred_at=datetime.now(timezone.utc).isoformat(),
                            error_code=evidence.error_code or state,
                            evidence_json=json.dumps(dict(state=state, request_digest=digest))))
                connection.commit()
                return state
            except BaseException:
                connection.rollback()
                raise
