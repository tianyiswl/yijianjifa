from __future__ import annotations

from sqlalchemy import Engine, select

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import HistoricalIdentity


_KIND = "mix:composition"


class CompositionHistoryRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def remember(self, fingerprint: str) -> None:
        value = str(fingerprint).strip()
        if not value:
            raise ValueError("composition fingerprint cannot be blank")
        with session_scope(self.engine) as session:
            existing = session.scalar(
                select(HistoricalIdentity).where(
                    HistoricalIdentity.identity_kind == _KIND,
                    HistoricalIdentity.content_hash == value,
                )
            )
            if existing is None:
                session.add(HistoricalIdentity(identity_kind=_KIND, content_hash=value))

    def seen(self, fingerprint: str) -> bool:
        with session_scope(self.engine) as session:
            value = session.scalar(
                select(HistoricalIdentity.id).where(
                    HistoricalIdentity.identity_kind == _KIND,
                    HistoricalIdentity.content_hash == fingerprint,
                ).limit(1)
            )
            return value is not None

    def all_fingerprints(self) -> set[str]:
        with session_scope(self.engine) as session:
            return set(
                session.scalars(
                    select(HistoricalIdentity.content_hash).where(
                        HistoricalIdentity.identity_kind == _KIND
                    )
                ).all()
            )
