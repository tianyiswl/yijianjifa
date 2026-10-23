from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import time


class Store:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, recovery_hash TEXT,
                    credits INTEGER NOT NULL DEFAULT 0 CHECK(credits >= 0),
                    used INTEGER NOT NULL DEFAULT 0 CHECK(used >= 0),
                    reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0),
                    created_at REAL NOT NULL, CHECK(credits >= used + reserved)
                );
                CREATE TABLE IF NOT EXISTS ledger (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                    event_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    delta INTEGER NOT NULL, available_after INTEGER NOT NULL,
                    purpose TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ledger_account_time ON ledger(account_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, family TEXT NOT NULL,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    access_hash TEXT NOT NULL UNIQUE, refresh_hash TEXT NOT NULL UNIQUE,
                    access_expires REAL NOT NULL, refresh_expires REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);
                CREATE TABLE IF NOT EXISTS redemption_codes (
                    id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE, suffix TEXT NOT NULL,
                    batch_id TEXT NOT NULL, order_ref TEXT NOT NULL,
                    price_fen INTEGER NOT NULL CHECK(price_fen=1990),
                    words INTEGER NOT NULL CHECK(words=10000),
                    created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    redeemed_by TEXT REFERENCES accounts(id), redeemed_at REAL
                );
                CREATE TABLE IF NOT EXISTS redemptions (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                    idempotency_key TEXT NOT NULL, code_hash TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    UNIQUE(account_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS code_purchase_notes (
                    order_ref TEXT PRIMARY KEY, note TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS code_issuance_orders (
                    order_ref TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE,
                    code_count INTEGER NOT NULL CHECK(code_count BETWEEN 1 AND 500),
                    created_at REAL NOT NULL
                );
            ''')
            # Nullable additive migration: old hashes remain redeemable, but
            # cannot be reconstructed or silently replaced by newly issued codes.
            conn.execute('BEGIN IMMEDIATE')
            try:
                columns = {row['name'] for row in conn.execute('PRAGMA table_info(redemption_codes)')}
                if 'encrypted_code' not in columns:
                    conn.execute('ALTER TABLE redemption_codes ADD COLUMN encrypted_code TEXT')
                if 'delivered_at' not in columns:
                    conn.execute('ALTER TABLE redemption_codes ADD COLUMN delivered_at REAL')
                conn.execute('CREATE INDEX IF NOT EXISTS redemption_order ON redemption_codes(order_ref)')
                conn.execute('CREATE INDEX IF NOT EXISTS redemption_time ON redemption_codes(created_at DESC,id DESC)')
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
        os.chmod(self.path, 0o600)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('PRAGMA synchronous=FULL')
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
