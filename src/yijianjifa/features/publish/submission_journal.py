"""Durable final-action marker for a future trusted execution coordinator.

Keys must identify a stable confirmed task item, never a freshly generated retry.
No transport, session data or operation capable of clearing a marker is exposed.
"""
import hashlib
import json
import re
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


class SubmissionAlreadyAttempted(RuntimeError):
    pass


class SubmissionJournal:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def _key(item_key):
        if not isinstance(item_key, str) or not item_key.strip() or len(item_key)>512:
            raise ValueError('invalid submission item key')
        return 'publish.final.' + hashlib.sha256(item_key.encode()).hexdigest()

    def get(self, item_key):
        with session_scope(self.engine) as session:
            row = session.get(Setting, self._key(item_key))
            return json.loads(row.value_json) if row else None

    def mark_before_final(self, item_key, request_digest):
        key = self._key(item_key)
        if not isinstance(request_digest, str) or not re.fullmatch('[0-9a-f]{64}', request_digest):
            raise ValueError('invalid frozen request digest')
        with self.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                if connection.execute(select(Setting.key).where(Setting.key==key)).first():
                    raise SubmissionAlreadyAttempted('submission requires reconciliation')
                with session_scope(connection) as session:
                    session.add(Setting(key=key,value_json=json.dumps(dict(
                        state='result_unknown',request_digest=request_digest))))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
