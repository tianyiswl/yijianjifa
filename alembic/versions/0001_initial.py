"""initial library schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from yijianjifa.storage.models import (
    MediaAsset, CopyRecord, Category, UsageProgress, HistoricalIdentity, Setting
)

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _legacy_category_links():
    # Freeze this historical table instead of importing today's single-category
    # model. Revision 0019 is responsible for converting existing associations.
    metadata = sa.MetaData()
    Category.__table__.to_metadata(metadata)
    return sa.Table(
        "category_links", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("category_id", sa.String(36), sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("target_type", sa.String(16), nullable=False, index=True),
        sa.Column("target_id", sa.String(64), nullable=False, index=True),
        sa.UniqueConstraint("category_id", "target_type", "target_id", name="uq_category_target"),
    )


def upgrade() -> None:
    tables = [MediaAsset.__table__, CopyRecord.__table__, Category.__table__, _legacy_category_links(), UsageProgress.__table__, HistoricalIdentity.__table__, Setting.__table__]
    for table in tables:
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    tables = [Setting.__table__, HistoricalIdentity.__table__, UsageProgress.__table__, _legacy_category_links(), Category.__table__, CopyRecord.__table__, MediaAsset.__table__]
    for table in tables:
        table.drop(bind=op.get_bind(), checkfirst=True)
