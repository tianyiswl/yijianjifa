from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir, user_log_dir

from yijianjifa import APP_NAME


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    cache_dir: Path
    logs_dir: Path
    managed_media_dir: Path


def get_app_paths() -> AppPaths:
    override = os.getenv("YJJF_TEST_DATA_DIR")
    if override:
        data = Path(override)
        cache = data / "cache"
        logs = data / "logs"
    else:
        data = Path(user_data_dir(APP_NAME, appauthor=False))
        cache = Path(user_cache_dir(APP_NAME, appauthor=False))
        logs = Path(user_log_dir(APP_NAME, appauthor=False))

    managed = data / "managed_media"
    for path in (data, cache, logs, managed):
        path.mkdir(parents=True, exist_ok=True)

    return AppPaths(
        data_dir=data,
        cache_dir=cache,
        logs_dir=logs,
        managed_media_dir=managed,
    )
