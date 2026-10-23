"""Keep the public account avatar locally, outside remote page context."""
from alembic import op
import sqlalchemy as sa
revision='0011_account_avatar'
down_revision='0010_web_command_receipts'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('accounts',sa.Column('avatar_data',sa.Text(),nullable=False,server_default=''))

def downgrade():
    op.drop_column('accounts','avatar_data')
