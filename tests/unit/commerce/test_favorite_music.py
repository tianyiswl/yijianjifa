import random
import pytest

from yijianjifa.features.commerce.models import FavoriteMusicChoice
from yijianjifa.features.commerce.music import FavoriteMusicService


def test_random_favorite_music_never_uses_other_account_music():
    music = FavoriteMusicService(rng=random.Random(0))
    music.seed("A", ["a1", "a2"])
    music.seed("B", ["b1"])
    choice = FavoriteMusicChoice.random(["a1", "a2"])

    picked = music.pick("A", choice)

    assert picked in {"a1", "a2"}
    assert picked != "b1"


def test_fixed_music_from_other_account_is_rejected():
    music = FavoriteMusicService()
    music.seed("A", ["a1"])
    music.seed("B", ["b1"])

    with pytest.raises(ValueError, match="current account"):
        music.pick("A", FavoriteMusicChoice.fixed("b1"))


def test_none_music_returns_none_and_does_not_modify_video_audio():
    music = FavoriteMusicService()
    music.seed("A", ["a1"])
    assert music.pick("A", FavoriteMusicChoice.none()) is None
