from sqlalchemy import inspect

from yijianjifa.storage.db import create_engine_for, migrate_to_head


def test_initial_schema_contains_library_and_history_tables(tmp_path):
    engine = create_engine_for(tmp_path / "app.db")
    migrate_to_head(engine)
    names = set(inspect(engine).get_table_names())
    assert {
        "media_assets",
        "copy_records",
        "categories",
        "category_links",
        "usage_progress",
        "historical_identities",
        "settings",
    } <= names
