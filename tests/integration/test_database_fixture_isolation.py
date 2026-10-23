"""Consumer contracts for isolated real SQLite fixtures and their teardown."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


CASES = r'''
from pathlib import Path
from sqlalchemy import inspect, text
from yijianjifa.storage.db import create_engine_for, migrate_to_head


def database_state(engine):
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    result = {}
    with engine.connect() as connection:
        for table in tables:
            result[table] = {
                'columns': [(c['name'], str(c['type']), c['nullable'], c['default'], c['primary_key'])
                            for c in inspector.get_columns(table)],
                'primary_key': inspector.get_pk_constraint(table)['constrained_columns'],
                'unique': sorted([tuple(c['column_names']) for c in inspector.get_unique_constraints(table)]),
                'indexes': sorted([(i['name'], tuple(i['column_names']), i['unique'])
                                   for i in inspector.get_indexes(table)]),
                'foreign_keys': sorted([(tuple(k['constrained_columns']), k['referred_table'],
                                        tuple(k['referred_columns']), k['options'])
                                       for k in inspector.get_foreign_keys(table)], key=repr),
                'checks': sorted([(c.get('name'), c['sqltext']) for c in inspector.get_check_constraints(table)], key=repr),
                'rows': connection.exec_driver_sql('SELECT * FROM "' + table.replace('"', '""') + '"').all(),
            }
        result['triggers'] = connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all()
        result['pragmas'] = {name: connection.exec_driver_sql('PRAGMA ' + name).scalar_one()
            for name in ('foreign_keys', 'journal_mode', 'synchronous', 'busy_timeout', 'locking_mode',
                         'wal_autocheckpoint', 'temp_store', 'cache_size', 'read_uncommitted', 'query_only',
                         'recursive_triggers', 'trusted_schema', 'page_size', 'auto_vacuum', 'encoding',
                         'application_id', 'user_version', 'schema_version')}
    return result


def observe_connection(request, engine):
    with engine.connect() as connection:
        request.config.held_connections.append(connection.connection.driver_connection)
    request.config.held_engines.append(engine)


def test_first_database_matches_real_migrations_and_persists_its_commit(db_engine, tmp_path, request):
    control = create_engine_for(tmp_path / 'independently-migrated.db')
    try:
        migrate_to_head(control)
        request.config.initial_database = database_state(control)
        assert database_state(db_engine) == request.config.initial_database
    finally:
        control.dispose()
    observe_connection(request, db_engine)
    request.config.first_database_path = db_engine.url.database
    with db_engine.begin() as connection:
        connection.execute(text("INSERT INTO settings(key,value_json,updated_at) "
                                "VALUES('fixture-isolation-probe',:value,'2026-10-22')"),
                           {'value': '{"committed":true}'})
        connection.exec_driver_sql('PRAGMA user_version=42')
    reopened = create_engine_for(db_engine.url.database)
    try:
        with reopened.connect() as connection:
            assert connection.execute(text("SELECT value_json FROM settings WHERE key='fixture-isolation-probe'"
                                           )).scalar_one() == '{"committed":true}'
    finally:
        reopened.dispose()
    # Enabling one connection's FK flag must not alter another new connection.
    with db_engine.connect() as first, db_engine.connect() as second:
        first.exec_driver_sql('PRAGMA foreign_keys=ON')
        assert first.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
        assert second.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == request.config.initial_database['pragmas']['foreign_keys']


def test_next_database_has_unchanged_defaults_and_no_previous_data(db_engine, request):
    observe_connection(request, db_engine)
    assert db_engine.url.database != request.config.first_database_path
    with db_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM settings WHERE key='fixture-isolation-probe'"
                                       )).scalar_one() == 0, 'FIXTURE_COPY_ISOLATION'
    assert database_state(db_engine) == request.config.initial_database
'''


LAUNCH = r'''
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import pytest

fixture_path, case_path, mode = sys.argv[1:]
spec = importlib.util.spec_from_file_location('real_database_fixture', fixture_path)
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)
migration_calls = []
real_migrate = fixture_module.migrate_to_head
def observe_real_migration(engine):
    real_migrate(engine)
    migration_calls.append(engine.url.database)
    if mode == 'wal':
        with engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA journal_mode=WAL')
fixture_module.migrate_to_head = observe_real_migration
if mode == 'hardlink':
    # Deliberately break only the filesystem boundary; run the actual fixture.
    fixture_module.copyfile = os.link

class VerifyClosedConnections:
    def pytest_configure(self, config):
        config.held_engines = []
        config.held_connections = []
    def pytest_sessionfinish(self, session, exitstatus):
        try:
            if len(migration_calls) != 1:
                print('FIXTURE_TEMPLATE_ONCE: migrations ran', len(migration_calls), 'times')
                session.exitstatus = 1
            for raw in session.config.held_connections:
                try:
                    raw.execute('SELECT 1')
                except sqlite3.ProgrammingError:
                    continue
                print('FIXTURE_ENGINE_DISPOSAL: a pooled connection is still open')
                session.exitstatus = 1
                break
        finally:
            for engine in session.config.held_engines:
                engine.dispose()

raise SystemExit(pytest.main([case_path, '-q', '--confcutdir=' + str(Path(case_path).parent)],
                            plugins=[fixture_module, VerifyClosedConnections()]))
'''


@pytest.mark.parametrize('mode', ['copy', 'hardlink', 'wal'])
def test_real_database_fixture_isolates_consumers_and_closes_connections(tmp_path, mode):
    """Shared-file/hardlink regressions leak the first consumer's marker."""
    root = Path(__file__).resolve().parents[2]
    cases = tmp_path / 'test_fixture_consumers.py'
    cases.write_text(textwrap.dedent(CASES), encoding='utf-8')
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    environment['PYTHONPATH'] = os.pathsep.join([str(root / 'src'), environment.get('PYTHONPATH', '')])
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(LAUNCH),
        str(root / 'tests/conftest.py'), str(cases), mode], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    if mode == 'copy':
        assert result.returncode == 0, output
        assert '2 passed' in output
    elif mode == 'hardlink':
        assert result.returncode == 1, output
        assert 'FIXTURE_COPY_ISOLATION' in output
    else:
        assert result.returncode == 1, output
        assert 'Test database template must use delete journal mode' in output
