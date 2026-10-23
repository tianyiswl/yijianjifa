"""Explicit local candidates; retain existing ownership/reference schema."""
from alembic import op
import sqlalchemy as sa
from contextlib import contextmanager

revision='0015_local_video'
down_revision='0014_downloads'
branch_labels=None
depends_on=None

@contextmanager
def _candidate_copy():
    bind=op.get_bind();sqlite=bind.dialect.name=='sqlite'
    # The application migration runner owns the connection and can disable FK
    # before BEGIN, then restore it after commit/rollback and before returning
    # the same connection. Alembic cannot safely restore within its caller's
    # still-active transaction, so refuse an unguarded FK-on parent copy.
    if sqlite and bind.exec_driver_sql('PRAGMA foreign_keys').scalar_one():
        raise RuntimeError('Local video migration requires the guarded migration runner')
    yield
    if sqlite and bind.exec_driver_sql('PRAGMA foreign_key_check').first():
        raise RuntimeError('Local video migration preserved an invalid reference')

def upgrade():
    # Keep the existing engine's enforcement preference unchanged after return.
    with _candidate_copy():
        with op.batch_alter_table('download_candidates',recreate='always') as batch:
            batch.drop_constraint('ck_download_candidate_platform',type_='check')
            batch.create_check_constraint('ck_download_candidate_platform',"platform IN ('douyin','direct','local')")
            batch.create_index('uq_download_local_source',['source_key'],unique=True,sqlite_where=sa.text("platform='local'"))

def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM download_candidates WHERE platform='local' LIMIT 1")).first():
        raise RuntimeError('Local video records must be preserved; downgrade blocked')
    with _candidate_copy():
        with op.batch_alter_table('download_candidates',recreate='always') as batch:
            batch.drop_index('uq_download_local_source')
            batch.drop_constraint('ck_download_candidate_platform',type_='check')
            batch.create_check_constraint('ck_download_candidate_platform',"platform IN ('douyin','direct')")
