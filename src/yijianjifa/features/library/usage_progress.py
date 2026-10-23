from __future__ import annotations

import json
import random as random_module
from typing import Iterable, Protocol

from sqlalchemy import Engine, func, select

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting, UsageProgress

_ALLOWED_SCOPES = {"mixing", "finished_metadata"}


class RandomChoice(Protocol):
    def choice(self, sequence): ...


class UsageProgressService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def mark_used(self, scope: str, copy_id: str) -> None:
        self._validate_scope(scope)
        with session_scope(self.engine) as session:
            cycle = self._cycle(session, scope)
            max_order = session.scalar(
                select(func.max(UsageProgress.used_order)).where(
                    UsageProgress.scope == scope,
                    UsageProgress.cycle == cycle,
                )
            )
            row = session.scalar(
                select(UsageProgress).where(
                    UsageProgress.scope == scope,
                    UsageProgress.copy_id == copy_id,
                )
            )
            next_order = (max_order or 0) + 1
            if row is None:
                session.add(
                    UsageProgress(
                        scope=scope,
                        copy_id=copy_id,
                        cycle=cycle,
                        used_order=next_order,
                    )
                )
            elif row.cycle != cycle:
                row.cycle = cycle
                row.used_order = next_order

    def is_used(self, scope: str, copy_id: str) -> bool:
        self._validate_scope(scope)
        with session_scope(self.engine) as session:
            cycle = self._cycle(session, scope)
            row = session.scalar(
                select(UsageProgress).where(
                    UsageProgress.scope == scope,
                    UsageProgress.copy_id == copy_id,
                )
            )
            return row is not None and row.cycle == cycle

    def next_copy(
        self,
        scope: str,
        candidate_ids: Iterable[str],
        mode: str,
        *,
        rng: RandomChoice | None = None,
    ) -> str | None:
        self._validate_scope(scope)
        candidates = list(dict.fromkeys(candidate_ids))
        if not candidates:
            return None
        if mode not in {"sequential", "random"}:
            raise ValueError("mode must be 'sequential' or 'random'")

        with session_scope(self.engine) as session:
            cycle = self._cycle(session, scope)
            used = set(
                session.scalars(
                    select(UsageProgress.copy_id).where(
                        UsageProgress.scope == scope,
                        UsageProgress.cycle == cycle,
                        UsageProgress.copy_id.in_(candidates),
                    )
                ).all()
            )
            remaining = [copy_id for copy_id in candidates if copy_id not in used]
            if not remaining:
                cycle += 1
                self._set_cycle(session, scope, cycle)
                remaining = candidates
            if mode == "sequential":
                return remaining[0]
            chooser = rng or random_module
            return chooser.choice(remaining)

    def reset_cycle(self, scope: str) -> None:
        self._validate_scope(scope)
        with session_scope(self.engine) as session:
            self._set_cycle(session, scope, self._cycle(session, scope) + 1)

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope not in _ALLOWED_SCOPES:
            raise ValueError(f"unknown usage progress scope: {scope}")

    @staticmethod
    def _setting_key(scope: str) -> str:
        return f"usage_progress:{scope}:cycle"

    def _cycle(self, session, scope: str) -> int:
        row = session.get(Setting, self._setting_key(scope))
        if row is None:
            return 0
        return int(json.loads(row.value_json))

    def _set_cycle(self, session, scope: str, cycle: int) -> None:
        key = self._setting_key(scope)
        row = session.get(Setting, key)
        value = json.dumps(cycle)
        if row is None:
            session.add(Setting(key=key, value_json=value))
        else:
            row.value_json = value
