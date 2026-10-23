"""typed reusable presets

Revision ID: 0009_presets
Revises: 0008_notifications
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_presets"
down_revision = "0008_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "presets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_presets_kind", "presets", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_presets_kind", table_name="presets")
    op.drop_table("presets")
