import json

from yijianjifa.features.tasks.diagnostics import DiagnosticBundle


def test_diagnostic_export_never_contains_auth_secrets():
    raw = {
        "error_code": "x",
        "cookie": "SECRET",
        "verification_code": "123456",
        "access_token": "TOKEN",
        "message": "safe",
    }
    text = DiagnosticBundle.from_raw(raw).to_json()
    assert "safe" in text
    assert "SECRET" not in text
    assert "123456" not in text
    assert "TOKEN" not in text
    payload = json.loads(text)
    assert "cookie" not in payload
    assert "verification_code" not in payload
    assert "access_token" not in payload


def test_secret_like_values_inside_error_text_are_redacted():
    raw = {
        "error_code": "x",
        "message": "Cookie: abc123 access_token=tok verification_code=654321 Bearer bearer-secret qr_code=qr-secret",
    }
    text = DiagnosticBundle.from_raw(raw).to_json()
    assert "abc123" not in text
    assert "tok" not in text
    assert "654321" not in text
    assert "bearer-secret" not in text
    assert "qr-secret" not in text
    assert "[REDACTED]" in text


def test_nested_platform_evidence_uses_allowlist_instead_of_dumping_raw_state():
    raw = {
        "task_id": "P1",
        "evidence": {
            "final_action_triggered": True,
            "public_result": None,
            "browser_storage_state": {"cookies": [{"value": "SECRET"}]},
            "access_token": "TOKEN",
        },
    }
    payload = json.loads(DiagnosticBundle.from_raw(raw).to_json())
    assert payload["evidence"] == {
        "final_action_triggered": True,
        "public_result": None,
    }
    assert "SECRET" not in json.dumps(payload)
    assert "TOKEN" not in json.dumps(payload)


def test_copyable_problem_text_contains_user_facing_fields_but_not_secrets():
    bundle = DiagnosticBundle.from_raw(
        {
            "task_id": "P1",
            "title": "发布结果待确认",
            "stage": "平台回读",
            "reason": "Cookie=secret session expired",
            "impact": "禁止自动重发",
            "next_action": "先核对平台",
            "occurred_at": "2026-09-23 18:00",
        }
    )
    text = bundle.copyable_problem_text()
    assert "发布结果待确认" in text
    assert "禁止自动重发" in text
    assert "先核对平台" in text
    assert "secret" not in text
