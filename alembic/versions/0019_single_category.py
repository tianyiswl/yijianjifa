"""Keep one category per content item and retain every removed association locally."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0019_single_category"
down_revision = "0018_image_post_deliveries"
branch_labels = None
depends_on = None

BACKUP_TABLE = "category_link_migration_backups"
SINGLE_INDEX = "uq_category_target_single"


def _backup_table(bind):
    metadata = sa.MetaData()
    table = sa.Table(
        BACKUP_TABLE, metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("migration_revision", sa.String(32), nullable=False),
        sa.Column("link_id", sa.String(36), nullable=False),
        sa.Column("category_id", sa.String(36), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("category_kind", sa.String(16), nullable=True),
        sa.Column("category_name", sa.String(128), nullable=True),
        sa.Column("category_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retained_link_id", sa.String(36), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
    )
    table.create(bind, checkfirst=True)
    return table


def _require_transaction(bind):
    if bind.dialect.name == "sqlite" and not bind.connection.driver_connection.in_transaction:
        raise RuntimeError("Single-category migration requires the guarded migration runner")


def upgrade() -> None:
    bind = op.get_bind()
    _require_transaction(bind)
    backups = _backup_table(bind)
    metadata = sa.MetaData()
    links = sa.Table("category_links", metadata, autoload_with=bind)
    categories = sa.Table("categories", metadata, autoload_with=bind)
    rows = list(bind.execute(
        sa.select(
            links,
            categories.c.kind.label("category_kind"),
            categories.c.name.label("category_name"),
            categories.c.created_at.label("category_created_at"),
        )
        .outerjoin(categories, categories.c.id == links.c.category_id)
        .order_by(
            links.c.target_type, links.c.target_id,
            categories.c.created_at.asc().nulls_last(),
            links.c.category_id, links.c.id,
        )
    ).mappings())
    retained = {}
    archived_at = datetime.now(timezone.utc)
    for row in rows:
        target = (row["target_type"], row["target_id"])
        if target not in retained:
            retained[target] = row["id"]
            continue
        bind.execute(backups.insert().values(
            id=str(uuid.uuid4()), migration_revision=revision,
            link_id=row["id"], category_id=row["category_id"],
            target_type=row["target_type"], target_id=row["target_id"],
            category_kind=row["category_kind"], category_name=row["category_name"],
            category_created_at=row["category_created_at"],
            retained_link_id=retained[target], archived_at=archived_at,
        ))
        bind.execute(links.delete().where(links.c.id == row["id"]))
    op.create_index(SINGLE_INDEX, "category_links", ["target_type", "target_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    _require_transaction(bind)
    backups = _backup_table(bind)
    metadata = sa.MetaData()
    links = sa.Table("category_links", metadata, autoload_with=bind)
    categories = sa.Table("categories", metadata, autoload_with=bind)
    rows = list(bind.execute(
        sa.select(backups).where(
            backups.c.migration_revision == revision,
            backups.c.restored_at.is_(None),
        ).order_by(backups.c.archived_at, backups.c.id)
    ).mappings())
    # Never resurrect a category the user has since deleted or overwrite a link
    # whose ID has been reused. Abort atomically, retaining the recovery evidence.
    for row in rows:
        if bind.execute(sa.select(categories.c.id).where(categories.c.id == row["category_id"])).first() is None:
            raise RuntimeError("Cannot restore category associations: an archived category was deleted; local backup retained")
        current = bind.execute(sa.select(links).where(links.c.id == row["link_id"])).mappings().first()
        if current is not None and any(current[column] != row[column] for column in ("category_id", "target_type", "target_id")):
            raise RuntimeError("Cannot restore category associations: a link ID was reused; local backup retained")
    op.drop_index(SINGLE_INDEX, table_name="category_links")
    restored_at = datetime.now(timezone.utc)
    for row in rows:
        exists = bind.execute(sa.select(links.c.id).where(
            links.c.category_id == row["category_id"],
            links.c.target_type == row["target_type"],
            links.c.target_id == row["target_id"],
        )).first()
        if exists is None:
            bind.execute(links.insert().values(
                id=row["link_id"], category_id=row["category_id"],
                target_type=row["target_type"], target_id=row["target_id"],
            ))
        bind.execute(backups.update().where(backups.c.id == row["id"]).values(restored_at=restored_at))
    # Keep the complete local archive, even after a successful downgrade.
