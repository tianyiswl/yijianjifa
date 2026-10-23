"""V2 target authority and durable per-attempt submission boundary.

V1 tables and markers are deliberately never rewritten by this module.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import secrets
import threading
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.storage.models import Base
from yijianjifa.storage.db import session_scope
from yijianjifa.features.tasks.service import TaskItemRow, TaskCenterRow
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow, PublishHistoryRepository


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@contextmanager
def write_session(engine):
    from sqlalchemy.engine import Connection
    if isinstance(engine, Connection):
        # Native command lane owns the enclosing atomic receipt transaction.
        with session_scope(engine) as session:
            yield session
        return
    with engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            with session_scope(connection) as session:
                yield session
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


class PublishDeliveryRow(Base):
    __tablename__ = 'publish_deliveries'
    __table_args__ = (UniqueConstraint('item_id'), UniqueConstraint('history_id'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('publish_tasks.id'), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey('task_center_items.id'), nullable=False)
    history_id: Mapped[str] = mapped_column(ForeignKey('publish_history.id'), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    platform_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    video_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    video_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False, default='video', server_default='video')
    content_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
        default=lambda context: context.get_current_parameters().get('video_id'))
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True,
        default=lambda context: context.get_current_parameters().get('video_hash'))
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    scheduled_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DeliveryAttemptRow(Base):
    __tablename__ = 'publish_delivery_attempts'
    __table_args__ = (UniqueConstraint('delivery_id', 'number'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(ForeignKey('publish_deliveries.id'), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class PublishResourceRow(Base):
    __tablename__ = 'publish_v2_resources'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('publish_tasks.id'), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cleaned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ResourceConsumerRow(Base):
    __tablename__ = 'publish_v2_resource_consumers'
    __table_args__ = (UniqueConstraint('resource_id', 'delivery_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey('publish_v2_resources.id'), nullable=False)
    delivery_id: Mapped[str] = mapped_column(ForeignKey('publish_deliveries.id'), nullable=False)
    released: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PublishBatchReceiptRow(Base):
    __tablename__ = 'publish_v2_receipts'
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey('publish_tasks.id'), nullable=False, unique=True)
    receipt_json: Mapped[str] = mapped_column(Text, nullable=False)


class LocalWorkerRegistry:
    """Host-owned proof, never an API boolean. Unknown process owners stay blocked.

    A host with durable process-supervision evidence may inject an implementation
    with this interface. The default only proves workers stopped in this process.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._owners = {}

    def started(self, owner_hash):
        with self._lock:
            if owner_hash in self._owners:
                raise ValueError('worker already registered')
            self._owners[owner_hash] = False

    def finished(self, owner_hash):
        with self._lock:
            if owner_hash not in self._owners:
                raise ValueError('unknown worker')
            self._owners[owner_hash] = True

    def resumed(self, owner_hash):
        with self._lock:
            if self._owners.get(owner_hash) is not True:
                raise ValueError('worker has not stopped')
            self._owners[owner_hash] = False

    def stopped(self, owner_hash):
        with self._lock:
            return self._owners.get(owner_hash) is True


TERMINAL = {'published', 'platform_scheduled', 'platform_rejected', 'cancelled'}


