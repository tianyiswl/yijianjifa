"""finished video library

Revision ID: 0003_finished_videos
Revises: 0002_mix_tasks
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_finished_videos"
down_revision = "0002_mix_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finished_videos",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("origin_type", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("original_path", sa.Text(), nullable=True),
        sa.Column("stored_path", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags", sa.Text(), nullable=False, server_default=""),
        sa.Column("copy_id", sa.String(length=64), nullable=True),
        sa.Column("task_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_finished_videos_origin_type", "finished_videos", ["origin_type"])
    op.create_index("ix_finished_videos_content_hash", "finished_videos", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_finished_videos_content_hash", table_name="finished_videos")
    op.drop_index("ix_finished_videos_origin_type", table_name="finished_videos")
    op.drop_table("finished_videos")
