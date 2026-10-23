"""Independent V2 delivery authority; never modify V1 snapshots or journals."""
from alembic import op
import sqlalchemy as sa

revision='0016_publish_deliveries'
down_revision='0015_local_video'
branch_labels=None
depends_on=None


def upgrade():
    # Keep historical schema definitions independent of future ORM changes.
    op.create_table('publish_deliveries',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('task_id',sa.String(36),sa.ForeignKey('publish_tasks.id'),nullable=False),
        sa.Column('item_id',sa.String(36),sa.ForeignKey('task_center_items.id'),nullable=False),
        sa.Column('history_id',sa.String(36),sa.ForeignKey('publish_history.id'),nullable=False),
        sa.Column('platform',sa.String(32),nullable=False),
        sa.Column('account_id',sa.String(36),nullable=False),
        sa.Column('platform_user_id',sa.String(128),nullable=False),
        sa.Column('video_id',sa.String(36),nullable=False),
        sa.Column('video_hash',sa.String(64),nullable=False),
        sa.Column('snapshot_json',sa.Text(),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('execution_mode',sa.String(32),nullable=False),
        sa.Column('scheduled_at',sa.String(64),nullable=True),
        sa.Column('attempt',sa.Integer(),nullable=False),
        sa.Column('position',sa.Integer(),nullable=False),
        sa.Column('hidden',sa.Boolean(),nullable=False),
        sa.Column('cancel_requested',sa.Boolean(),nullable=False),
        sa.UniqueConstraint('item_id'),sa.UniqueConstraint('history_id'))
    op.create_index('ix_publish_deliveries_task_id','publish_deliveries',['task_id'])
    op.create_table('publish_delivery_attempts',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('delivery_id',sa.String(36),sa.ForeignKey('publish_deliveries.id'),nullable=False),
        sa.Column('number',sa.Integer(),nullable=False),
        sa.Column('snapshot_json',sa.Text(),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('owner_hash',sa.String(64),nullable=True),
        sa.Column('final_json',sa.Text(),nullable=True),
        sa.Column('result_json',sa.Text(),nullable=True),
        sa.Column('context_json',sa.Text(),nullable=True),
        sa.Column('recovery_json',sa.Text(),nullable=True),
        sa.UniqueConstraint('delivery_id','number'))
    op.create_index('ix_publish_delivery_attempts_delivery_id','publish_delivery_attempts',['delivery_id'])
    op.create_table('publish_v2_resources',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('task_id',sa.String(36),sa.ForeignKey('publish_tasks.id'),nullable=False),
        sa.Column('path',sa.Text(),nullable=False),
        sa.Column('content_hash',sa.String(64),nullable=False),
        sa.Column('cleaned',sa.Boolean(),nullable=False))
    op.create_table('publish_v2_resource_consumers',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('resource_id',sa.String(36),sa.ForeignKey('publish_v2_resources.id'),nullable=False),
        sa.Column('delivery_id',sa.String(36),sa.ForeignKey('publish_deliveries.id'),nullable=False),
        sa.Column('released',sa.Boolean(),nullable=False),
        sa.UniqueConstraint('resource_id','delivery_id'))
    op.create_table('publish_v2_receipts',
        sa.Column('request_id',sa.String(128),primary_key=True),
        sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('task_id',sa.String(36),sa.ForeignKey('publish_tasks.id'),nullable=False,unique=True),
        sa.Column('receipt_json',sa.Text(),nullable=False))


def downgrade():
    bind=op.get_bind()
    if bind.execute(sa.text('SELECT 1 FROM publish_deliveries LIMIT 1')).first():
        raise RuntimeError('V2 evidence exists; downgrade must preserve target history')
    # A direct SQLite downgrade that later reaches the FK-on parent copy can
    # autocommit these drops before 0015 refuses. Refuse before any DDL instead.
    if bind.dialect.name=='sqlite' and bind.exec_driver_sql('PRAGMA foreign_keys').scalar_one():
        raise RuntimeError('V2 SQLite downgrade requires the guarded migration runner')
    for name in ('publish_v2_receipts','publish_v2_resource_consumers','publish_v2_resources','publish_delivery_attempts','publish_deliveries'):
        op.drop_table(name)
