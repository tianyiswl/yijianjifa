"""Trusted, offline-tested task boundary; no transport or enablement entry point.

The future coordinator claims before page preparation, then supplies the returned
frozen digest and owner token to mark_before_final as the final-action callback.
"""
import hashlib
import json
import secrets
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.commerce.service import CommerceTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow, PublishHistoryRepository
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.features.publish.submission_journal import SubmissionJournal


class TaskSubmissionBoundary:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def _load(session, task_id, item_id):
        center = session.get(TaskCenterRow, task_id)
        item = session.get(TaskItemRow, item_id)
        if center is None or item is None or item.task_id != task_id:
            raise ValueError('submission task item unavailable')
        models = {'normal_publish': PublishTaskRow, 'commerce_publish': CommerceTaskRow}
        model = models.get(center.kind)
        task = session.get(model, task_id) if model else None
        if task is None or task.snapshot_json != center.snapshot_json:
            raise ValueError('submission snapshot mismatch')
        snapshot = json.loads(task.snapshot_json)
        if snapshot.get('kind') != center.kind or snapshot.get('execution_enabled') is not True:
            raise ValueError('platform execution disabled')
        if task.state != 'running' or center.state != 'running' or item.state != 'running':
            raise ValueError('submission not ready or requires reconciliation')
        detail = json.loads(item.detail_json)
        if detail not in snapshot.get('items', []):
            raise ValueError('submission item differs from frozen snapshot')
        videos = [v for v in snapshot['videos'] if v['id'] == detail['video_id']]
        if len(videos) != 1:
            raise ValueError('submission video unavailable')
        histories = list(session.scalars(select(PublishHistoryRow).where(
            PublishHistoryRow.task_id == task_id,
            PublishHistoryRow.account_id == detail['account_id'],
            PublishHistoryRow.video_hash == videos[0]['content_hash'])))
        if len(histories) != 1 or histories[0].state not in {'queued', 'running'}:
            raise ValueError('submission history unavailable or ambiguous')
        PublishHistoryRepository.assert_no_other_target_history(session, videos[0]['content_hash'],
            detail['account_id'], history_id=histories[0].id,
            allow_repeat_success=detail.get('repeat_confirmed') is True, legacy=True,allow_new_request=detail.get('allow_new_request') is True)
        key = SubmissionJournal._key(json.dumps([task_id, item_id]))
        if session.get(Setting, key) is not None:
            raise ValueError('submission requires reconciliation')
        digest = hashlib.sha256(json.dumps([task.snapshot_json, item.detail_json],
                                          ensure_ascii=False).encode()).hexdigest()
        return task, center, item, histories[0], key, digest

    def check_ready(self, task_id, item_id):
        with session_scope(self.engine) as session:
            return self._load(session, task_id, item_id)[-1]

    def claim(self, task_id, item_id):
        """Acquire once before any page preparation. Never expires automatically.

        A crashed owner requires explicit reconciliation; wall-clock expiry is
        insufficient proof that its browser/upload has stopped.
        """
        token = secrets.token_hex(32)
        with self.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(connection) as session:
                    *_, key, digest = self._load(session, task_id, item_id)
                    claim_key = key.replace('publish.final.', 'publish.claim.', 1)
                    if session.get(Setting, claim_key) is not None:
                        raise ValueError('submission preparation already owned')
                    session.add(Setting(key=claim_key, value_json=json.dumps(dict(
                        owner_hash=hashlib.sha256(token.encode()).hexdigest(), request_digest=digest))))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return dict(owner_token=token, digest=digest)

    def mark_before_final(self, task_id, item_id, expected_digest, *, owner_token=None):
        with self.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with session_scope(connection) as session:
                    task, center, item, history, key, digest = self._load(session, task_id, item_id)
                    claim = session.get(Setting, key.replace('publish.final.', 'publish.claim.', 1))
                    if claim is None or not isinstance(owner_token, str) or len(owner_token) != 64:
                        raise ValueError('submission preparation owner required')
                    claim_data = json.loads(claim.value_json)
                    if not secrets.compare_digest(claim_data['owner_hash'], hashlib.sha256(owner_token.encode()).hexdigest()):
                        raise ValueError('submission preparation belongs to another worker')
                    if claim_data['request_digest'] != digest:
                        raise ValueError('claimed submission changed')
                    if digest != expected_digest:
                        raise ValueError('submission changed after preparation')
                    session.add(Setting(key=key, value_json=json.dumps(dict(
                        state='result_unknown', request_digest=digest))))
                    task.state = center.state = item.state = history.state = 'result_unknown'
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