class PublishDeliveries:
    def __init__(self, engine, *, workers=None):
        self.engine = engine
        self.workers = workers or LocalWorkerRegistry()
        self.claim_guard = None
        self.final_guard = None

    @staticmethod
    def load(session, delivery_id):
        row = session.get(PublishDeliveryRow, delivery_id)
        if row is None:
            raise ValueError('delivery unavailable')
        item = session.get(TaskItemRow, row.item_id)
        history = session.get(PublishHistoryRow, row.history_id)
        attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id, DeliveryAttemptRow.number==row.attempt))
        task = session.get(PublishTaskRow, row.task_id)
        center = session.get(TaskCenterRow, row.task_id)
        if (item is None or history is None or attempt is None or task is None or center is None or
            item.task_id != row.task_id or history.task_id != row.task_id or
            history.account_id != row.account_id or history.video_hash != row.video_hash or history.content_type != row.content_type or
            center.kind != 'multi_publish' or json.loads(task.snapshot_json).get('version') != 2 or
            task.snapshot_json != center.snapshot_json):
            raise ValueError('delivery authority mismatch')
        batch=json.loads(task.snapshot_json)
        if batch.get('excluded_count',0) and batch.get('consent_partial') is not True:
            raise ValueError('partial delivery execution lacks consent')
        payload = json.loads(row.snapshot_json)
        if row.content_type == 'video':
            content_valid = (payload.get('content_type','video') == 'video' and
                payload.get('video_id') == row.video_id and payload.get('video_hash') == row.video_hash and
                row.content_id == row.video_id and row.content_hash == row.video_hash)
            detail = {'delivery_id':row.id,'video_id':row.video_id,'account_id':row.account_id,'platform':row.platform}
        elif row.content_type == 'image_post':
            content_valid = (payload.get('content_type') == 'image_post' and payload.get('content_id') == row.content_id and
                payload.get('content_hash') == row.content_hash and history.content_id == row.content_id and
                history.content_hash == row.content_hash and row.video_id is None and row.video_hash is None and
                not {'video_id','video_hash','video_path'} & set(payload))
            images = payload.get('images')
            if (type(images) is not list or not 1 <= len(images) <= 100 or
                    any(type(image) is not dict for image in images) or
                    [image.get('position') for image in images] != list(range(len(images)))):
                raise ValueError('image delivery order changed')
            refs = list(session.scalars(select(ResourceConsumerRow).where(ResourceConsumerRow.delivery_id==row.id)))
            image_ids = [image.get('resource_id') for image in images]
            if len(set(image_ids)) != len(images) or set(image_ids) != {ref.resource_id for ref in refs}:
                raise ValueError('image delivery resources changed')
            for image in images:
                resource = session.get(PublishResourceRow,image['resource_id'])
                if resource is None or resource.task_id != row.task_id or (resource.path,resource.content_hash) != (image.get('path'),image.get('content_hash')):
                    raise ValueError('image delivery resource authority mismatch')
            detail = {'delivery_id':row.id,'content_type':row.content_type,'content_id':row.content_id,
                'account_id':row.account_id,'platform':row.platform}
        else:
            raise ValueError('unsupported delivery content type')
        if (digest(payload) != row.request_digest or attempt.request_digest != row.request_digest or
            attempt.snapshot_json != row.snapshot_json or payload['platform'] != row.platform or
            payload['account_id'] != row.account_id or payload['platform_user_id'] != row.platform_user_id or
            not content_valid or
            payload['execution_mode'] != row.execution_mode or payload['scheduled_at'] != row.scheduled_at or
            item.detail_json != canonical(detail) or
            history.state != item.state):
            raise ValueError('delivery snapshot or history changed')
        for journal in (attempt.final_json,attempt.result_json):
            if journal is not None and json.loads(journal).get('request_digest')!=row.request_digest:
                raise ValueError('delivery journal digest mismatch')
        return row, item, history, attempt, task, center, payload

    @staticmethod
    def _owner(attempt, token, expected_digest):
        if (type(token) is not str or len(token)!=64 or attempt.owner_hash is None or
            not secrets.compare_digest(attempt.owner_hash, hashlib.sha256(token.encode()).hexdigest()) or
            attempt.request_digest != expected_digest):
            raise ValueError('delivery owner or digest mismatch')

    @staticmethod
    def aggregate(session, task_id):
        center=session.get(TaskCenterRow,task_id)
        task=session.get(PublishTaskRow,task_id)
        items=list(session.scalars(select(TaskItemRow).where(TaskItemRow.task_id==task_id)))
        states=[i.state for i in items]
        if center.state=='paused':
            return
        if states and all(s in TERMINAL for s in states):
            state='completed' if all(s in {'published','platform_scheduled'} for s in states) else 'completed_with_errors'
        elif (json.loads(center.snapshot_json).get('content_type')=='image_post' and states and
                all(s in {'published','platform_scheduled','platform_accepted','processing','reviewing'} for s in states)):
            # Completing this local image delivery only confirms platform
            # acceptance. Public visibility remains each child's actual state.
            state='completed'
        elif 'running' in states:
            state='running'
        elif any(s=='waiting' for s in states):
            state='waiting'
        else:
            state='action_required'
        center.state=task.state=state

    def claim(self, delivery_id):
        token=secrets.token_hex(32)
        with write_session(self.engine) as s:
            row,item,history,attempt,task,center,payload=self.load(s,delivery_id)
            if item.state!='waiting' or center.state=='paused' or row.cancel_requested or attempt.owner_hash or attempt.final_json or attempt.result_json:
                raise ValueError('delivery requires reconciliation or is not ready')
            # Hosts can bind execution permission to the exact current attempt.
            # The check shares this write transaction with the claim: a worker
            # may still hold ids collected before a concurrent re-preparation.
            if self.claim_guard is not None:self.claim_guard(s,row)
            from yijianjifa.features.accounts.service import AccountRow
            account=s.get(AccountRow,row.account_id)
            if account is None or account.login_state=='removed' or (account.platform,account.platform_user_id)!=(row.platform,row.platform_user_id):
                raise ValueError('delivery account identity changed')
            for other in s.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.platform==row.platform,PublishDeliveryRow.platform_user_id==row.platform_user_id,PublishDeliveryRow.id!=row.id)):
                other_item=s.get(TaskItemRow,other.item_id)
                if other_item.state in ({'running','paused_auth'} if json.loads(row.snapshot_json).get('allow_new_request') is True else {'running','paused_auth','result_unknown','interrupted'}):
                    raise ValueError('delivery account is parked or unresolved')
            PublishHistoryRepository.assert_no_other_content_history(s,row.content_type,row.content_hash,row.account_id,
                history_id=row.history_id,identity=(row.platform,row.platform_user_id),allow_new_request=json.loads(row.snapshot_json).get('allow_new_request') is True)
            if row.execution_mode=='software_schedule':
                time=datetime.fromisoformat(row.scheduled_at)
                now=datetime.now(timezone.utc)
                if time>now:raise ValueError('software schedule is not due')
                if (now-time).total_seconds()>60:raise ValueError('software schedule missed')
            from pathlib import Path
            from yijianjifa.features.library.media_identity import sha256_file
            for ref in s.scalars(select(ResourceConsumerRow).where(ResourceConsumerRow.delivery_id==row.id)):
                resource=s.get(PublishResourceRow,ref.resource_id)
                if ref.released or resource.cleaned or not Path(resource.path).is_file() or sha256_file(Path(resource.path))!=resource.content_hash:
                    raise ValueError('frozen delivery resource changed')
            attempt.owner_hash=hashlib.sha256(token.encode()).hexdigest()
            item.state=history.state='running'
            self.aggregate(s,row.task_id)
            owner_hash=attempt.owner_hash
        self.workers.started(owner_hash)
        return {'owner_token':token,'owner_hash':owner_hash,'digest':row.request_digest,'payload':payload,'task_id':row.task_id,'delivery_id':row.id,'attempt':row.attempt}

    def defer_account_busy(self,delivery_id,expected_digest,*,owner_token):
        """Release only a live pre-browser claim; a final marker never returns to queue."""
        with write_session(self.engine) as s:
            row,item,history,attempt,task,center,payload=self.load(s,delivery_id)
            self._owner(attempt,owner_token,expected_digest)
            if attempt.final_json or attempt.result_json or row.cancel_requested or item.state!='running':return False
            attempt.owner_hash=None
            item.state=history.state='waiting'
            self.aggregate(s,row.task_id)
        return True

    def mark_before_final(self, delivery_id, expected_digest, *, owner_token):
        with write_session(self.engine) as s:
            row,item,history,attempt,task,center,payload=self.load(s,delivery_id)
            self._owner(attempt,owner_token,expected_digest)
            if center.state=='paused' or row.cancel_requested or item.state not in {'running','paused_auth'} or attempt.final_json is not None:
                raise ValueError('delivery final action stopped or already marked')
            from yijianjifa.features.accounts.service import AccountRow
            account=s.get(AccountRow,row.account_id)
            if account is None or account.login_state=='removed' or (account.platform,account.platform_user_id)!=(row.platform,row.platform_user_id):
                raise ValueError('delivery account identity changed')
            PublishHistoryRepository.assert_no_other_content_history(s,row.content_type,row.content_hash,row.account_id,
                history_id=row.history_id,identity=(row.platform,row.platform_user_id),allow_new_request=json.loads(row.snapshot_json).get('allow_new_request') is True)
            if row.execution_mode=='software_schedule':
                scheduled=datetime.fromisoformat(row.scheduled_at)
                if not 0<=(datetime.now(timezone.utc)-scheduled).total_seconds()<=60:
                    raise ValueError('software schedule no longer due')
            from pathlib import Path
            from yijianjifa.features.library.media_identity import sha256_file
            for ref in s.scalars(select(ResourceConsumerRow).where(ResourceConsumerRow.delivery_id==row.id)):
                resource=s.get(PublishResourceRow,ref.resource_id)
                if ref.released or resource.cleaned or not Path(resource.path).is_file() or sha256_file(Path(resource.path))!=resource.content_hash:
                    raise ValueError('frozen delivery resource changed')
            if self.final_guard is not None:self.final_guard(s,row)
            attempt.final_json=canonical({'request_digest':expected_digest,'state':'result_unknown','at':datetime.now(timezone.utc).isoformat()})
            item.state=history.state='result_unknown'
            self.aggregate(s,row.task_id)

    def record(self, delivery_id, expected_digest, *, owner_token, outcome, reconcile=False):
        from .coordinator import AdapterOutcome, outcome_evidence
        if not isinstance(outcome,AdapterOutcome):raise ValueError('trusted adapter outcome required')
        formal={'platform_accepted','processing','reviewing'}
        submitted={'published','platform_scheduled','platform_rejected','result_unknown'}|formal
        allowed=submitted|{'pre_submit_failed','paused_auth'}
        if outcome.state not in allowed:raise ValueError('unsupported adapter outcome')
        try:evidence=outcome_evidence(outcome,expected_digest)
        except ValueError:
            if outcome.state not in submitted:raise
            evidence=outcome_evidence(AdapterOutcome('result_unknown',error_code='result_unknown'),expected_digest)
            evidence.update(reported_state=outcome.state,protocol_violation='invalid_submitted_receipt')
        with write_session(self.engine) as s:
            row,item,history,attempt,task,center,payload=self.load(s,delivery_id)
            self._owner(attempt,owner_token,expected_digest)
            final=attempt.final_json is not None
            state='result_unknown' if evidence.get('protocol_violation') else outcome.state
            if outcome.state in submitted and not final:
                evidence['reported_state']=outcome.state
                evidence['protocol_violation']='receipt_without_final_marker'
                state='result_unknown'
            if state in formal and (not final or not evidence['receipt_reference']):
                state='result_unknown'
                if not final:evidence['protocol_violation']='receipt_without_final_marker'
            if final and state in {'pre_submit_failed','paused_auth'}:
                state='result_unknown'
            if state=='platform_scheduled' and (row.execution_mode!='platform_schedule' or outcome.scheduled_at!=row.scheduled_at):
                state='result_unknown'
            if attempt.result_json and not reconcile:
                previous=json.loads(attempt.result_json)
                evidence['state']=state
                if previous == evidence and not attempt.context_json:
                    return state
                if not (previous['state']=='paused_auth' and attempt.context_json):
                    raise ValueError('conflicting result requires reconciliation')
            if reconcile and (not final or item.state not in {'result_unknown','platform_scheduled','platform_accepted','processing','reviewing'}):
                raise ValueError('delivery does not need result reconciliation')
            evidence['state']=state
            # No raw platform response/error is stored or exposed. Context is
            # private host data bounded to JSON and never part of the public DTO.
            if outcome.context is not None:
                import re
                if (type(outcome.context) is not dict or set(outcome.context)-{'handle','session_handle','resource_id','verification_handle'} or
                    any(type(v) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}',v) for v in outcome.context.values()) or len(canonical(outcome.context))>2048):
                    if outcome.state not in submitted:raise ValueError('invalid parked adapter context')
                    state='result_unknown'
                    evidence.update(state=state,reported_state=outcome.state,protocol_violation='invalid_submitted_context')
                    attempt.context_json=None
                else:attempt.context_json=canonical(outcome.context)
            else:
                attempt.context_json=None
            if reconcile:
                previous=json.loads(attempt.result_json) if attempt.result_json else None
                attempt.recovery_json=canonical({'previous_result':previous,'verified_result':evidence})
            attempt.result_json=canonical(evidence)
            item.state=history.state=state
            self.aggregate(s,row.task_id)
        return state

    def recover_after_restart(self):
        ids=[]
        with write_session(self.engine) as s:
            for row in s.scalars(select(PublishDeliveryRow)):
                row,item,history,attempt,*_=self.load(s,row.id)
                if attempt.owner_hash and item.state in {'running','paused_auth'}:
                    item.state=history.state='result_unknown' if attempt.final_json else 'interrupted'
                    ids.append(row.id)
                    self.aggregate(s,row.task_id)
        return ids
