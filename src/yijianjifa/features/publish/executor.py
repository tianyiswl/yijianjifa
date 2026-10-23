from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubmissionEvidence:
    final_action_triggered: bool
    platform_schedule_confirmation: str | None = None
    public_result: str | None = None
    error_code: str | None = None
    auth_required: bool = False


class PublishExecutor:
    def __init__(self):
        self._batch_accounts: dict[str, dict[str, str]] = {}
        self._pause_reasons: dict[tuple[str, str], str] = {}

    def classify(self, evidence: SubmissionEvidence) -> str:
        if evidence.auth_required and not evidence.final_action_triggered:
            return "paused_auth"
        if not evidence.final_action_triggered:
            return "pre_submit_failed" if evidence.error_code else "not_submitted"
        if evidence.public_result == "published":
            return "published"
        if evidence.public_result == "rejected":
            return "platform_rejected"
        if evidence.platform_schedule_confirmation is not None:
            return "platform_scheduled"
        return "result_unknown"

    @staticmethod
    def retry_allowed(state: str) -> bool:
        return state == "pre_submit_failed"

    def register_batch(self, batch_id: str, account_ids: list[str]) -> None:
        accounts = list(dict.fromkeys(account_ids))
        if not accounts:
            raise ValueError("publish batch requires at least one account")
        self._batch_accounts[batch_id] = {account_id: "ready" for account_id in accounts}

    def pause_account(self, batch_id: str, account_id: str, *, reason: str) -> None:
        try:
            states = self._batch_accounts[batch_id]
        except KeyError as exc:
            raise KeyError(batch_id) from exc
        if account_id not in states:
            raise KeyError(account_id)
        states[account_id] = "paused_auth" if reason == "auth_required" else "paused"
        self._pause_reasons[(batch_id, account_id)] = reason

    def account_state(self, batch_id: str, account_id: str) -> str:
        try:
            return self._batch_accounts[batch_id][account_id]
        except KeyError as exc:
            raise KeyError((batch_id, account_id)) from exc
