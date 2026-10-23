"""Atomic receipts for local Web UI library commands."""
from alembic import op
import sqlalchemy as sa

revision = '0010_web_command_receipts'
down_revision = '0009_presets'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('web_command_receipts',
        sa.Column('request_id', sa.String(64), primary_key=True),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('response_json', sa.Text(), nullable=False))


def downgrade():
    op.drop_table('web_command_receipts')
