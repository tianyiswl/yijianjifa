"""Developer-only read facade; never bootstrap/migrate or repair a managed source.

WAL databases and sidecars are deliberately unsupported. The human must close the
source client for this standalone validation (its account mutex is process-local).
"""
from pathlib import Path
import sqlite3
from sqlalchemy import create_engine, select
from sqlalchemy.pool import NullPool
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.accounts.session_store import SessionStore
from .account_session import ManagedDownloadSessions
from .contracts import DownloadError


class ReadOnlySessionStore(SessionStore):
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink() or not self.root.is_dir():
            raise DownloadError('session_missing')

    def put(self, *args, **kwargs): raise DownloadError('unsupported_source')
    def delete(self, *args, **kwargs): raise DownloadError('unsupported_source')
    def ensure_profile_dir(self, *args, **kwargs): raise DownloadError('unsupported_source')

    def get(self, *args, **kwargs):
        if self.root.is_symlink() or not self.root.is_dir(): raise DownloadError('session_invalid')
        return super().get(*args, **kwargs)


class ReadOnlyAccounts:
    def __init__(self, engine):
        self.service = AccountService(engine)
        self.engine = engine

    def default(self, platform):
        with self.engine.connect() as connection:
            rows = connection.execute(select(AccountRow.id).where(
                AccountRow.platform == platform, AccountRow.is_default.is_(True))).fetchmany(2)
        if len(rows) != 1: raise DownloadError('account_unavailable')
        return self.service.get(rows[0][0])

    def get(self, account_id): return self.service.get(account_id)


def open_managed_sessions(data_dir, *, decoder=None):
    root = Path(data_dir)
    db = root / 'app.db'

    def validate_source():
        if root.is_symlink() or not root.is_dir() or db.is_symlink() or not db.is_file():
            raise DownloadError('account_unavailable')
        if any(Path(str(db) + suffix).exists() or Path(str(db) + suffix).is_symlink()
               for suffix in ('-wal', '-shm', '-journal')):
            raise DownloadError('account_unavailable')
        with db.open('rb') as stream: header = stream.read(100)
        if len(header) != 100 or header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01':
            raise DownloadError('account_unavailable')

    engine = None
    try:
        validate_source()
        store = ReadOnlySessionStore(root / 'profiles')
        def connect():
            validate_source()  # Recheck before every read connection; never enable WAL.
            return sqlite3.connect(db.absolute().as_uri() + '?mode=ro', uri=True)
        engine = create_engine('sqlite://', creator=connect, poolclass=NullPool)
        sessions = ManagedDownloadSessions(ReadOnlyAccounts(engine), store, decoder=decoder)
        return sessions, engine
    except Exception:
        if engine is not None: engine.dispose()
        raise DownloadError('account_unavailable') from None
