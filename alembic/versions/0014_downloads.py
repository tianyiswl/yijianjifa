"""Download-only persistence; task_center_records owns the batch state."""
from alembic import op
import sqlalchemy as sa

revision = '0014_downloads'
down_revision = '0013_account_login_time'
branch_labels = None
depends_on = None


def stamp(name):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def identifier(name='id', size=64):
    return sa.Column(name, sa.String(size), primary_key=True)


def candidate_reference(ondelete='RESTRICT'):
    return sa.Column('candidate_id', sa.String(128), sa.ForeignKey('download_candidates.id', ondelete=ondelete), nullable=False)


def item_ownership():
    return sa.ForeignKeyConstraint(['item_id', 'candidate_id'], ['download_items.id', 'download_items.candidate_id'], ondelete='CASCADE')


def upgrade():
    op.create_table('download_candidates', identifier(size=128),
        sa.Column('platform', sa.String(16), nullable=False),
        sa.Column('work_id', sa.String(128), nullable=False),
        sa.Column('source_key', sa.String(128), nullable=False),
        sa.Column('canonical_source', sa.Text(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False), sa.Column('author', sa.Text(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=True), sa.Column('published_at', sa.String(64), nullable=True),
        sa.Column('cover_resource_id', sa.String(64), sa.ForeignKey('download_outputs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('region_json', sa.Text(), nullable=False), stamp('created_at'), stamp('updated_at'),
        sa.CheckConstraint("platform IN ('douyin','direct')", name='ck_download_candidate_platform'))
    op.create_index('uq_download_work', 'download_candidates', ['platform', 'work_id'], unique=True, sqlite_where=sa.text("platform='douyin'"))
    op.create_index('uq_download_direct_source', 'download_candidates', ['source_key'], unique=True, sqlite_where=sa.text("platform='direct'"))
    op.create_table('download_resolutions', identifier(),
        sa.Column('source_kind', sa.String(32), nullable=False), sa.Column('source_key', sa.String(128), nullable=False),
        sa.Column('canonical_source', sa.Text(), nullable=True), sa.Column('completion', sa.String(16), nullable=False),
        stamp('created_at'), stamp('updated_at'),
        sa.CheckConstraint("completion IN ('partial','complete','limited')", name='ck_download_resolution_completion'))
    op.create_table('download_auxiliary_jobs', identifier(),
        sa.Column('session_id', sa.String(64), sa.ForeignKey('download_resolutions.id', ondelete='CASCADE'), nullable=True),
        sa.Column('candidate_id', sa.String(128), sa.ForeignKey('download_candidates.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('kind', sa.String(32), nullable=False), sa.Column('state', sa.String(16), nullable=False),
        sa.Column('completion', sa.String(16), nullable=False), sa.Column('error_code', sa.String(32), nullable=True),
        sa.Column('resource_id', sa.String(64), sa.ForeignKey('download_outputs.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('model_id', sa.String(64), nullable=True),
        sa.Column('output_resource_id', sa.String(64), sa.ForeignKey('download_outputs.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('subtitle_original_id', sa.String(64), sa.ForeignKey('download_subtitle_originals.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('subtitle_draft_id', sa.String(64), sa.ForeignKey('download_subtitle_drafts.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('subtitle_revision', sa.Integer(), nullable=True), sa.Column('subtitle_origin', sa.String(32), nullable=True),
        sa.Column('export_format', sa.String(8), nullable=True),
        sa.Column('input_grant_id', sa.String(64), nullable=True), sa.Column('output_grant_id', sa.String(64), nullable=True),
        sa.Column('category_ids_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('library_id', sa.String(36), sa.ForeignKey('media_assets.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('installed_model_id', sa.String(64), nullable=True),
        stamp('created_at'), stamp('updated_at'),
        sa.CheckConstraint("state IN ('waiting','running','succeeded','failed','skipped','cancelled','interrupted')", name='ck_download_job_state'),
        sa.CheckConstraint("completion IN ('partial','complete','limited')", name='ck_download_job_completion'),
        sa.CheckConstraint("kind IN ('resolve','cover','preview','region','resource_import','resource_preview','resource_reveal','resource_delete','subtitle_export','model_download','model_import')", name='ck_download_job_kind'),
        sa.CheckConstraint('subtitle_revision IS NULL OR subtitle_revision >= 0', name='ck_download_job_revision'),
        sa.CheckConstraint('installed_model_id IS NULL OR installed_model_id = model_id', name='ck_download_job_model_result'),
        sa.ForeignKeyConstraint(['subtitle_draft_id', 'subtitle_revision'], ['download_subtitle_revisions.draft_id', 'download_subtitle_revisions.revision'], ondelete='RESTRICT'))
    op.create_table('download_items', identifier(size=36),
        sa.Column('task_id', sa.String(64), sa.ForeignKey('task_center_records.id', ondelete='CASCADE'), nullable=False),
        candidate_reference(), sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('snapshot_json', sa.Text(), nullable=False), stamp('created_at'),
        sa.UniqueConstraint('task_id', 'candidate_id', name='uq_download_task_candidate'),
        sa.UniqueConstraint('id', 'candidate_id', name='uq_download_item_candidate'))
    op.create_index('ix_download_items_task_id', 'download_items', ['task_id'])
    op.create_table('download_outputs', identifier(), candidate_reference(),
        sa.Column('item_id', sa.String(36), nullable=True), item_ownership(),
        sa.Column('kind', sa.String(32), nullable=False), sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False), sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('backend_path', sa.Text(), nullable=False), stamp('created_at'),
        sa.CheckConstraint('size_bytes >= 0', name='ck_download_output_size'),
        sa.CheckConstraint('duration_ms IS NULL OR duration_ms >= 0', name='ck_download_output_duration'))
    op.create_table('download_stages', identifier(size=36),
        sa.Column('item_id', sa.String(36), sa.ForeignKey('download_items.id', ondelete='CASCADE'), nullable=False),
        sa.Column('stage', sa.String(32), nullable=False), sa.Column('required_kind', sa.String(32), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('resource_id', sa.String(64), sa.ForeignKey('download_outputs.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('error_code', sa.String(32), nullable=True), stamp('updated_at'),
        sa.UniqueConstraint('item_id', 'stage', name='uq_download_item_stage'),
        sa.CheckConstraint("state IN ('waiting','running','succeeded','failed','skipped','cancelled','interrupted')", name='ck_download_stage_state'))
    op.create_index('ix_download_stages_item_id', 'download_stages', ['item_id'])
    op.create_table('download_sources', identifier(size=36), candidate_reference('CASCADE'),
        sa.Column('session_id', sa.String(64), sa.ForeignKey('download_resolutions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('source_kind', sa.String(32), nullable=False), sa.Column('source_key', sa.String(128), nullable=False),
        sa.Column('canonical_source', sa.Text(), nullable=True), stamp('created_at'),
        sa.UniqueConstraint('candidate_id', 'source_key', name='uq_download_candidate_source'))
    op.create_table('download_subtitle_originals', identifier(), candidate_reference(),
        sa.Column('item_id', sa.String(36), nullable=True), item_ownership(),
        sa.Column('origin', sa.String(32), nullable=False), sa.Column('segments_json', sa.Text(), nullable=False), stamp('created_at'),
        sa.CheckConstraint("origin IN ('platform_caption','embedded_caption','asr','ocr')", name='ck_download_subtitle_origin'))
    op.create_table('download_subtitle_drafts', identifier(),
        sa.Column('original_id', sa.String(64), sa.ForeignKey('download_subtitle_originals.id', ondelete='CASCADE'), nullable=False),
        stamp('created_at'))
    op.create_table('download_subtitle_revisions', identifier(size=36),
        sa.Column('draft_id', sa.String(64), sa.ForeignKey('download_subtitle_drafts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('segments_json', sa.Text(), nullable=False), stamp('created_at'),
        sa.UniqueConstraint('draft_id', 'revision', name='uq_download_draft_revision'),
        sa.CheckConstraint('revision > 0', name='ck_download_revision_positive'))
    op.create_table('download_decisions', identifier(size=36),
        sa.Column('item_id', sa.String(36), sa.ForeignKey('download_items.id', ondelete='CASCADE'), nullable=False),
        sa.Column('decision', sa.String(16), nullable=False), stamp('created_at'),
        sa.CheckConstraint("decision IN ('original','skip')", name='ck_download_decision'))
    # SQLite is the application's storage backend. Freeze only download records;
    # no PRAGMA or trigger changes the legacy modules' write/delete semantics.
    for table in ('download_items', 'download_outputs', 'download_subtitle_originals', 'download_subtitle_revisions', 'download_decisions'):
        op.execute(f"CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'immutable download record'); END")
    op.execute("CREATE TRIGGER immutable_download_task_snapshot BEFORE UPDATE OF snapshot_json,kind ON task_center_records WHEN OLD.kind='download' AND (NEW.snapshot_json IS NOT OLD.snapshot_json OR NEW.kind IS NOT OLD.kind) BEGIN SELECT RAISE(ABORT, 'immutable download snapshot'); END")
    op.execute("CREATE TRIGGER sequential_download_revision BEFORE INSERT ON download_subtitle_revisions WHEN NEW.revision != COALESCE((SELECT MAX(revision) FROM download_subtitle_revisions WHERE draft_id=NEW.draft_id),0)+1 BEGIN SELECT RAISE(ABORT, 'nonsequential subtitle revision'); END")
    inputs = ('kind', 'session_id', 'candidate_id', 'model_id', 'output_resource_id', 'subtitle_original_id', 'subtitle_draft_id', 'subtitle_revision', 'subtitle_origin', 'export_format', 'input_grant_id', 'output_grant_id', 'category_ids_json')
    changed = ' OR '.join(f'NEW.{field} IS NOT OLD.{field}' for field in inputs)
    op.execute(f"CREATE TRIGGER immutable_download_job_inputs BEFORE UPDATE ON download_auxiliary_jobs WHEN {changed} BEGIN SELECT RAISE(ABORT, 'immutable download job input'); END")


def downgrade():
    op.execute('DROP TRIGGER IF EXISTS immutable_download_task_snapshot')
    op.execute('DROP TRIGGER IF EXISTS sequential_download_revision')
    for table in ('download_decisions', 'download_auxiliary_jobs', 'download_subtitle_revisions', 'download_subtitle_drafts', 'download_subtitle_originals',
                  'download_sources', 'download_stages', 'download_outputs', 'download_items',
                  'download_resolutions', 'download_candidates'):
        op.drop_table(table)
