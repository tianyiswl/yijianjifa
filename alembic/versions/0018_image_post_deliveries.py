"""General content identity without inventing video identities for image posts."""
from alembic import op
import sqlalchemy as sa

revision = '0018_image_post_deliveries'
down_revision = '0017_image_posts'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for name in ('publish_history', 'publish_deliveries'):
        with op.batch_alter_table(name) as batch:
            batch.add_column(sa.Column('content_type', sa.String(32), nullable=False, server_default='video'))
            batch.add_column(sa.Column('content_id', sa.String(36), nullable=True))
            batch.add_column(sa.Column('content_hash', sa.String(64), nullable=True))
            batch.alter_column('video_hash', existing_type=sa.String(64), nullable=True)
            if name == 'publish_deliveries':
                batch.alter_column('video_id', existing_type=sa.String(36), nullable=True)
        bind.execute(sa.text(f'UPDATE {name} SET content_hash=video_hash'))
    bind.execute(sa.text('UPDATE publish_deliveries SET content_id=video_id'))
    bind.execute(sa.text('UPDATE publish_history SET content_id=(SELECT video_id FROM publish_deliveries WHERE history_id=publish_history.id)'))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM publish_deliveries WHERE content_type!='video' LIMIT 1")).first():
        raise RuntimeError('image publication evidence must be retained')
    if bind.dialect.name == 'sqlite' and bind.exec_driver_sql('PRAGMA foreign_keys').scalar_one():
        raise RuntimeError('content migration requires the guarded migration runner')
    for name in ('publish_deliveries', 'publish_history'):
        with op.batch_alter_table(name) as batch:
            if name == 'publish_deliveries':
                batch.alter_column('video_id', existing_type=sa.String(36), nullable=False)
            batch.alter_column('video_hash', existing_type=sa.String(64), nullable=False)
            for column in ('content_hash', 'content_id', 'content_type'):
                batch.drop_column(column)
