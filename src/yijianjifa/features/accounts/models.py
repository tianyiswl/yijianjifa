from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias

PlatformId: TypeAlias = str
CapabilityState: TypeAlias = str


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    ordinary: CapabilityState = "pending"
    commerce: CapabilityState = "pending"
    location_scope: str = "pending"

    def __post_init__(self) -> None:
        allowed = {"available", "not_found", "pending"}
        if self.ordinary not in allowed:
            raise ValueError(f"invalid ordinary capability state: {self.ordinary}")
        if self.commerce not in allowed:
            raise ValueError(f"invalid commerce capability state: {self.commerce}")


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    platform: PlatformId
    platform_user_id: str
    nickname: str
    remark: str
    group_name: str
    is_default: bool
    login_state: str
    session_ref: str | None
    capabilities: CapabilitySnapshot
    avatar_data: str = ""
    last_login_at: datetime | None = None
