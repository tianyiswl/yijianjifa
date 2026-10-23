"""single-account commerce tasks

Revision ID: 0006_commerce
Revises: 0005_publish_tasks
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_commerce"
down_revision = "0005_publish_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commerce_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="confirmed"),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_commerce_tasks_account_id", "commerce_tasks", ["account_id"])
    op.create_index("ix_commerce_tasks_state", "commerce_tasks", ["state"])


def downgrade() -> None:
    op.drop_index("ix_commerce_tasks_state", table_name="commerce_tasks")
    op.drop_index("ix_commerce_tasks_account_id", table_name="commerce_tasks")
    op.drop_table("commerce_tasks")
