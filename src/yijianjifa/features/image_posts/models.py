from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from yijianjifa.storage.models import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ImagePost(Base):
    __tablename__ = "image_posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class ImagePostImage(Base):
    __tablename__ = "image_post_images"
    __table_args__ = (UniqueConstraint("post_id", "position", name="uq_image_post_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    post_id: Mapped[str] = mapped_column(ForeignKey("image_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)


@dataclass(frozen=True, slots=True)
class ImagePostImageRecord:
    id: str
    position: int
    path: Path
    content_hash: str
    width: int
    height: int
    format: str
    original_name: str
    thumbnail_path: Path


@dataclass(frozen=True, slots=True)
class ImagePostRecord:
    id: str
    display_name: str
    title: str
    body: str
    tags: str
    content_hash: str
    created_at: datetime
    images: tuple[ImagePostImageRecord, ...]

    @property
    def thumbnail_path(self) -> Path | None:
        return self.images[0].thumbnail_path if self.images else None
