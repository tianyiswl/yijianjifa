"""Record successful account login separately from metadata updates."""
from alembic import op
import sqlalchemy as sa
revision='0013_account_login_time'
down_revision='0012_video_notes'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('accounts',sa.Column('last_login_at',sa.DateTime(timezone=True),nullable=True))

def downgrade():
    op.drop_column('accounts','last_login_at')
