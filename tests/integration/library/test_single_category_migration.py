"""Exercise schema changes only on temporary SQLite databases."""
from __future__ import annotations

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError

from yijianjifa.storage.db import (
    create_engine_for, downgrade_to_revision, migrate_to_head, migrate_to_revision,
)
from yijianjifa.storage.models import Base, Category, CategoryLink


PREVIOUS = "0018_image_post_deliveries"
CURRENT = "0019_single_category"
BACKUP = "category_link_migration_backups"
INDEX = "uq_category_target_single"


def _rows(connection, table):
    return sorted([dict(row) for row in connection.execute(text(f'SELECT * FROM "{table}"')).mappings()], key=repr)


def _business_snapshot(connection):
    return {
        name: _rows(connection, name) for name in inspect(connection).get_table_names()
        if name not in {"alembic_version", "category_links", BACKUP}
    }


@pytest.fixture
def legacy_database(tmp_path):
    engine = create_engine_for(tmp_path / "legacy.db")
    migrate_to_revision(engine, PREVIOUS)
    with engine.begin() as connection:
        connection.execute(text("""INSERT INTO media_assets
            (id,kind,display_name,original_path,content_hash,missing,created_at,updated_at)
            VALUES ('media','video','素材','/isolated/original.mp4','original-hash',0,'2026-10-01','2026-10-01')"""))
        connection.execute(text("""INSERT INTO copy_records
            (id,title,voice,body,tags,full,created_at,updated_at)
            VALUES ('copy','文案','口播','正文','#标签','全文','2026-10-01','2026-10-01')"""))
        connection.execute(text("""INSERT INTO finished_videos
            (id,origin_type,display_name,content_hash,duration_ms,title,body,tags,task_locked,created_at,updated_at)
            VALUES ('finished','external','成品','finished-hash',1000,'标题','正文','#标签',0,'2026-10-01','2026-10-01')"""))
        connection.execute(text("""INSERT INTO task_center_records
            (id,kind,name,state,snapshot_json,queue_order,created_at,updated_at)
            VALUES ('task','mixing','原任务','paused','{"category_ids":["video-old","video-new"]}',0,'2026-10-01','2026-10-01')"""))
        # Identical target IDs in different content types are distinct targets.
        for kind in ("video", "music", "audio", "copy", "finished"):
            connection.execute(text("""INSERT INTO categories(id,kind,name,created_at)
                VALUES (:id,:kind,:name,:created_at)"""), [
                    {"id": f"{kind}-new", "kind": kind, "name": "后建分类", "created_at": "2026-10-02 00:00:00.123456"},
                    {"id": f"{kind}-old", "kind": kind, "name": "先建分类", "created_at": "2026-10-01 00:00:00.654321"},
                ])
            connection.execute(text("""INSERT INTO category_links(id,category_id,target_type,target_id)
                VALUES (:id,:category_id,:kind,'same-content-id')"""), [
                    {"id": f"{kind}-link-new", "category_id": f"{kind}-new", "kind": kind},
                    {"id": f"{kind}-link-old", "category_id": f"{kind}-old", "kind": kind},
                ])
        connection.execute(text("""INSERT INTO categories(id,kind,name,created_at)
            VALUES ('video-tie-a','video','并列A','2026-09-01'),('video-tie-b','video','并列B','2026-09-01')"""))
        connection.execute(text("""INSERT INTO category_links(id,category_id,target_type,target_id) VALUES
            ('tie-link-z','video-tie-a','video','tie-content'),
            ('tie-link-a','video-tie-b','video','tie-content'),
            ('single-link','video-new','video','already-single')"""))
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_keeps_earliest_category_and_complete_local_recovery(legacy_database):
    engine = legacy_database
    with engine.connect() as connection:
        before = _business_snapshot(connection)
        original = {row["id"]: row for row in _rows(connection, "category_links")}
    migrate_to_head(engine)
    with engine.connect() as connection:
        assert _business_snapshot(connection) == before
        live = {row["id"]: row for row in _rows(connection, "category_links")}
        assert set(live) == {f"{kind}-link-old" for kind in ("video", "music", "audio", "copy", "finished")} | {"tie-link-z", "single-link"}
        archived = _rows(connection, BACKUP)
        assert {row["link_id"] for row in archived} == set(original) - set(live)
        for row in archived:
            assert {column: row[column] for column in ("category_id", "target_type", "target_id")} == {
                column: original[row["link_id"]][column] for column in ("category_id", "target_type", "target_id")
            }
            assert row["migration_revision"] == CURRENT
            assert row["retained_link_id"] in live
            assert row["category_name"] in {"后建分类", "并列B"}
            assert row["category_created_at"]
            assert row["archived_at"] and row["restored_at"] is None
        assert inspect(connection).get_foreign_keys(BACKUP) == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == CURRENT
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("""INSERT INTO category_links(id,category_id,target_type,target_id)
            VALUES ('forbidden','video-new','video','same-content-id')"""))


