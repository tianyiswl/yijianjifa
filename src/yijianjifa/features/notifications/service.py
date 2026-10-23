from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, DateTime, Integer, String, Text, insert, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_STATUS_LABELS = {
    "mix_completed": "制作完成",
    "platform_scheduled": "平台定时已设置",
    "published": "实际已发布",
    "result_unknown": "结果待确认",
    "action_required": "需要处理",
    "download_completed": "下载完成",
    "download_completed_with_errors": "下载部分完成",
    "download_failed": "下载失败",
}


def user_status_label(status: str) -> str:
    try:
        return _STATUS_LABELS[status]
    except KeyError as exc:
        raise ValueError(f"unknown notification status: {status}") from exc


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    deep_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


@dataclass(frozen=True)
class NotificationRecord:
    id: str
    task_id: str | None
    status: str
    message: str
    deep_link: str | None
    created_at: datetime
    read: bool


class NotificationService:
    def __init__(self, engine: Engine, *, sound_enabled: bool = True):
        self.engine = engine
        self.sound_enabled = bool(sound_enabled)
        self.sound_play_count = 0

    def set_sound_enabled(self, enabled: bool) -> None:
        self.sound_enabled = bool(enabled)

    def emit_batch_summary(
        self,
        *,
        task_id: str,
        status: str,
        success_count: int,
        failure_count: int,
    ) -> NotificationRecord:
        label = user_status_label(status)
        message = f"{label}：成功 {int(success_count)}，失败 {int(failure_count)}"
        return self._emit(task_id=task_id, status=status, message=message, deep_link=f"task:{task_id}")

    def emit_action_required(self, task_id: str, message: str) -> NotificationRecord:
        return self._emit(
            task_id=task_id,
            status="action_required",
            message=str(message),
            deep_link=f"task:{task_id}",
        )

    def emit_download_transition(self, connection: Connection, *, task_id: str,
                                 state: str, counts: dict[str, int], reasons: tuple[str, ...] = ()) -> NotificationRecord:
        """Caller commits the task/marker/note atomically; shared sound pump reads it."""
        fields = {'waiting', 'running', 'succeeded', 'partial', 'failed', 'skipped', 'cancelled', 'interrupted'}
        if (not connection.in_transaction() or type(task_id) is not str or not 1 <= len(task_id) <= 64
                or state not in ('completed', 'completed_with_errors', 'paused_auth', 'paused')
                or type(reasons) is not tuple or len(reasons) > 3
                or any(type(reason) is not str or reason not in ('auth', 'watermark', 'model') for reason in reasons)
                or len(set(reasons)) != len(reasons)
                or state in ('paused', 'paused_auth') and not reasons
                or type(counts) is not dict or set(counts) != fields
                or any(type(value) is not int or not 0 <= value <= 200 for value in counts.values())
                or not 1 <= sum(counts.values()) <= 200):
            raise ValueError('invalid download notification')
        summary = (f"成功 {counts['succeeded']}，部分完成 {counts['partial']}，失败 {counts['failed']}，"
                   f"跳过 {counts['skipped']}，取消 {counts['cancelled']}")
        if state in ('paused_auth', 'paused'):
            actions = {'auth': '登录或手动验证', 'watermark': '确认原样下载或跳过', 'model': '下载或导入所需识别模型'}
            status, message = 'action_required', '下载任务需要' + '、'.join(actions[reason] for reason in reasons) + '，请打开任务后继续。' + summary
        else:
            status = ('download_completed' if state == 'completed' else
                      'download_failed' if counts['failed'] and not (counts['succeeded'] or counts['partial'])
                      else 'download_completed_with_errors')
            message = user_status_label(status) + '：' + summary
        values = dict(id=_uuid(), task_id=task_id, status=status, message=message,
                      deep_link=f'task:{task_id}', created_at=_utcnow(), read=False)
        connection.execute(insert(NotificationRow.__table__).values(**values))
        return NotificationRecord(**values)

    def history_for(self, task_id: str) -> list[NotificationRecord]:
        with session_scope(self.engine) as session:
            rows = session.scalars(
                select(NotificationRow)
                .where(NotificationRow.task_id == task_id)
                .order_by(NotificationRow.created_at, NotificationRow.id)
            ).all()
            return [self._record(row) for row in rows]

    def history(self, *, unread_only: bool = False) -> list[NotificationRecord]:
        with session_scope(self.engine) as session:
            stmt = select(NotificationRow)
            if unread_only:
                stmt = stmt.where(NotificationRow.read.is_(False))
            rows = session.scalars(stmt.order_by(NotificationRow.created_at.desc(), NotificationRow.id.desc())).all()
            return [self._record(row) for row in rows]

    def mark_all_read(self) -> int:
        from sqlalchemy import update
        with session_scope(self.engine) as session:
            result = session.execute(update(NotificationRow).where(NotificationRow.read.is_(False)).values(read=True))
            return result.rowcount

    def mark_read(self, notification_id: str) -> NotificationRecord:
        with session_scope(self.engine) as session:
            row = session.get(NotificationRow, notification_id)
            if row is None:
                raise KeyError(notification_id)
            row.read = True
            session.flush()
            return self._record(row)

    def _emit(self, *, task_id: str | None, status: str, message: str, deep_link: str | None) -> NotificationRecord:
        user_status_label(status)
        with session_scope(self.engine) as session:
            row = NotificationRow(task_id=task_id, status=status, message=message, deep_link=deep_link)
            session.add(row)
            session.flush()
            record = self._record(row)
        if self.sound_enabled:
            self.sound_play_count += 1
        return record

    @staticmethod
    def _record(row: NotificationRow) -> NotificationRecord:
        return NotificationRecord(
            id=row.id,
            task_id=row.task_id,
            status=row.status,
            message=row.message,
            deep_link=row.deep_link,
            created_at=row.created_at,
            read=row.read,
        )
