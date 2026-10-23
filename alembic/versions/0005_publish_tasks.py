"""normal publish tasks and history

Revision ID: 0005_publish_tasks
Revises: 0004_accounts
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_publish_tasks"
down_revision = "0004_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publish_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="confirmed"),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_publish_tasks_platform", "publish_tasks", ["platform"])
    op.create_index("ix_publish_tasks_state", "publish_tasks", ["state"])
    op.create_table(
        "publish_history",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("video_hash", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_publish_history_video_hash", "publish_history", ["video_hash"])
    op.create_index("ix_publish_history_account_id", "publish_history", ["account_id"])
    op.create_index("ix_publish_history_state", "publish_history", ["state"])


def downgrade() -> None:
    op.drop_index("ix_publish_history_state", table_name="publish_history")
    op.drop_index("ix_publish_history_account_id", table_name="publish_history")
    op.drop_index("ix_publish_history_video_hash", table_name="publish_history")
    op.drop_table("publish_history")
    op.drop_index("ix_publish_tasks_state", table_name="publish_tasks")
    op.drop_index("ix_publish_tasks_platform", table_name="publish_tasks")
    op.drop_table("publish_tasks")
