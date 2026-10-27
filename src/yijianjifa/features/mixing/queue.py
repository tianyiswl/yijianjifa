from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import subprocess
from typing import Any, Mapping, Protocol
import uuid

from sqlalchemy import Boolean, DateTime, Integer, String, Text, select, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.core.snapshots import FrozenSnapshot
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base, Setting
from .diagnostics import error_diagnostic
from .tts.hosted import VoicePaused


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MixTask(Base):
    __tablename__ = "mix_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting", index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    current_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pause_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_resume: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class ItemCancelled(Exception):
    """The active item was discarded before registration."""


class ItemInterrupted(Exception):
    """Keep a durable render pending until the user explicitly resumes it."""


class MixItemExecutor(Protocol):
    def render_item(self, task_id: str, item_index: int, snapshot: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class MixTaskRecord:
    id: str
    state: str
    quantity: int
    current_index: int
    completed_count: int
    success_count: int
    failure_count: int
    pause_requested: bool
    auto_resume: bool
    position: int
    snapshot: dict[str, Any]
    last_error: str | None
    created_at: str | None = None

    @property
    def planned_publish_times(self) -> list[Any]:
        value = self.snapshot.get("planned_publish_times", [])
        return list(value) if isinstance(value, list) else []


class MixQueue:
    def __init__(self, engine: Engine, executor: MixItemExecutor, global_settings=None):
        self.engine = engine
        self.executor = executor
        self.global_settings = global_settings
        self._active_ids = set()
        self.on_settled = None
        self._serial_claim_busy = False

    def confirm_batch(self, snapshot: Mapping[str, Any]) -> MixTaskRecord:
        data = dict(snapshot)
        if self.global_settings is not None:
            data["output_directory"] = str(self.global_settings.output_directory)
        return self.enqueue(data)

    def enqueue(self, snapshot: Mapping[str, Any]) -> MixTaskRecord:
        frozen = FrozenSnapshot.from_mapping(snapshot)
        data = frozen.data
        quantity = int(data.get("quantity", 0))
        if quantity < 1:
            raise ValueError("mix task quantity must be >= 1")
        with session_scope(self.engine) as session:
            max_position = session.scalar(select(func.max(MixTask.position)))
            row = MixTask(
                snapshot_json=frozen.json,
                state="waiting",
                quantity=quantity,
                position=(max_position or 0) + 1,
                auto_resume=False,
            )
            session.add(row)
            session.flush()
            return self._to_record(row)

    def get(self, task_id: str) -> MixTaskRecord:
        with session_scope(self.engine) as session:
            row = session.get(MixTask, task_id)
            if row is None:
                raise KeyError(task_id)
            return self._to_record(row)

    def request_pause(self, task_id: str) -> None:
        with session_scope(self.engine) as session:
            row = session.get(MixTask, task_id)
            if row is None:
                raise KeyError(task_id)
            if row.state in {"waiting", "running", 'waiting_publish'}:
                row.pause_requested = True
                if row.state in {"waiting", 'waiting_publish'}:
                    row.state = "paused"
                    row.pause_requested = False

    def resume(self, task_id: str) -> None:
        with session_scope(self.engine) as session:
            row = session.get(MixTask, task_id)
            if row is None:
                raise KeyError(task_id)
            if row.state not in {"paused", "interrupted"}:
                raise ValueError(f"task cannot resume from {row.state}")
            row.state = "waiting"
            row.pause_requested = False
            row.auto_resume = False
            self.authorize_voice_retry(session, task_id)
            self.clear_voice_pause(session, task_id)

    @staticmethod
    def clear_voice_pause(session, task_id):
        saved = session.get(Setting, 'mix-voice-pause:' + task_id)
        if saved is not None:
            row = session.get(MixTask, task_id)
            try:
                if row and row.last_error == json.loads(saved.value_json).get('message'):
                    row.last_error = None
            except (ValueError, AttributeError):
                pass
            session.delete(saved)

    @staticmethod
    def authorize_voice_retry(session, task_id):
        """Only native user resume commands call this; background ticks cannot."""
        pause = session.get(Setting, 'mix-voice-pause:' + task_id)
        if pause is None:
            return
        try:
            code = json.loads(pause.value_json).get('code')
        except (ValueError, AttributeError):
            return
        if code not in {'job_unknown', 'voice_generation_failed', 'retry_not_ready'}:
            return
        key = 'mix-voice-retry:' + task_id
        value = json.dumps({'authorization': uuid.uuid4().hex})
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value_json=value))
        else:
            row.value_json = value

    def cancel_remaining(self, task_id: str) -> None:
        with session_scope(self.engine) as session:
            row = session.get(MixTask, task_id)
            if row is None:
                raise KeyError(task_id)
            if row.state not in {"completed", "completed_with_errors", "cancelled"}:
                row.state = "cancelling" if task_id in self._active_ids else "cancelled"
                row.pause_requested = False
                row.auto_resume = False
        # A connection-bound queue belongs to an outer command/receipt transaction;
        # its owner must request cleanup only after that transaction commits.
        if (isinstance(self.engine, Engine) and task_id not in self._active_ids
                and hasattr(self.executor, 'discard_cancelled')):
            self.executor.discard_cancelled(task_id)

    def move(self, task_id: str, delta: int) -> None:
        if delta == 0:
            return
        with session_scope(self.engine) as session:
            row = session.get(MixTask, task_id)
            if row is None:
                raise KeyError(task_id)
            if row.state != "waiting":
                raise ValueError("only waiting tasks can be reordered")
            rows = list(session.scalars(select(MixTask).where(MixTask.state == "waiting").order_by(MixTask.position)))
            index = next(i for i, item in enumerate(rows) if item.id == task_id)
            target = max(0, min(len(rows) - 1, index + delta))
            if target == index:
                return
            other = rows[target]
            row.position, other.position = other.position, row.position

    def recover_interrupted(self) -> list[str]:
        recovered: list[str] = []
        from yijianjifa.features.publish.deliveries import write_session
        with write_session(self.engine) as session:
            rows = list(session.scalars(select(MixTask).where(MixTask.state.in_(
                ["waiting", "running", "paused", "interrupted", "waiting_publish", "cancelling", "cancelled"]))))
            for row in rows:
                from .serial_gate import is_serial, has_flow_marker
                snapshot = json.loads(row.snapshot_json)
                serial = has_flow_marker(snapshot)
                if serial and not is_serial(snapshot):
                    row.state = 'cancelled' if row.state in {'cancelled', 'cancelling'} else 'paused'
                    row.auto_resume = row.pause_requested = False
                    continue
                if serial and row.state == 'running':
                    from .serial_claim import SerialMixClaims, claim_key
                    if not hasattr(self, 'serial_claims'): self.serial_claims = SerialMixClaims()
                    saved_claim = session.get(Setting, claim_key(row.id, row.current_index))
                    if saved_claim is not None and not self.serial_claims.owner_stopped(session, row.id, row.current_index):
                        continue
                    if saved_claim is not None:
                        self.serial_claims.recover(session, row.id, row.current_index, controlled_recovery=True)
                original_state = row.state
                stop = None
                if serial:
                    from .serial_gate import value
                    stop = value(session, 'mix.daily.status:' + row.id)
                self._reconcile_success_receipts(session, row)
                if row.state in {"cancelling", "cancelled"}:
                    row.state = "cancelled"
                elif serial:
                    from .serial_gate import can_claim_next
                    # 旧版可能把已全部收到明确回执的批次误留在重开提醒中。
                    # 只修正这种结束记账；用户主动暂停/取消仍优先，不再执行任何任务。
                    if (row.current_index >= row.quantity and can_claim_next(session, row)
                            and stop.get('reason_code') == 'daily_authorization_required'
                            and not row.pause_requested and not stop.get('automatic_stopped')):
                        row.state = 'completed'
                        self._notify_completed(session, row)
                    elif (original_state in {'paused', 'interrupted'} or row.pause_requested
                            or stop.get('automatic_stopped')):
                        row.state = 'cancelled' if stop.get('reason_code') == 'daily_batch_cancelled' else original_state if original_state in {'paused', 'interrupted'} else 'paused'
                    elif row.current_index and not can_claim_next(session, row):
                        row.state = 'waiting_publish'
                    elif row.current_index >= row.quantity:
                        row.state = 'completed'
                        self._notify_completed(session, row)
                    elif row.state == 'running':
                        row.state = 'interrupted'
                elif row.current_index >= row.quantity:
                    row.state = "completed_with_errors" if row.failure_count else "completed"
                    self._notify_completed(session, row)
                elif row.state == "running":
                    row.state = "interrupted"
                row.auto_resume = False
                row.pause_requested = False
                if row.state == "interrupted":
                    recovered.append(row.id)
        return recovered

    @staticmethod
    def _reconcile_success_receipts(session, row):
        """Replay only committed desktop successes, never invoke an executor."""
        from pathlib import Path
        from .output_names import output_filename
        from yijianjifa.features.finished.service import FinishedVideo
        snapshot = json.loads(row.snapshot_json)
        if snapshot.get('schema') != 'desktop-local-v1':
            return
        while row.current_index < row.quantity:
            index = row.current_index
            receipt = session.get(Setting, f'mix-success:{row.id}:{index}')
            if receipt is None:
                break
            try:
                value = json.loads(receipt.value_json)
                finished = session.get(FinishedVideo, value['finished_id'])
                expected = str(Path(snapshot['output_directory']) / row.id / output_filename(snapshot, index))
                if (finished is None or finished.origin_type != 'generated'
                        or finished.stored_path != expected or value.get('path') != expected):
                    break
            except (ValueError, TypeError, KeyError):
                break
            key = f'mix-result:{row.id}:{index}'
            result = json.dumps({'index': index, 'state': 'completed', 'error': None,
                                 'time': _utcnow().isoformat()}, ensure_ascii=False)
            existing = session.get(Setting, key)
            if existing is None:
                session.add(Setting(key=key, value_json=result))
            else:
                existing.value_json = result
            row.current_index += 1
            row.completed_count += 1
            row.success_count += 1

    def run_once_until_idle(self) -> list[str]:
        processed: list[str] = []
        while True:
            with session_scope(self.engine) as session:
                row = session.scalar(
                    select(MixTask)
                    .where(MixTask.state.in_(["waiting", "running"]))
                    .order_by(MixTask.position, MixTask.created_at)
                    .limit(1)
                )
                task_id = row.id if row else None
            if task_id is None:
                return processed
            if task_id not in processed:
                processed.append(task_id)
            while True:
                record = self.get(task_id)
                if record.state not in {"waiting", "running"}:
                    break
                self._run_single_item(task_id)
                if self._serial_claim_busy:
                    return processed
                if self.on_settled is not None:self.on_settled(task_id)

    def _run_single_item(self, task_id: str) -> None:
        self._run_single_item_inner(task_id)
        record = self.get(task_id)
        if (record.state in {'paused', 'interrupted', 'cancelled', 'completed', 'completed_with_errors'}
                and hasattr(self.executor, 'release_voice_reservation')):
            self.executor.release_voice_reservation(task_id, record.snapshot)

    def _run_single_item_inner(self, task_id: str) -> None:
        self._serial_claim_busy = False
        allowed=getattr(self,'can_start',None)
        if allowed and not allowed():
            with session_scope(self.engine) as session:
                row=session.get(MixTask,task_id)
                if row and row.state in {'waiting','running'}:row.state='paused'
            return
        from yijianjifa.features.publish.deliveries import write_session
        from .serial_gate import has_flow_marker, can_claim_next, render_authorized, value, STATUS
        claim = None
        with write_session(self.engine) as session:
            row = session.get(MixTask, task_id)
            if row is None:
                raise KeyError(task_id)
            if row.state not in {"waiting", "running"}:
                return
            snapshot = json.loads(row.snapshot_json)
            serial = has_flow_marker(snapshot)
            if serial and not render_authorized(session, row, getattr(self, 'serial_host_session', None)):
                row.state = 'paused'
                proof = value(session, STATUS + task_id)
                saved = session.get(Setting, STATUS + task_id)
                encoded = json.dumps({**proof, 'auth_phase': 'needs_attention', 'reason_code': 'daily_target_changed',
                                      'reason': '所选账号身份或会话已改变，请核对后继续；尚未开始下一条。'}, ensure_ascii=False)
                if saved is None: session.add(Setting(key=STATUS + task_id, value_json=encoded))
                else: saved.value_json = encoded
                return
            if serial and not can_claim_next(session, row):
                row.state = 'waiting_publish'
                return
            if row.current_index >= row.quantity:
                row.state = "completed_with_errors" if row.failure_count else "completed"
                self._notify_completed(session, row)
                return
            row.state = "running"
            item_index = row.current_index
            if serial:
                from .serial_claim import SerialMixClaims
                if not hasattr(self, 'serial_claims'): self.serial_claims = SerialMixClaims()
                claim = self.serial_claims.claim(session, task_id, item_index, snapshot)
                if claim is None:
                    self._serial_claim_busy = True
                    return

        error: str | None = None
        diagnostic = None
        started_at = _utcnow().isoformat()
        discarded = False
        self._active_ids.add(task_id)
        try:
            self.executor.render_item(task_id, item_index, snapshot)
        except VoicePaused as exc:
            self._active_ids.discard(task_id)
            with write_session(self.engine) as session:
                row = session.get(MixTask, task_id)
                if claim is not None: self.serial_claims.finish(session, claim)
                row.state = 'cancelled' if row.state in {'cancelling', 'cancelled'} else 'paused'
                row.pause_requested = False
                row.auto_resume = False
                row.last_error = str(exc)
                value = json.dumps({'code': exc.code, 'message': str(exc), 'time': _utcnow().isoformat()}, ensure_ascii=False)
                key = 'mix-voice-pause:' + task_id
                saved = session.get(Setting, key)
                if saved is None:
                    session.add(Setting(key=key, value_json=value))
                else:
                    saved.value_json = value
            return
        except ItemCancelled:
            discarded = True
        except ItemInterrupted as exc:
            self._active_ids.discard(task_id)
            with write_session(self.engine) as session:
                row = session.get(MixTask, task_id)
                if claim is not None: self.serial_claims.finish(session, claim)
                row.state = 'cancelled' if row.state in {'cancelling', 'cancelled'} else 'interrupted'
                row.pause_requested = False
                row.auto_resume = False
                row.last_error = str(exc)
            return
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode('utf-8', errors='replace') if isinstance(exc.stderr, bytes) else (exc.stderr or str(exc))
            error = f"本机媒体处理失败：{detail[-4000:]}"
            diagnostic = error_diagnostic(exc)
        except Exception as exc:  # individual render failure is recorded, not retried here
            error = f"{type(exc).__name__}: {exc}"
            diagnostic = error_diagnostic(exc)

        with write_session(self.engine) as session:
            row = session.get(MixTask, task_id)
            if row is None:
                raise KeyError(task_id)
            if claim is not None:
                if not self.serial_claims.verify(session, claim): return
                self.serial_claims.finish(session, claim)
            self._active_ids.discard(task_id)
            if discarded:
                row.state = "cancelled"
                row.pause_requested = False
                return
            key = f"mix-result:{task_id}:{item_index}"
            ended_at = _utcnow().isoformat()
            item = {"index": item_index, "state": "failed" if error else "completed",
                    "error": error, "time": ended_at,
                    "execution_log": [dict(time=started_at, step='mix.started'),
                                      dict(time=ended_at, step='mix.failed' if error else 'mix.completed',
                                           **(diagnostic or {}))]}
            if diagnostic is not None:
                item['diagnostic'] = diagnostic
            value = json.dumps(item, ensure_ascii=False)
            receipt = session.get(Setting, key)
            if receipt is None:
                session.add(Setting(key=key, value_json=value))
            else:
                receipt.value_json = value
            # Cancellation can be requested while one item is executing. That item has
            # already finished/failed, so record its result but never start another.
            if not serial or error is None:
                row.current_index += 1
            row.completed_count += 1
            if error is None:
                row.success_count += 1
                self.clear_voice_pause(session, task_id)
            else:
                row.failure_count += 1
                row.last_error = error

            if row.state in {"cancelled", "cancelling"}:
                row.state = "cancelled"
                row.pause_requested = False
                return
            if row.pause_requested and (row.current_index < row.quantity or serial):
                row.state = "paused"
                row.pause_requested = False
                return
            if serial:
                row.state = 'paused' if error else 'waiting_publish'
                row.pause_requested = False
                return
            if row.current_index >= row.quantity:
                row.state = "completed_with_errors" if row.failure_count else "completed"
                row.pause_requested = False
                self._notify_completed(session, row)
            else:
                row.state = "running"

    @staticmethod
    def _notify_completed(session, row):
        # Commit the batch summary with terminal state, never per rendered item.
        from yijianjifa.features.notifications.service import NotificationRow
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'p011:mix-completed:' + row.id))
        if session.get(NotificationRow, identity) is None:
            session.add(NotificationRow(id=identity, task_id=row.id, status='mix_completed',
                message=f'制作完成：成功 {row.success_count}，失败 {row.failure_count}',
                deep_link='task:' + row.id))

    @staticmethod
    def _to_record(row: MixTask) -> MixTaskRecord:
        return MixTaskRecord(
            id=row.id,
            state=row.state,
            quantity=row.quantity,
            current_index=row.current_index,
            completed_count=row.completed_count,
            success_count=row.success_count,
            failure_count=row.failure_count,
            pause_requested=row.pause_requested,
            auto_resume=row.auto_resume,
            position=row.position,
            snapshot=json.loads(row.snapshot_json),
            last_error=row.last_error,
            created_at=(row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at).isoformat(),
        )
