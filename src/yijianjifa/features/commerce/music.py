from __future__ import annotations

import random
from typing import Protocol

from yijianjifa.features.commerce.models import FavoriteMusicChoice


class _ChoiceSource(Protocol):
    def choice(self, seq): ...


class FavoriteMusicService:
    """Keeps platform favorite-music candidates isolated by account.

    IDs returned here are platform references only. This service never opens or
    rewrites the finished video, so choosing favorite music cannot alter the
    local media soundtrack.
    """

    def __init__(self, *, rng: _ChoiceSource | None = None):
        self._rng = rng or random.SystemRandom()
        self._by_account: dict[str, tuple[str, ...]] = {}

    def seed(self, account_id: str, music_ids: list[str] | tuple[str, ...]) -> None:
        self._by_account[str(account_id)] = tuple(dict.fromkeys(str(mid) for mid in music_ids))

    def ids_for(self, account_id: str) -> tuple[str, ...]:
        return self._by_account.get(str(account_id), ())

    def pick(self, account_id: str, choice: FavoriteMusicChoice) -> str | None:
        if choice.mode == "none":
            return None
        available = set(self.ids_for(account_id))
        requested = tuple(choice.ids)
        if not set(requested).issubset(available):
            raise ValueError("favorite music must belong to current account")
        if choice.mode == "fixed":
            return requested[0]
        return str(self._rng.choice(requested))


__all__ = ["FavoriteMusicChoice", "FavoriteMusicService"]
