#!/usr/bin/env python3
"""Prepare a local, review-only source candidate from explicit tracked paths.

No network access, Git history export, untracked traversal, publication or new
runtime dependencies. Passing this limited preflight is not a security or rights
audit. Read docs/release/PUBLIC_SOURCE_PREPARATION.md before using the candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


# Expand these only after reviewing a new source family. Never replace with a
# recursive repository copy: this checkout also holds private operational work.
INCLUDED_FILES = frozenset({
    ".gitattributes", ".gitignore", ".github/workflows/windows-test.yml",
    "README.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
    "pyproject.toml", "alembic.ini", ".env.example", "config.example.toml",
    "tests/conftest.py", "server/cloud_voice/README.md",
    "server/cloud_voice/pyproject.toml", "docs/CONFIGURATION.md",
    "docs/QUICK_START.md", "docs/release/PUBLIC_SOURCE_PREPARATION.md",
    "docs/release/THIRD_PARTY_RELEASE_AUDIT.md",
    "docs/release/THIRD_PARTY_RELEASE_READINESS.md",
    "tools/prepare_public_source.py", "tools/audit_windows_bundle.py",
    "tools/mac_license_inventory.py", "tools/audit_supplier_voices.py",
    "tools/verify_release_licenses.py",
    "tools/download_probe.py", "tools/voice_list_scroll_probe.py",
    "tools/probe_qt_trim.py", "tools/probe_bundle_trim.py",
    "tools/windows_audio_probe.py", "tools/windows_video_probe.py",
    "tools/windows_native_video_probe.py", "tools/windows_embedded_video_probe.py",
    "tools/webui_probe/auth_config_check.py", "tools/webui_probe/bridge_probe.py",
    "tools/webui_probe/isolated_bridge_probe.py", "tools/webui_probe/media_registry.py",
    "tools/webui_probe/native_probe.py", "tools/webui_probe/probe_lifecycle.py",
    "tools/webui_probe/qt_probe.py", "tools/webui_probe/resource_probe.py",
    "tools/webui_probe/test_media_registry.py", "tools/webui_probe/test_probe_lifecycle.py",
    "tools/webui_probe/assets/index.html", "tools/webui_probe/assets/native.html",
    "docs/validation/2026-10-20-windows-trim-candidates.json",
})
INCLUDED_PREFIXES = (
    "src/yijianjifa/", "alembic/", "packaging/",
    "server/cloud_voice/yijianjifa_cloud_voice/", "server/cloud_voice/tests/",
    "server/cloud_voice/deploy/", "tests/unit/", "tests/integration/",
    "tests/webui/", "tests/ui/", "tests/acceptance/",
)
TEXT_SUFFIXES = frozenset({
    ".py", ".js", ".cjs", ".mjs", ".html", ".css", ".svg", ".json",
    ".toml", ".ini", ".yaml", ".yml", ".txt", ".md", ".sh", ".ps1",
    ".spec", ".iss", ".service", ".example",
})
PRIVATE_COMPONENTS = frozenset({
    ".git", ".worktrees", ".superpowers", ".venv", "venv", "__pycache__",
    ".pytest_cache", "node_modules", "build", "dist", "outputs", "output",
    "profiles", "cache", "logs", "data", "runtime", "evidence", "screenshots",
})
PRIVATE_FILENAMES = frozenset({
    "cookies.json", "credentials.json", "secrets.json", "session.json",
    "sessions.json", "settings.json", "accounts.json",
})
PRIVATE_SUFFIXES = frozenset({
    ".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx",
})
ASSET_SUFFIXES = frozenset({
    ".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".mov", ".png", ".jpg",
    ".jpeg", ".webp", ".gif", ".ico", ".icns", ".ttf", ".otf", ".woff",
    ".woff2", ".zip", ".gz", ".exe", ".dll", ".dylib", ".so", ".onnx",
})
CONFIG_SUFFIXES = frozenset({".json", ".toml", ".ini", ".yaml", ".yml", ".example"})
CREDENTIAL_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,255}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,255}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b"),
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,255}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
)
SECRET_NAME = r"[\w.-]*(?:token|secret|password|api[_-]?key|access[_-]?key)[\w.-]*"
QUOTED_SECRET = re.compile(
    rf'''["']{SECRET_NAME}["']\s*[:=]\s*["']([^"'\r\n]*)["']''', re.I,
)
LINE_SECRET = re.compile(
    rf"^\s*(?:export\s+)?{SECRET_NAME}\s*[:=]\s*(.*?)\s*$", re.I | re.M,
)


# These are known project-level gaps caused by deliberate privacy/asset policy,
# not a claim that every other dependency is present or cleared for release.
KNOWN_RELEASE_CLOSURE_GAPS = [
    {"path": "docs/acceptance/live-douyin-final.md", "needed_by": "tests/acceptance/test_live_release_docs.py", "reason": "acceptance history/templates remain outside the public source policy"},
    {"path": "docs/release/V1_RELEASE_CHECKLIST.md", "needed_by": "tests/acceptance/test_live_release_docs.py", "reason": "historical release evidence remains outside the public source policy"},
    {"path": "docs/acceptance/v1-matrix.md", "needed_by": "tests/acceptance/test_matrix_static_links.py", "reason": "historical matrix and evidence links remain outside the public source policy"},
    {"path": "docs/acceptance/windows-install.md", "needed_by": "tests/acceptance/test_packaging_contracts.py", "reason": "acceptance history/templates remain outside the public source policy"},
    {"path": "docs/acceptance/macos-install.md", "needed_by": "tests/acceptance/test_packaging_contracts.py", "reason": "historical local installation evidence remains outside the public source policy"},
    {"path": "src/yijianjifa/webui/assets/app-icon.png", "needed_by": "desktop UI and packaging", "reason": "binary asset rights/build closure requires separate review"},
    {"path": "src/yijianjifa/webui/assets/capability-test.mp4", "needed_by": "native media capability checks", "reason": "binary asset rights/build closure requires separate review"},
    {"path": "src/yijianjifa/webui/assets/voice_samples/**/*.mp3", "needed_by": "voice sample UI, tests and tools/windows_audio_probe.py", "reason": "sample rights and redistribution terms remain unresolved; pattern includes direct and official subdirectory samples"},
    {"path": "packaging/resources/icons/yijianjifa.png", "needed_by": "packaging", "reason": "binary icon asset rights/build closure requires separate review"},
    {"path": "packaging/resources/icons/yijianjifa.ico", "needed_by": "Windows packaging", "reason": "binary icon asset rights/build closure requires separate review"},
    {"path": "packaging/resources/icons/yijianjifa.icns", "needed_by": "macOS packaging", "reason": "binary icon asset rights/build closure requires separate review"},
    {"path": "tools/webui_probe/assets/probe.mp4", "needed_by": "tools/webui_probe/native_probe.py and qt_probe.py", "reason": "binary probe fixture is excluded pending asset review"},
]


class PreparationError(Exception):
    """An actionable preflight failure; never contains matched source values."""


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False,
    )
    if result.returncode:
        raise PreparationError("Git tracked-file inventory failed; use a Git working-tree root")
    return result.stdout


def _relative_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute() or "\\" in name or ":" in name
        or any(ord(char) < 32 for char in name)
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or path.as_posix() != name
    ):
        raise PreparationError("Unsafe tracked path in inventory; no source copied")
    return path


def _exclusion(path: PurePosixPath) -> str | None:
    name = path.as_posix()
    lower_parts = tuple(part.lower() for part in path.parts)
    if any(part in PRIVATE_COMPONENTS or part.startswith(".venv") for part in lower_parts):
        return "runtime_or_private_path"
    if path.name.lower() in PRIVATE_FILENAMES:
        return "runtime_or_credential_file"
    if path.name.lower().startswith(".env") and not path.name.lower().endswith(".example"):
        return "runtime_or_credential_file"
    if path.suffix.lower() in PRIVATE_SUFFIXES or re.search(r"\.(?:db|sqlite\d?)-", path.name, re.I):
        return "runtime_or_credential_file"
    if name not in INCLUDED_FILES and not name.startswith(INCLUDED_PREFIXES):
        return "outside_explicit_source_policy"
    if path.suffix.lower() in ASSET_SUFFIXES:
        return "binary_or_asset_requires_manual_rights_review"
    if name.startswith("packaging/resources/licenses/"):
        return None
    if name in INCLUDED_FILES or path.suffix.lower() in TEXT_SUFFIXES:
        return None
    return "unsupported_file_type_requires_manual_review"


def _check_output(repo: Path, output: Path) -> Path:
    if not output.is_absolute() or ".." in output.parts:
        raise PreparationError("Output must be a fresh absolute path without traversal")
    if any(parent.is_symlink() for parent in (output, *output.parents)):
        raise PreparationError("Output and its parents must not be symlinks")
    if output.exists() or not output.parent.is_dir():
        raise PreparationError("Output must not exist and its parent must already be a directory")
    output = output.parent.resolve() / output.name
    if output == repo or repo in output.parents or output in repo.parents:
        raise PreparationError("Output must be outside the source repository and its ancestors")
    return output


def _placeholder(value: str, *, github_workflow: bool = False) -> bool:
    value = value.strip().split(" #", 1)[0].strip().strip("\"'").rstrip(",")
    return (
        not value or value.lower() in {"none", "null"}
        or (value.startswith("<") and value.endswith(">"))
        or re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value) is not None
        or (github_workflow and re.fullmatch(
            r"\$\{\{\s*(?:github\.token|secrets\.[A-Za-z_][A-Za-z0-9_]*)\s*\}\}", value,
        ) is not None)
        or re.match(r"(?:your|replace|changeme|change_me|example|dummy|fake|synthetic|sample)(?:[_ -]|$)", value, re.I) is not None
    )


def _check_content(path: PurePosixPath, data: bytes) -> None:
    if b"\0" in data:
        raise PreparationError(f"Non-text included file requires manual review: {path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PreparationError(f"Non-UTF-8 included file requires manual review: {path}") from None
    if any(pattern.search(text) for pattern in CREDENTIAL_PATTERNS):
        raise PreparationError(f"Potential credential in included file: {path}; values suppressed")
    if path.suffix.lower() in CONFIG_SUFFIXES:
        values = [match.group(1) for pattern in (QUOTED_SECRET, LINE_SECRET) for match in pattern.finditer(text)]
        github_workflow = path.parts[:2] == (".github", "workflows") and path.suffix.lower() in {".yml", ".yaml"}
        if any(not _placeholder(value, github_workflow=github_workflow) for value in values):
            raise PreparationError(f"Potential credential in included configuration: {path}; values suppressed")


def prepare_public_source(repo: Path, output: Path) -> dict:
    repo = repo.resolve(strict=True)
    top = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != repo:
        raise PreparationError("Repository argument must be the Git working-tree root")
    output = _check_output(repo, output)
    included = []
    excluded = []
    payloads = []
    # --stage enumerates only the index, including deliberately staged new files.
    # It never walks ignored/untracked account state or opens Git object history.
    for entry in _git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        meta, raw_name = entry.split(b"\t", 1)
        mode, _object_id, stage = meta.decode("ascii").split()
        path = _relative_path(raw_name.decode("utf-8"))
        reason = _exclusion(path)
        if reason:
            excluded.append({"path": path.as_posix(), "reason": reason})
            continue
        if stage != "0":
            raise PreparationError(f"Unresolved tracked file: {path}")
        source = repo.joinpath(*path.parts)
        if mode == "120000" or any(part.is_symlink() for part in (source, *source.parents) if part != repo):
            raise PreparationError(f"Included symlink refused: {path}")
        if mode not in {"100644", "100755"} or not source.is_file():
            raise PreparationError(f"Included source must be an existing regular file: {path}")
        data = source.read_bytes()
        _check_content(path, data)
        # 执行位属于 Git 源码元数据，Windows 文件系统不能可靠表示它。
        executable = mode == "100755"
        included.append({
            "path": path.as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data), "mode": "100755" if executable else "100644",
        })
        payloads.append((path, data, executable))
    if not included:
        raise PreparationError("No tracked files matched the explicit source policy")
    manifest = {
        "schema_version": 1, "policy": "explicit_tracked_source_v1",
        "source_snapshot": "current_worktree_contents_of_git_tracked_paths",
        "publication_status": "not_published", "release_ready": False,
        "untracked_files": "excluded_without_reading_or_enumerating",
        "included": included, "excluded": excluded,
        "known_release_closure_gaps": KNOWN_RELEASE_CLOSURE_GAPS,
        "manual_review_required": [
            "Review all included code, configuration, endpoints and synthetic test fixtures for private or sensitive material; regex checks are incomplete.",
            "Resolve known_release_closure_gaps before claiming the candidate can run the full repository suite or reconstruct a complete release; the listed gaps are not an exhaustive dependency audit.",
            "Confirm authorship and third-party source license obligations for every included component; MIT does not relicense third-party code.",
            "Resolve excluded media, icons, fonts, models and binary dependencies separately before claiming a buildable or redistributable release.",
            "Obtain the user's explicit approval before creating a public repository, changing visibility, pushing source or publishing binaries.",
        ],
    }
    # Preflight everything before creating output. mkdir is exclusive; existing
    # output is never overwritten; preflight failures create no directory.
    output.mkdir()
    for path, data, executable in payloads:
        destination = output.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(data)
        destination.chmod(0o755 if executable else 0o644)
    with (output / "PUBLIC_SOURCE_MANIFEST.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="fresh absolute directory outside the repository")
    args = parser.parse_args()
    try:
        manifest = prepare_public_source(args.repo, args.output)
    except (PreparationError, OSError, UnicodeError, ValueError) as error:
        # Known failures name paths, never source lines or credential matches.
        if isinstance(error, PreparationError):
            print(f"Source preparation refused: {error}", file=sys.stderr)
        else:
            print("Source preparation refused: filesystem or inventory failure; no credential values displayed", file=sys.stderr)
        return 1
    print(f"Local review candidate: {len(manifest['included'])} files included, {len(manifest['excluded'])} tracked paths excluded. Source not published; manual rights and privacy review remains required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
