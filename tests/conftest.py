from contextlib import closing
from pathlib import Path
from shutil import copyfile
import sqlite3
import subprocess

import pytest

from yijianjifa.os_integration.paths import AppPaths
from yijianjifa.storage.db import create_engine_for, migrate_to_head


@pytest.fixture(scope='session')
def synthetic_mp3(tmp_path_factory):
    """A local sine tone, never a supplier sample or a paid voice request."""
    from yijianjifa.os_integration.resources import ffmpeg_path
    output = tmp_path_factory.mktemp('synthetic-audio') / 'tone.mp3'
    subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000:duration=1',
                    '-c:a', 'libmp3lame', '-b:a', '64k', str(output)],
                   check=True, timeout=30, capture_output=True)
    return output


@pytest.fixture
def app_paths(tmp_path: Path) -> AppPaths:
    data = tmp_path / "appdata"
    paths = AppPaths(
        data_dir=data,
        cache_dir=data / "cache",
        logs_dir=data / "logs",
        managed_media_dir=data / "managed_media",
    )
    for path in (paths.data_dir, paths.cache_dir, paths.logs_dir, paths.managed_media_dir):
        path.mkdir(parents=True, exist_ok=True)
    return paths


@pytest.fixture(scope="session")
def _migrated_database_template(tmp_path_factory):
    """Migrate this process's source once; never share its writable engine."""
    path = tmp_path_factory.mktemp("database-template") / "template.db"
    engine = create_engine_for(path)
    try:
        migrate_to_head(engine)
    finally:
        engine.dispose()
    # A main-file copy cannot include live WAL/journal transactions. Keep this
    # a closed, freshly migrated database; never reuse a prior run's template.
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise RuntimeError("Test database template must use delete journal mode")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise RuntimeError("Test database template has an active journal sidecar")
    return path


@pytest.fixture
def db_engine(tmp_path: Path, _migrated_database_template):
    path = tmp_path / "test.db"
    copyfile(_migrated_database_template, path)
    engine = create_engine_for(path)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def media_service(db_engine, app_paths):
    from yijianjifa.features.library.media_storage import MediaStorageService

    return MediaStorageService(db_engine, app_paths, kind="video")

@pytest.fixture
def copy_repo(db_engine):
    from yijianjifa.features.library.copy_service import CopyRepository

    return CopyRepository(db_engine)


@pytest.fixture
def progress(db_engine):
    from yijianjifa.features.library.usage_progress import UsageProgressService

    return UsageProgressService(db_engine)


@pytest.fixture
def daily_auth_sync(monkeypatch):
    """Deterministic fake online IO for existing render/planning contract tests.

    The real asynchronous login job is exercised separately. This fixture still
    goes through both native daily authorization checkpoints and never permits
    a local login flag to stand in for an online observation.
    """
    def configure(api):
        from yijianjifa.features.accounts.service import AccountService
        def start(task_id):
            if not api._daily.auth_current(task_id):
                return False
            try:
                targets=api._daily.auth_begin(task_id)
                for target in targets:
                    if not target.get('deferred_reason'):AccountService(api._engine).set_login_state(target['account_id'],'valid')
                api._daily.auth_accept(task_id,[{**target,'state':'deferred' if target.get('deferred_reason') else 'valid'} for target in targets])
            except Exception as error:
                code=getattr(error,'code','daily_login_check_failed')
                from yijianjifa.app_services.daily_mixing import MESSAGES
                api._daily._record(task_id,auth_phase='needs_attention',reason_code=code,reason=MESSAGES.get(code,code))
            return True
        monkeypatch.setattr(api._daily.auth,'start',start)
        monkeypatch.setattr(api._daily,'wake_making',lambda:None)
        return api
    return configure