def test_downgrade_restores_every_original_link_and_repeat_upgrade_is_safe(legacy_database):
    engine = legacy_database
    with engine.connect() as connection:
        original_links = _rows(connection, "category_links")
        original_business = _business_snapshot(connection)
    migrate_to_head(engine)
    downgrade_to_revision(engine, PREVIOUS)
    with engine.connect() as connection:
        assert _rows(connection, "category_links") == original_links
        assert _business_snapshot(connection) == original_business
        restored = _rows(connection, BACKUP)
        assert len(restored) == 6 and all(row["restored_at"] for row in restored)
        assert INDEX not in {index["name"] for index in inspect(connection).get_indexes("category_links")}
    migrate_to_head(engine)
    with engine.connect() as connection:
        assert len(_rows(connection, BACKUP)) == 12
        assert sum(row["restored_at"] is None for row in _rows(connection, BACKUP)) == 6
        assert all(count == 1 for count in connection.exec_driver_sql("SELECT count(*) FROM category_links GROUP BY target_type,target_id").scalars())
    downgrade_to_revision(engine, PREVIOUS)
    with engine.connect() as connection:
        assert _rows(connection, "category_links") == original_links


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_failed_upgrade_rolls_back_archive_links_schema_and_revision(legacy_database, foreign_keys):
    engine = legacy_database
    with engine.connect() as connection:
        before = _rows(connection, "category_links")
        schema = connection.exec_driver_sql("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").all()
        connection.exec_driver_sql(f"PRAGMA foreign_keys={foreign_keys}")
        connection.commit()

    def fail_after_archive(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(f"CREATE UNIQUE INDEX {INDEX}"):
            raise RuntimeError("injected index failure")

    event.listen(engine, "before_cursor_execute", fail_after_archive)
    try:
        with pytest.raises(RuntimeError, match="injected index failure"):
            migrate_to_head(engine)
    finally:
        event.remove(engine, "before_cursor_execute", fail_after_archive)
    with engine.connect() as connection:
        assert _rows(connection, "category_links") == before
        assert connection.exec_driver_sql("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").all() == schema
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == PREVIOUS
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == foreign_keys
    # The same database can be upgraded after the fault is removed.
    migrate_to_head(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == foreign_keys
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_failed_downgrade_rolls_back_restoration_and_unique_index(legacy_database):
    engine = legacy_database
    migrate_to_head(engine)
    with engine.connect() as connection:
        before_links = _rows(connection, "category_links")
        before_backup = _rows(connection, BACKUP)

    def fail_during_restore(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO category_links"):
            raise RuntimeError("injected restore failure")

    event.listen(engine, "before_cursor_execute", fail_during_restore)
    try:
        with pytest.raises(RuntimeError, match="injected restore failure"):
            downgrade_to_revision(engine, PREVIOUS)
    finally:
        event.remove(engine, "before_cursor_execute", fail_during_restore)
    with engine.connect() as connection:
        assert _rows(connection, "category_links") == before_links
        assert _rows(connection, BACKUP) == before_backup
        assert INDEX in {index["name"] for index in inspect(connection).get_indexes("category_links")}
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == CURRENT


@pytest.mark.parametrize("conflict", ["deleted_category", "reused_link_id"])
def test_downgrade_never_recreates_deleted_category_or_overwrites_id(legacy_database, conflict):
    engine = legacy_database
    migrate_to_head(engine)
    with engine.begin() as connection:
        if conflict == "deleted_category":
            connection.execute(text("DELETE FROM category_links WHERE category_id='video-new'"))
            connection.execute(text("DELETE FROM categories WHERE id='video-new'"))
            # No FK on backups: recovery evidence survives category deletion.
        else:
            connection.execute(text("""INSERT INTO category_links(id,category_id,target_type,target_id)
                VALUES ('video-link-new','video-old','video','new-user-content')"""))
        before = _rows(connection, "category_links")
        archive = _rows(connection, BACKUP)
    with pytest.raises(RuntimeError, match="local backup retained"):
        downgrade_to_revision(engine, PREVIOUS)
    with engine.connect() as connection:
        assert _rows(connection, "category_links") == before
        assert _rows(connection, BACKUP) == archive
        assert INDEX in {index["name"] for index in inspect(connection).get_indexes("category_links")}
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == CURRENT


def test_model_create_all_also_prevents_multiple_categories(tmp_path):
    engine = create_engine_for(tmp_path / "model.db")
    try:
        # Create only the tables needed here; no dependent feature imports.
        Base.metadata.create_all(engine, tables=[Category.__table__, CategoryLink.__table__])
        with engine.begin() as connection:
            connection.execute(text("""INSERT INTO categories(id,kind,name,created_at) VALUES
                ('a','video','A','2026-10-01'),('b','video','B','2026-10-01')"""))
            connection.execute(text("""INSERT INTO category_links(id,category_id,target_type,target_id)
                VALUES ('first','a','video','same')"""))
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text("""INSERT INTO category_links(id,category_id,target_type,target_id)
                VALUES ('second','b','video','same')"""))
    finally:
        engine.dispose()
