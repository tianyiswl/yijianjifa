from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yijianjifa.features.accounts.session_store import SessionStore


@dataclass(frozen=True, slots=True)
class BrowserSessionConfig:
    account_id: str
    user_data_dir: Path
    headless: bool = False


def build_browser_session_config(store: SessionStore, account_id: str, *, headless: bool = False) -> BrowserSessionConfig:
    profile = store.ensure_profile_dir(account_id)
    return BrowserSessionConfig(account_id=account_id, user_data_dir=profile, headless=headless)
