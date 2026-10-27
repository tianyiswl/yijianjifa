"""Exercise the actual recovery gate in PowerShell without GitHub access."""

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="Windows recovery contract requires PowerShell 7")
def test_recovery_requires_complete_ui_or_verified_legacy_combined_checks(tmp_path):
    workflow = (ROOT / ".github/workflows/windows-test.yml").read_text(encoding="utf-8")
    recovery = re.search(
        r"(?ms)^      - name: Validate optional draft delivery recovery\n"
        r".*?^        run: \|\n(?P<run>.*?)(?=^      - (?:name:|uses:)|\Z)",
        workflow,
    )
    assert recovery is not None
    actual_run = "\n".join(line[10:] for line in recovery["run"].splitlines())
    legacy = (Path(__file__).parent / "fixtures/windows-3ab5770-backend.yml").read_text(encoding="utf-8")
    source = "a" * 40
    required = [
        "Backend and interface checks",
        "Build current Windows application",
        "Frozen database, media and bundled asset checks",
        "Frozen startup and private bridge smoke check",
        "Record build and stage delivery instructions",
        "Package portable folder and hash deliverables",
    ]
    ui_name = "Complete Windows UI regressions"
    legacy_full = "python -m pytest tests -q --durations=10"
    qt_failure = "if ($LASTEXITCODE -ne 0) { throw 'Real Windows voice-list scroll regression failed' }"
    node_failure = "if ($LASTEXITCODE -ne 0) { throw 'Interface checks failed' }"
    incomplete = "Verified workflow lacks complete Python/Qt/Node commands or failure checks"
    ui_block = re.search(
        r"(?ms)^      - name: Complete Windows UI regressions\n.*?(?=^      - (?:name:|uses:)|\Z)",
        workflow,
    )
    assert ui_block is not None
    scenarios = [
        ("new-complete", workflow, "success", None),
        ("new-ui-failed", workflow, "failure", f"Verified run lacks successful step: {ui_name}"),
        ("new-ui-missing", workflow, None, f"Verified run lacks successful step: {ui_name}"),
        ("legacy-3ab-complete", legacy, None, None),
        ("legacy-missing-full-python", legacy.replace(legacy_full, "Write-Host 'no Python checks'"), None, incomplete),
        ("legacy-ignores-ui", legacy.replace(legacy_full, "python -m pytest tests --ignore=tests/ui -q --durations=10"), None, incomplete),
        ("legacy-missing-qt-failure", legacy.replace(qt_failure, "Write-Host 'unchecked Qt'"), None, incomplete),
        ("legacy-missing-node-failure", legacy.replace(node_failure, "Write-Host 'unchecked Node'"), None, incomplete),
        ("new-hides-ui-stage", workflow.replace(ui_block[0], ""), None, incomplete),
        ("new-partial-ui", workflow.replace("python -m pytest tests/ui -vv", "python -m pytest tests/ui/library -vv"), "success", "Verified workflow lacks the complete UI command and failure check"),
    ]
    cases = []
    for name, definition, ui_conclusion, expected_error in scenarios:
        steps = [{"name": step, "conclusion": "success"} for step in required]
        if ui_conclusion is not None:
            steps.append({"name": ui_name, "conclusion": ui_conclusion})
        responses = {
            "repos/fixture/repo": {"private": True},
            "repos/fixture/repo/actions/runs/900": {"workflow_id": 17},
            "repos/fixture/repo/actions/runs/800": {
                "status": "completed", "head_sha": source, "workflow_id": 17,
                "head_repository": {"full_name": "fixture/repo"},
                "path": ".github/workflows/windows-test.yml",
            },
            "repos/fixture/repo/actions/runs/800/jobs?filter=latest&per_page=100": {
                "jobs": [{"name": "build", "status": "completed", "steps": steps}],
            },
            f"repos/fixture/repo/contents/.github/workflows/windows-test.yml?ref={source}": {
                "type": "file", "encoding": "base64", "path": ".github/workflows/windows-test.yml",
                "content": base64.b64encode(definition.encode("utf-8")).decode("ascii"),
            },
            "repos/fixture/repo/releases/700": {
                "id": 700, "draft": True, "target_commitish": source, "assets": [],
            },
        }
        cases.append({"name": name, "responses": responses, "expected_error": expected_error})
    fixture = tmp_path / "recovery-cases.json"
    fixture.write_text(json.dumps({"run": actual_run, "cases": cases}), encoding="utf-8")
    harness = r"""
$ErrorActionPreference = 'Stop'
$fixture = Get-Content $env:P011_CONTRACT_FIXTURE -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
$run = [scriptblock]::Create($fixture.run)
function gh([string]$command, [string]$path) {
  if ($command -ne 'api' -or -not $script:responses.ContainsKey($path)) { throw "Unexpected fixture API path: $command $path" }
  $global:LASTEXITCODE = 0
  $script:responses[$path] | ConvertTo-Json -Depth 50 -Compress
}
foreach ($case in $fixture.cases) {
  $script:responses = $case.responses
  $env:GITHUB_OUTPUT = Join-Path $env:P011_CONTRACT_OUTPUT $case.name
  $failure = $null
  try { & $run | Out-Null } catch { $failure = $_.Exception.Message }
  if ($null -eq $case.expected_error) {
    if ($failure) { throw "$($case.name): unexpectedly rejected: $failure" }
    $output = Get-Content $env:GITHUB_OUTPUT -Raw
    if ($output -notmatch '(?m)^recovery=true\r?$' -or $output -notmatch '(?m)^draft_delivery=true\r?$') { throw "$($case.name): missing accepted recovery outputs" }
  } else {
    if (-not $failure -or -not $failure.Contains($case.expected_error)) { throw "$($case.name): expected rejection '$($case.expected_error)', got '$failure'" }
    if (Test-Path $env:GITHUB_OUTPUT) { throw "$($case.name): rejected recovery emitted success outputs" }
  }
}
Write-Output "RECOVERY_CONTRACT_PASSED=$($fixture.cases.Count)"
"""
    env = os.environ | {
        "P011_CONTRACT_FIXTURE": str(fixture), "P011_CONTRACT_OUTPUT": str(tmp_path),
        "P011_SOURCE_COMMIT": source, "P011_VERIFIED_RUN_ID": "800", "P011_DRAFT_RELEASE_ID": "700",
        "GITHUB_RUN_ID": "900", "GITHUB_REPOSITORY": "fixture/repo", "GITHUB_SHA": source,
    }
    script = tmp_path / "recovery-contract.ps1"
    script.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-File", str(script)],
        text=True, encoding="utf-8", capture_output=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"RECOVERY_CONTRACT_PASSED={len(scenarios)}" in result.stdout, result.stdout + result.stderr
