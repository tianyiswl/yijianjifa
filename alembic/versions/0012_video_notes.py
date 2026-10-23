"""Local video notes, independent from publish copy."""
from alembic import op
import sqlalchemy as sa
revision='0012_video_notes'
down_revision='0011_account_avatar'
branch_labels=None
depends_on=None

def upgrade():
    for table in ('media_assets','finished_videos'):
        if 'note' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table,sa.Column('note',sa.Text(),nullable=False,server_default=''))

def downgrade():
    for table in ('media_assets','finished_videos'):
        op.drop_column(table,'note')
