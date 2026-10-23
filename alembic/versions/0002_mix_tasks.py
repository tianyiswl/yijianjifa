"""persistent mix task queue

Revision ID: 0002_mix_tasks
Revises: 0001_initial
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_mix_tasks"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mix_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("current_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pause_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_resume", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mix_tasks_state", "mix_tasks", ["state"])
    op.create_index("ix_mix_tasks_position", "mix_tasks", ["position"])


def downgrade() -> None:
    op.drop_index("ix_mix_tasks_position", table_name="mix_tasks")
    op.drop_index("ix_mix_tasks_state", table_name="mix_tasks")
    op.drop_table("mix_tasks")
