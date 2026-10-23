"""platform aware accounts

Revision ID: 0004_accounts
Revises: 0003_finished_videos
Create Date: 2026-09-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_accounts"
down_revision = "0003_finished_videos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("platform_user_id", sa.String(length=128), nullable=False),
        sa.Column("nickname", sa.String(length=255), nullable=False),
        sa.Column("remark", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("group_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("login_state", sa.String(length=32), nullable=False, server_default="valid"),
        sa.Column("session_ref", sa.Text(), nullable=True),
        sa.Column("capability_json", sa.Text(), nullable=False, server_default='{"ordinary":"pending","commerce":"pending","location_scope":"pending"}'),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("platform", "platform_user_id", name="uq_account_platform_identity"),
    )
    op.create_index("ix_accounts_platform", "accounts", ["platform"])
    op.create_index("ix_accounts_is_default", "accounts", ["is_default"])


def downgrade() -> None:
    op.drop_index("ix_accounts_is_default", table_name="accounts")
    op.drop_index("ix_accounts_platform", table_name="accounts")
    op.drop_table("accounts")
