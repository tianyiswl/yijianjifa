import shutil
import sys
from pathlib import Path
from sqlalchemy import inspect
from yijianjifa.storage.db import create_engine_for,migrate_to_head


def test_database_migrates_using_only_frozen_bundle_resources(tmp_path,monkeypatch):
    bundle=tmp_path/'bundle';bundle.mkdir()
    shutil.copy('alembic.ini',bundle/'alembic.ini')
    shutil.copytree('alembic',bundle/'alembic',ignore=shutil.ignore_patterns('__pycache__'))
    monkeypatch.setattr(sys,'_MEIPASS',str(bundle),raising=False)
    from yijianjifa.storage import db
    # Simulate PyInstaller's module path, outside the development checkout.
    monkeypatch.setattr(db,'__file__',str(bundle/'yijianjifa/storage/db.py'))
    engine=create_engine_for(tmp_path/'user-data/app.db')
    try:
        migrate_to_head(engine)
        assert {'mix_tasks','finished_videos','settings'}<=set(inspect(engine).get_table_names())
        migrate_to_head(engine)
    finally:engine.dispose()
