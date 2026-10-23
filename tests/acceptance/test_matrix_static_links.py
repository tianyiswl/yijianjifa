from pathlib import Path
import re


def parse_matrix(path: str):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = re.match(r"\|\s*(\d+)\s*\|\s*([^|]+)\|", line)
        if match:
            rows.append((int(match.group(1)), match.group(2).strip()))
    return rows


def test_acceptance_matrix_contains_38_unique_scenarios():
    rows = parse_matrix("docs/acceptance/v1-matrix.md")
    numbers = [number for number, _ in rows]
    assert sorted(numbers) == list(range(1, 39))
    assert len(numbers) == len(set(numbers)) == 38


def test_acceptance_matrix_does_not_prefill_pass_without_os_and_live_evidence():
    text = Path("docs/acceptance/v1-matrix.md").read_text(encoding="utf-8")
    assert "| PASS |" not in text
