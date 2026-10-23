from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.features.tasks.models import IssueRecord, TaskItemRecord, TaskRecord
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskCenterRow(Base):
    __tablename__ = "task_center_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    queue_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class TaskItemRow(Base):
    __tablename__ = "task_center_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_center_records.id", ondelete="CASCADE"), nullable=False, index=True)
    item_key: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class TaskIssueRow(Base):
    __tablename__ = "task_issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_center_records.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    object_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    impact: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    occurred_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)


class TaskCenterService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def register_task(
        self,
        task_id: str,
        *,
        kind: str,
        name: str,
        state: str,
        snapshot_json: str,
        account_id: str | None = None,
        queue_order: int = 0,
    ) -> TaskRecord:
        # Parse now so malformed snapshots can never become confirmed records.
        json.loads(snapshot_json)
        with session_scope(self.engine) as session:
            if session.get(TaskCenterRow, task_id) is not None:
                raise ValueError(f"task already registered: {task_id}")
            row = TaskCenterRow(
                id=str(task_id),
                kind=str(kind),
                name=str(name),
                state=str(state),
                account_id=account_id,
                snapshot_json=snapshot_json,
                queue_order=int(queue_order),
            )
            session.add(row)
            session.flush()
            return self._task(row)

    def get(self, task_id: str) -> TaskRecord:
        with session_scope(self.engine) as session:
            row = session.get(TaskCenterRow, task_id)
            if row is None:
                raise KeyError(task_id)
            return self._task(row)

    def list(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
        query: str | None = None,
    ) -> list[TaskRecord]:
        with session_scope(self.engine) as session:
            stmt = select(TaskCenterRow)
            if kind is not None:
                stmt = stmt.where(TaskCenterRow.kind == kind)
            if state is not None:
                stmt = stmt.where(TaskCenterRow.state == state)
            rows = session.scalars(stmt.order_by(TaskCenterRow.queue_order, TaskCenterRow.created_at, TaskCenterRow.id)).all()
            result = [self._task(row) for row in rows]
        if query:
            needle = query.casefold().strip()
            result = [
                row for row in result
                if needle in " ".join((row.id, row.name, row.account_id or "")).casefold()
            ]
        return result

    def set_state(self, task_id: str, state: str) -> TaskRecord:
        with session_scope(self.engine) as session:
            row = session.get(TaskCenterRow, task_id)
            if row is None:
                raise KeyError(task_id)
            row.state = str(state)
            row.updated_at = _utcnow()
            session.flush()
            return self._task(row)

    def record_issue(
        self,
        *,
        task_id: str,
        state: str,
        evidence: dict,
        title: str = "",
        object_name: str = "",
        stage: str = "",
        reason: str = "",
        impact: str = "",
        next_action: str = "",
        occurred_at: str = "",
        error_code: str = "",
    ) -> IssueRecord:
        # Use the same finite diagnostic contract at the storage boundary;
        # hiding credentials only during display/export leaves them on disk.
        from yijianjifa.features.tasks.diagnostics import DiagnosticBundle
        clean = DiagnosticBundle.from_raw(dict(
            title=title, object=object_name, stage=stage, reason=reason,
            impact=impact, next_action=next_action, occurred_at=occurred_at,
            error_code=error_code, evidence=evidence,
        )).payload
        with session_scope(self.engine) as session:
            task = session.get(TaskCenterRow, task_id)
            if task is None:
                raise KeyError(task_id)
            task.state = str(state)
            task.updated_at = _utcnow()
            row = TaskIssueRow(
                task_id=task_id,
                title=clean["title"],
                object_name=clean["object"],
                stage=clean["stage"],
                reason=clean["reason"],
                impact=clean["impact"],
                next_action=clean["next_action"],
                occurred_at=clean["occurred_at"],
                error_code=clean["error_code"],
                evidence_json=json.dumps(clean["evidence"], ensure_ascii=False, sort_keys=True),
            )
            session.add(row)
            session.flush()
            return self._issue(row)

    def latest_issue(self, task_id: str) -> IssueRecord | None:
        with session_scope(self.engine) as session:
            row = session.scalar(
                select(TaskIssueRow)
                .where(TaskIssueRow.task_id == task_id)
                .order_by(TaskIssueRow.created_at.desc(), TaskIssueRow.id.desc())
                .limit(1)
            )
            return None if row is None else self._issue(row)

    def recover_after_restart(self) -> list[TaskRecord]:
        """Freeze previously running work and return tasks needing a decision."""

        # V2 child records own recovery; do not convert only the mixed parent.
        from yijianjifa.features.publish.deliveries import PublishDeliveries
        PublishDeliveries(self.engine).recover_after_restart()
        with session_scope(self.engine) as session:
            rows = session.scalars(select(TaskCenterRow)).all()
            for row in rows:
                if row.kind != "multi_publish" and row.state in {"running", "publishing"}:
                    row.state = "interrupted"
                    row.updated_at = _utcnow()
            session.flush()
            candidates = [
                self._task(row)
                for row in rows
                if row.state
                in {"interrupted", "paused", "paused_auth", "result_unknown", "pre_submit_failed", "failed"}
            ]
        return sorted(candidates, key=lambda row: (row.queue_order, row.created_at, row.id))

    def move_waiting(self, task_id: str, *, direction: str) -> None:
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        with session_scope(self.engine) as session:
            target = session.get(TaskCenterRow, task_id)
            if target is None:
                raise KeyError(task_id)
            if target.state != "waiting":
                raise ValueError("only waiting tasks can be reordered")
            waiting = session.scalars(
                select(TaskCenterRow)
                .where(TaskCenterRow.kind == target.kind, TaskCenterRow.state == "waiting")
                .order_by(TaskCenterRow.queue_order, TaskCenterRow.created_at, TaskCenterRow.id)
            ).all()
            index = next(i for i, row in enumerate(waiting) if row.id == task_id)
            other_index = index - 1 if direction == "up" else index + 1
            if other_index < 0 or other_index >= len(waiting):
                return
            other = waiting[other_index]
            target.queue_order, other.queue_order = other.queue_order, target.queue_order
            target.updated_at = other.updated_at = _utcnow()

    @staticmethod
    def _task(row: TaskCenterRow) -> TaskRecord:
        return TaskRecord(
            id=row.id,
            kind=row.kind,
            name=row.name,
            state=row.state,
            account_id=row.account_id,
            snapshot_json=row.snapshot_json,
            queue_order=row.queue_order,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _issue(row: TaskIssueRow) -> IssueRecord:
        return IssueRecord(
            id=row.id,
            task_id=row.task_id,
            title=row.title,
            object_name=row.object_name,
            stage=row.stage,
            reason=row.reason,
            impact=row.impact,
            next_action=row.next_action,
            occurred_at=row.occurred_at,
            error_code=row.error_code,
            evidence_json=row.evidence_json,
        )
