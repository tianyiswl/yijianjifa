from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VideoAssignment:
    video_id: str
    account_id: str


def distribute(video_ids: list[str], account_ids: list[str], *, mode: str = "split_across_accounts") -> list[VideoAssignment]:
    videos = list(dict.fromkeys(video_ids))
    accounts = list(dict.fromkeys(account_ids))
    if not videos:
        return []
    if not accounts:
        raise ValueError("at least one account is required")
    if mode == "split_across_accounts":
        return [VideoAssignment(video_id, accounts[index % len(accounts)]) for index, video_id in enumerate(videos)]
    if mode == "whole_batch":
        return [VideoAssignment(video_id, account_id) for account_id in accounts for video_id in videos]
    raise ValueError(f"unsupported distribution mode: {mode}")
