"""unified task center and issues

Revision ID: 0007_task_center
Revises: 0006_commerce
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_task_center"
down_revision = "0006_commerce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_center_records",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("queue_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_center_records_kind", "task_center_records", ["kind"])
    op.create_index("ix_task_center_records_state", "task_center_records", ["state"])
    op.create_index("ix_task_center_records_account_id", "task_center_records", ["account_id"])
    op.create_index("ix_task_center_records_queue_order", "task_center_records", ["queue_order"])

    op.create_table(
        "task_center_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=64), sa.ForeignKey("task_center_records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_task_center_items_task_id", "task_center_items", ["task_id"])
    op.create_index("ix_task_center_items_state", "task_center_items", ["state"])

    op.create_table(
        "task_issues",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=64), sa.ForeignKey("task_center_records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("object_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("stage", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("impact", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_action", sa.Text(), nullable=False, server_default=""),
        sa.Column("occurred_at", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("error_code", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_issues_task_id", "task_issues", ["task_id"])
    op.create_index("ix_task_issues_created_at", "task_issues", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_task_issues_created_at", table_name="task_issues")
    op.drop_index("ix_task_issues_task_id", table_name="task_issues")
    op.drop_table("task_issues")
    op.drop_index("ix_task_center_items_state", table_name="task_center_items")
    op.drop_index("ix_task_center_items_task_id", table_name="task_center_items")
    op.drop_table("task_center_items")
    op.drop_index("ix_task_center_records_queue_order", table_name="task_center_records")
    op.drop_index("ix_task_center_records_account_id", table_name="task_center_records")
    op.drop_index("ix_task_center_records_state", table_name="task_center_records")
    op.drop_index("ix_task_center_records_kind", table_name="task_center_records")
    op.drop_table("task_center_records")
