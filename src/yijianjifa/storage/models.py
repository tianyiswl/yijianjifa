from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (UniqueConstraint("kind", "content_hash", name="uq_media_kind_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    managed_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    missing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class CopyRecord(Base):
    __tablename__ = "copy_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    voice: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")
    full: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_category_kind_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class CategoryLink(Base):
    __tablename__ = "category_links"
    __table_args__ = (
        UniqueConstraint("category_id", "target_type", "target_id", name="uq_category_target"),
        Index("uq_category_target_single", "target_type", "target_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class CategoryLinkMigrationBackup(Base):
    """Local recovery evidence for associations removed by the single-category migration.

    Deliberately has no foreign keys: deleting content or a category must not
    erase its migration history. Downgrading also keeps this recovery table.
    """

    __tablename__ = "category_link_migration_backups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    migration_revision: Mapped[str] = mapped_column(String(32), nullable=False)
    link_id: Mapped[str] = mapped_column(String(36), nullable=False)
    category_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    category_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    category_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retained_link_id: Mapped[str] = mapped_column(String(36), nullable=False)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageProgress(Base):
    __tablename__ = "usage_progress"
    __table_args__ = (UniqueConstraint("scope", "copy_id", name="uq_progress_scope_copy"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    copy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class HistoricalIdentity(Base):
    __tablename__ = "historical_identities"
    __table_args__ = (UniqueConstraint("identity_kind", "content_hash", name="uq_history_identity"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    identity_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class DownloadCandidateRow(Base):
    __tablename__ = 'download_candidates'
    __table_args__ = (
        CheckConstraint("platform IN ('douyin','direct','local')", name='ck_download_candidate_platform'),
        Index('uq_download_work', 'platform', 'work_id', unique=True, sqlite_where=text("platform='douyin'")),
        Index('uq_download_direct_source', 'source_key', unique=True, sqlite_where=text("platform='direct'")),
        Index('uq_download_local_source', 'source_key', unique=True, sqlite_where=text("platform='local'")),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    work_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_source: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[str | None] = mapped_column(String(64))
    cover_resource_id: Mapped[str | None] = mapped_column(ForeignKey('download_outputs.id', ondelete='SET NULL'))
    region_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadResolutionRow(Base):
    __tablename__ = 'download_resolutions'
    __table_args__ = (CheckConstraint("completion IN ('partial','complete','limited')", name='ck_download_resolution_completion'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_source: Mapped[str | None] = mapped_column(Text)
    completion: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadAuxiliaryJobRow(Base):
    __tablename__ = 'download_auxiliary_jobs'
    __table_args__ = (CheckConstraint("state IN ('waiting','running','succeeded','failed','skipped','cancelled','interrupted')", name='ck_download_job_state'),
                      CheckConstraint("completion IN ('partial','complete','limited')", name='ck_download_job_completion'),
                      CheckConstraint("kind IN ('resolve','cover','preview','region','resource_import','resource_preview','resource_reveal','resource_delete','subtitle_export','model_download','model_import')", name='ck_download_job_kind'),
                      CheckConstraint('subtitle_revision IS NULL OR subtitle_revision >= 0', name='ck_download_job_revision'),
                      CheckConstraint('installed_model_id IS NULL OR installed_model_id = model_id', name='ck_download_job_model_result'),
                      ForeignKeyConstraint(['subtitle_draft_id', 'subtitle_revision'], ['download_subtitle_revisions.draft_id', 'download_subtitle_revisions.revision'], ondelete='RESTRICT'))
    # Generated safe job ID, never a provider's MediaSource.handle.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey('download_resolutions.id', ondelete='CASCADE'))
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey('download_candidates.id', ondelete='RESTRICT'))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    completion: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(ForeignKey('download_outputs.id', ondelete='RESTRICT'))
    model_id: Mapped[str | None] = mapped_column(String(64))
    output_resource_id: Mapped[str | None] = mapped_column(ForeignKey('download_outputs.id', ondelete='RESTRICT'))
    subtitle_original_id: Mapped[str | None] = mapped_column(ForeignKey('download_subtitle_originals.id', ondelete='RESTRICT'))
    subtitle_draft_id: Mapped[str | None] = mapped_column(ForeignKey('download_subtitle_drafts.id', ondelete='RESTRICT'))
    subtitle_revision: Mapped[int | None] = mapped_column(Integer)
    subtitle_origin: Mapped[str | None] = mapped_column(String(32))
    export_format: Mapped[str | None] = mapped_column(String(8))
    input_grant_id: Mapped[str | None] = mapped_column(String(64))
    output_grant_id: Mapped[str | None] = mapped_column(String(64))
    category_ids_json: Mapped[str] = mapped_column(Text, nullable=False, server_default='[]')
    library_id: Mapped[str | None] = mapped_column(ForeignKey('media_assets.id', ondelete='RESTRICT'))
    installed_model_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadItemRow(Base):
    __tablename__ = 'download_items'
    __table_args__ = (UniqueConstraint('task_id', 'candidate_id', name='uq_download_task_candidate'),
                      UniqueConstraint('id', 'candidate_id', name='uq_download_item_candidate'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('task_center_records.id', ondelete='CASCADE'), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey('download_candidates.id', ondelete='RESTRICT'), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadOutputRow(Base):
    __tablename__ = 'download_outputs'
    __table_args__ = (
        ForeignKeyConstraint(['item_id', 'candidate_id'], ['download_items.id', 'download_items.candidate_id'], ondelete='CASCADE'),
        CheckConstraint('size_bytes >= 0', name='ck_download_output_size'),
        CheckConstraint('duration_ms IS NULL OR duration_ms >= 0', name='ck_download_output_duration'),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey('download_candidates.id', ondelete='RESTRICT'), nullable=False)
    item_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    backend_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadStageRow(Base):
    __tablename__ = 'download_stages'
    __table_args__ = (UniqueConstraint('item_id', 'stage', name='uq_download_item_stage'),
                      CheckConstraint("state IN ('waiting','running','succeeded','failed','skipped','cancelled','interrupted')", name='ck_download_stage_state'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey('download_items.id', ondelete='CASCADE'), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    required_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(ForeignKey('download_outputs.id', ondelete='RESTRICT'))
    error_code: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadSourceRow(Base):
    __tablename__ = 'download_sources'
    __table_args__ = (UniqueConstraint('candidate_id', 'source_key', name='uq_download_candidate_source'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey('download_candidates.id', ondelete='CASCADE'), nullable=False)
    session_id: Mapped[str | None] = mapped_column(ForeignKey('download_resolutions.id', ondelete='SET NULL'))
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadSubtitleOriginalRow(Base):
    __tablename__ = 'download_subtitle_originals'
    __table_args__ = (ForeignKeyConstraint(['item_id', 'candidate_id'], ['download_items.id', 'download_items.candidate_id'], ondelete='CASCADE'),
                      CheckConstraint("origin IN ('platform_caption','embedded_caption','asr','ocr')", name='ck_download_subtitle_origin'))
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey('download_candidates.id', ondelete='RESTRICT'), nullable=False)
    item_id: Mapped[str | None] = mapped_column(String(36))
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    segments_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadSubtitleDraftRow(Base):
    __tablename__ = 'download_subtitle_drafts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    original_id: Mapped[str] = mapped_column(ForeignKey('download_subtitle_originals.id', ondelete='CASCADE'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadSubtitleRevisionRow(Base):
    __tablename__ = 'download_subtitle_revisions'
    __table_args__ = (UniqueConstraint('draft_id', 'revision', name='uq_download_draft_revision'),
                      CheckConstraint('revision > 0', name='ck_download_revision_positive'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey('download_subtitle_drafts.id', ondelete='CASCADE'), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    segments_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadDecisionRow(Base):
    __tablename__ = 'download_decisions'
    __table_args__ = (CheckConstraint("decision IN ('original','skip')", name='ck_download_decision'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey('download_items.id', ondelete='CASCADE'), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
