from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    kind: str
    name: str
    state: str
    account_id: str | None
    snapshot_json: str
    queue_order: int
    created_at: datetime
    updated_at: datetime

    @property
    def retry_allowed(self) -> bool:
        # Result-unknown can represent a submission the platform already
        # accepted, so it is deliberately absent from this allowlist.
        return self.state in {"pre_submit_failed", "failed"}

    @property
    def snapshot(self) -> dict[str, Any]:
        return json.loads(self.snapshot_json)


@dataclass(frozen=True, slots=True)
class TaskItemRecord:
    id: str
    task_id: str
    item_key: str
    state: str
    detail_json: str


@dataclass(frozen=True, slots=True)
class IssueRecord:
    id: str
    task_id: str
    title: str
    object_name: str
    stage: str
    reason: str
    impact: str
    next_action: str
    occurred_at: str
    error_code: str
    evidence_json: str

    @property
    def evidence(self) -> dict[str, Any]:
        return json.loads(self.evidence_json or "{}")
