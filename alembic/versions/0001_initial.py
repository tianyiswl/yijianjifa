"""initial library schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op

from yijianjifa.storage.models import (
    MediaAsset, CopyRecord, Category, CategoryLink, UsageProgress, HistoricalIdentity, Setting
)

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = [MediaAsset.__table__, CopyRecord.__table__, Category.__table__, CategoryLink.__table__, UsageProgress.__table__, HistoricalIdentity.__table__, Setting.__table__]
    for table in tables:
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    tables = [Setting.__table__, HistoricalIdentity.__table__, UsageProgress.__table__, CategoryLink.__table__, Category.__table__, CopyRecord.__table__, MediaAsset.__table__]
    for table in tables:
        table.drop(bind=op.get_bind(), checkfirst=True)
