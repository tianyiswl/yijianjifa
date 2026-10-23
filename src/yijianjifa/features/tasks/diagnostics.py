from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from yijianjifa.features.tasks.models import IssueRecord

# The diagnostic serializer is intentionally allowlist-based. Unknown fields are
# dropped, rather than inspected for whether they "look safe".
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "task_id",
        "task_type",
        "title",
        "object",
        "stage",
        "reason",
        "impact",
        "next_action",
        "occurred_at",
        "error_code",
        "app_version",
        "os",
        "message",
        "raw_error",
        "recent_steps",
        "evidence",
    }
)
_ALLOWED_EVIDENCE = frozenset(
    {
        "final_action_triggered",
        "platform_schedule_confirmation",
        "public_result",
        "error_code",
        "auth_required",
        "decision",
        "selector_state",
        "store_id",
        "scope",
    }
)

# Redact whole credential expressions, including the key name, so the secret
# value cannot survive in exception messages or platform text snippets.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:cookie|set-cookie|access[_ -]?token|refresh[_ -]?token|oauth[_ -]?token|"
    r"verification[_ -]?code|验证码|password|passwd|qr[_ -]?code|二维码)\s*[:=]\s*[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def _redact_text(value: str) -> str:
    # Structured exception snippets and whole Cookie headers must not leak
    # the remainder after a space or semicolon.
    keys = r'cookie|set-cookie|access[_ -]?token|refresh[_ -]?token|oauth[_ -]?token|verification[_ -]?code|验证码|password|passwd|qr[_ -]?code|二维码'
    value = re.sub(r'(?i)"(?:' + keys + r')"\s*:\s*"(?:\\.|[^"\\])*"', '"[REDACTED]"', value)
    value = re.sub(r'(?im)(?:^|\b)(?:cookie|set-cookie)\s*[:=][^\r\n]*', '[REDACTED]', value)
    value = _SECRET_ASSIGNMENT.sub("[REDACTED]", value)
    value = _BEARER.sub("[REDACTED]", value)
    return value


def _sanitize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _sanitize_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in _ALLOWED_EVIDENCE:
        if key in value:
            result[key] = _sanitize_scalar(value[key])
    return result


@dataclass(frozen=True, slots=True)
class DiagnosticBundle:
    payload: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "DiagnosticBundle":
        clean: dict[str, Any] = {}
        for key in _ALLOWED_TOP_LEVEL:
            if key not in raw:
                continue
            value = raw[key]
            if key == "evidence":
                clean[key] = _sanitize_evidence(value)
            elif key == "recent_steps":
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                    clean[key] = [_sanitize_scalar(item) for item in value]
                else:
                    clean[key] = []
            else:
                clean[key] = _sanitize_scalar(value)
        return cls(clean)

    @classmethod
    def from_issue(
        cls,
        issue: IssueRecord,
        *,
        task_type: str = "",
        environment: Mapping[str, Any] | None = None,
        recent_steps: Sequence[str] = (),
    ) -> "DiagnosticBundle":
        environment = environment or {}
        return cls.from_raw(
            {
                "task_id": issue.task_id,
                "task_type": task_type,
                "title": issue.title,
                "object": issue.object_name,
                "stage": issue.stage,
                "reason": issue.reason,
                "impact": issue.impact,
                "next_action": issue.next_action,
                "occurred_at": issue.occurred_at,
                "error_code": issue.error_code,
                "app_version": environment.get("app_version", ""),
                "os": environment.get("os", ""),
                "recent_steps": list(recent_steps),
                "evidence": issue.evidence,
            }
        )

    def to_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True, indent=2)

    def copyable_problem_text(self) -> str:
        labels = (
            ("问题", "title"),
            ("任务", "task_id"),
            ("对象", "object"),
            ("出错阶段", "stage"),
            ("原因", "reason"),
            ("当前影响", "impact"),
            ("建议处理", "next_action"),
            ("发生时间", "occurred_at"),
            ("错误编号", "error_code"),
        )
        lines = [f"{label}：{self.payload[key]}" for label, key in labels if self.payload.get(key) not in {None, ""}]
        return "\n".join(lines)

    def export_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(), encoding="utf-8")
        return target
