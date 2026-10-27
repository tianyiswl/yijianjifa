from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from typing import Iterator

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_engine_for(path: str | Path) -> Engine:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", future=True)


def _alembic_config() -> Config:
    root = Path(sys._MEIPASS) if getattr(sys,"_MEIPASS",None) else Path(__file__).resolve().parents[3]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


def _run_migrations(engine: Engine, revision: str, *, downgrade: bool = False) -> None:
    cfg = _alembic_config()
    with engine.connect() as connection:
        scripts = ScriptDirectory.from_config(cfg)
        current = MigrationContext.configure(connection).get_current_heads()
        if downgrade:
            planned = scripts.iterate_revisions(current, revision)
        else:
            planned = scripts.iterate_revisions(revision, current)
        local_copy = connection.dialect.name == "sqlite" and any(
            step.revision in {"0015_local_video", "0018_image_post_deliveries", "0019_single_category"} for step in planned
        )
        # This is our fresh, exclusively owned connection. Reading the revision
        # can create a SQLAlchemy logical transaction, but has made no writes.
        connection.rollback()
        dbapi = connection.connection.driver_connection
        previous = bool(dbapi.execute("PRAGMA foreign_keys").fetchone()[0]) if local_copy else None
        try:
            if local_copy:
                # SQLite parent-table replacement requires enforcement disabled
                # before BEGIN. Do not change any engine/pool default or commit
                # within an Alembic migration. Explicit BEGIN also makes SQLite
                # DDL rollback with the rest of this isolated migration packet.
                dbapi.execute("PRAGMA foreign_keys=OFF")
            with connection.begin():
                if local_copy:
                    dbapi.execute("BEGIN")
                cfg.attributes["connection"] = connection
                (command.downgrade if downgrade else command.upgrade)(cfg, revision)
                if local_copy and connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                    raise RuntimeError("Local video migration preserved an invalid reference")
        finally:
            if previous is not None:
                # The transaction context has already committed or rolled back.
                # Restore on this exact connection before it is returned, not
                # via a listener waiting for a later pool/transaction event.
                dbapi.execute("PRAGMA foreign_keys=ON" if previous else "PRAGMA foreign_keys=OFF")


def migrate_to_revision(engine: Engine, revision: str) -> None:
    _run_migrations(engine, revision)


def downgrade_to_revision(engine: Engine, revision: str) -> None:
    _run_migrations(engine, revision, downgrade=True)


def migrate_to_head(engine: Engine) -> None:
    migrate_to_revision(engine, "head")


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
