"""Exercise the offline exporter with controlled tracked files, never real data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "prepare_public_source.py"


def _write(root: Path, relative: str, content: str = "synthetic fixture\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return path


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    return root


def _track(root: Path, *paths: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "--", *paths], check=True)


def _track_raw_name(root: Path, name: str) -> None:
    """构造真实 Git v2 索引，避免宿主文件系统改写或拒绝危险名称。"""
    blob = subprocess.run(["git", "-C", str(root), "hash-object", "-w", "--stdin"],
                          input=b"synthetic fixture\n", capture_output=True, check=True).stdout.strip()
    encoded = name.encode("utf-8")
    entry = (struct.pack(">10I", 0, 0, 0, 0, 0, 0, 0o100644, 0, 0, 0)
             + bytes.fromhex(blob.decode("ascii")) + struct.pack(">H", len(encoded)) + encoded + b"\0")
    entry += bytes(-len(entry) % 8)
    data = b"DIRC" + struct.pack(">II", 2, 1) + entry
    (root / ".git/index").write_bytes(data + hashlib.sha1(data).digest())
    inventory = subprocess.run(["git", "-C", str(root), "ls-files", "--stage", "-z"],
                               capture_output=True, check=True).stdout
    assert inventory == b"100644 " + blob + b" 0\t" + encoded + b"\0"


def _export(root: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(root), "--output", str(output)],
        text=True, capture_output=True,
    )


def test_exports_current_tracked_product_server_build_migrations_and_synthetic_tests(source_repo, tmp_path):
    # Dropping a source family, exporting HEAD, or losing a build executable breaks this.
    expected = [
        "README.md", "LICENSE", "pyproject.toml", "alembic.ini",
        "src/yijianjifa/main.py", "src/yijianjifa/webui/assets/main.js",
        "server/cloud_voice/yijianjifa_cloud_voice/api.py",
        "server/cloud_voice/tests/test_api.py",
        "server/cloud_voice/deploy/service.env.example",
        "alembic/versions/0001_initial.py", "packaging/macos/build.sh",
        "packaging/windows/yijianjifa.spec", "tests/unit/test_synthetic.py",
        "tests/integration/test_synthetic.py", "tests/webui/synthetic.test.cjs",
        "tests/conftest.py", ".github/workflows/windows-test.yml",
        "tools/prepare_public_source.py", "tools/verify_release_licenses.py", "config.example.toml",
        "packaging/resources/licenses/native/demo/LICENSE",
        "packaging/release_configuration.py", "docs/CONFIGURATION.md", "docs/QUICK_START.md",
        "docs/release/THIRD_PARTY_RELEASE_READINESS.md",
        "docs/release/THIRD_PARTY_RELEASE_AUDIT.md",
    ]
    for relative in expected:
        _write(source_repo, relative)
    build = source_repo / "packaging/macos/build.sh"
    build.chmod(0o755)
    _track(source_repo, *expected)
    subprocess.run(["git", "-C", str(source_repo), "update-index", "--chmod=+x",
                    "packaging/macos/build.sh"], check=True)
    content = "# local tracked edit\nprint('synthetic')\n"
    (source_repo / "src/yijianjifa/main.py").write_bytes(content.encode("utf-8"))

    output = tmp_path / "candidate"
    result = _export(source_repo, output)

    assert result.returncode == 0, result.stderr
    assert (output / "src/yijianjifa/main.py").read_text(encoding="utf-8") == content
    if sys.platform != "win32":
        assert (output / "packaging/macos/build.sh").stat().st_mode & 0o111
    manifest = json.loads((output / "PUBLIC_SOURCE_MANIFEST.json").read_text())
    assert {item["path"] for item in manifest["included"]} == set(expected)
    included = {item["path"]: item for item in manifest["included"]}
    assert included["packaging/macos/build.sh"]["mode"] == "100755"
    assert included["src/yijianjifa/main.py"]["mode"] == "100644"
    assert included["src/yijianjifa/main.py"]["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert manifest["publication_status"] == "not_published"
    assert manifest["release_ready"] is False
    assert manifest["manual_review_required"]
    assert not (output / ".git").exists()


def test_excludes_tracked_operational_runtime_assets_and_all_untracked_files(source_repo, tmp_path):
    # A broad recursive copy or broad docs/assets inclusion leaks these fixtures.
    _write(source_repo, "src/yijianjifa/main.py", "print('safe')\n")
    excluded = [
        "docs/validation/live.json", "docs/research/private.md",
        "docs/acceptance/screenshot.png", "docs/superpowers/plans/internal.md",
        "outputs/customer.mp4", "profiles/account.json", "logs/private.log",
        "src/yijianjifa/data/customer.json", "src/yijianjifa/cache/real.txt",
        "src/yijianjifa/state.db", "src/yijianjifa/.env",
        "src/yijianjifa/cookies.json", "src/yijianjifa/credentials.json",
        "server/cloud_voice/yijianjifa_cloud_voice/session.json",
        "src/yijianjifa/webui/assets/voice_samples/sample.mp3",
        "packaging/resources/icons/yijianjifa.icns", "build/generated.py",
        "dist/customer.zip", "server/cloud_voice/data/voice.db",
    ]
    for relative in excluded:
        _write(source_repo, relative)
    _track(source_repo, "src/yijianjifa/main.py", *excluded)
    _write(source_repo, "src/yijianjifa/untracked_private.py", "private value\n")
    _write(source_repo, "private-notes.txt", "private value\n")

    output = tmp_path / "candidate"
    result = _export(source_repo, output)

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "PUBLIC_SOURCE_MANIFEST.json").read_text())
    assert {item["path"] for item in manifest["included"]} == {"src/yijianjifa/main.py"}
    assert {item["path"] for item in manifest["excluded"]} == set(excluded)
    assert manifest["untracked_files"] == "excluded_without_reading_or_enumerating"
    assert not (output / "src/yijianjifa/untracked_private.py").exists()
    assert not (output / "private-notes.txt").exists()


def test_rejects_included_symlink_without_reading_target(source_repo, tmp_path):
    # Following a tracked source symlink must never copy its private target.
    secret = _write(tmp_path, "private.txt", "outside content must never appear\n")
    path = source_repo / "src/yijianjifa/link.py"
    path.parent.mkdir(parents=True)
    path.symlink_to(secret)
    _track(source_repo, "src/yijianjifa/link.py")
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert "outside content" not in result.stdout + result.stderr
    assert not output.exists()



@pytest.mark.parametrize("unsafe_name", ["..\\outside.py", "C:outside.py", "line\nbreak.py"], ids=["windows-traversal", "drive-path", "control-character"])
def test_rejects_unsafe_tracked_source_paths(source_repo, tmp_path, unsafe_name):
    # 在真实索引中保留原始名称；不依赖 Windows 创建这些 POSIX 文件名。
    relative = "src/yijianjifa/" + unsafe_name
    _track_raw_name(source_repo, relative)
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode != 0
    assert "unsafe tracked path" in result.stderr.lower()
    assert not output.exists()

def test_rejects_symlinked_source_parent(source_repo, tmp_path):
    _write(source_repo, "src/yijianjifa/subdir/main.py", "print('safe')\n")
    _track(source_repo, "src/yijianjifa/subdir/main.py")
    (source_repo / "src/yijianjifa/subdir/main.py").unlink()
    (source_repo / "src/yijianjifa/subdir").rmdir()
    outside = tmp_path / "outside"
    _write(outside, "main.py", "private content\n")
    (source_repo / "src/yijianjifa/subdir").symlink_to(outside, target_is_directory=True)
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert not output.exists()


@pytest.mark.parametrize("location", ["existing", "inside", "ancestor", "traversal", "relative"])
def test_rejects_unsafe_output_without_changing_existing_files(source_repo, tmp_path, location):
    _write(source_repo, "src/yijianjifa/main.py", "print('safe')\n")
    _track(source_repo, "src/yijianjifa/main.py")
    sentinel = _write(tmp_path, "existing/sentinel.txt", "keep me\n")
    output = {
        "existing": tmp_path / "existing",
        "inside": source_repo / "candidate",
        "ancestor": tmp_path,
        "traversal": tmp_path / "existing" / ".." / "candidate",
        "relative": Path("relative-candidate"),
    }[location]

    result = _export(source_repo, output)

    assert result.returncode != 0
    assert "output" in result.stderr.lower()
    assert sentinel.read_text() == "keep me\n"
    assert not (source_repo / "candidate").exists()
    assert not (tmp_path / "candidate").exists()


def test_rejects_output_parent_symlink(source_repo, tmp_path):
    _write(source_repo, "src/yijianjifa/main.py")
    _track(source_repo, "src/yijianjifa/main.py")
    (tmp_path / "destination").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "destination", target_is_directory=True)

    result = _export(source_repo, alias / "candidate")

    assert result.returncode != 0
    assert "output" in result.stderr.lower()
    assert not (tmp_path / "destination/candidate").exists()


@pytest.mark.parametrize("path,content", [
    ("src/yijianjifa/main.py", "API_TOKEN = '" + "ghp_" + "A" * 36 + "'\n"),
    ("server/cloud_voice/deploy/service.env.example", "YJJF_VOLC_API_KEY=production-secret-12345\n"),
    ("config.example.toml", 'api_key = "production-secret-12345"\n'),
    ("src/yijianjifa/config.json", '{"api_key": "production-secret-12345"}\n'),
])
def test_rejects_obvious_credential_values_without_echoing_them(source_repo, tmp_path, path, content):
    # Removing the preflight content gate must fail before any candidate is created.
    _write(source_repo, path, content)
    _track(source_repo, path)
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode != 0
    assert "credential" in result.stderr.lower()
    assert "production-secret-12345" not in result.stdout + result.stderr
    assert "ghp_" + "A" * 36 not in result.stdout + result.stderr
    assert not output.exists()


def test_accepts_placeholder_configuration_without_weakening_provider_token_check(source_repo, tmp_path):
    _write(source_repo, "config.example.toml", 'api_key = "REPLACE_WITH_YOUR_API_KEY"\npassword = "${SERVICE_PASSWORD}"\n')
    _write(source_repo, "server/cloud_voice/deploy/service.env.example", "YJJF_VOLC_API_KEY=<your-api-key>\n")
    _track(source_repo, "config.example.toml", "server/cloud_voice/deploy/service.env.example")

    result = _export(source_repo, tmp_path / "candidate")

    assert result.returncode == 0, result.stderr


def _github_workflow(token_value: str) -> str:
    return (
        "name: Synthetic Windows test\n"
        "on: workflow_dispatch\n"
        "jobs:\n"
        "  tests:\n"
        "    runs-on: windows-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: Check a build\n"
        "        env:\n"
        f"          GH_TOKEN: {token_value}\n"
        "        run: gh run view --repo $env:GITHUB_REPOSITORY\n"
    )


@pytest.mark.parametrize("reference", ["${{ github.token }}", "${{ secrets.RELEASE_TOKEN }}"])
def test_preserves_github_workflow_secret_references_without_treating_them_as_values(source_repo, tmp_path, reference):
    # A real workflow reference is resolved by Actions and contains no credential value.
    content = _github_workflow(reference)
    relative = ".github/workflows/windows-test.yml"
    _write(source_repo, relative, content)
    _track(source_repo, relative)
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode == 0, result.stderr
    assert (output / relative).read_text(encoding="utf-8") == content


@pytest.mark.parametrize("token_value", [
    "${{ github.token }}production-secret-12345",
    "${{ github.token || 'production-secret-12345' }}",
    "${{ 'production-secret-12345' }}",
    "${{ secrets.RELEASE_TOKEN }}" + "ghp_" + "A" * 36,
], ids=["concatenation", "literal-fallback", "literal-expression", "provider-token"])
def test_github_reference_support_does_not_allow_embedded_credential_values(source_repo, tmp_path, token_value):
    relative = ".github/workflows/windows-test.yml"
    _write(source_repo, relative, _github_workflow(token_value))
    _track(source_repo, relative)
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode != 0
    assert "credential" in result.stderr.lower()
    assert "production-secret-12345" not in result.stdout + result.stderr
    assert "ghp_" + "A" * 36 not in result.stdout + result.stderr
    assert not output.exists()


def test_github_reference_exception_does_not_apply_to_ordinary_configuration(source_repo, tmp_path):
    _write(source_repo, "config.example.toml", 'api_key = "${{ github.token }}"\n')
    _track(source_repo, "config.example.toml")
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode != 0
    assert "credential" in result.stderr.lower()
    assert not output.exists()


def test_preserves_required_build_test_tooling_and_only_reviewed_trim_configuration(source_repo, tmp_path):
    # Omitting a workflow/import dependency or broadly admitting validation evidence breaks this.
    expected = [
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
        "tests/acceptance/test_build.py",
        "docs/validation/2026-10-20-windows-trim-candidates.json",
    ]
    for relative in expected:
        _write(source_repo, relative)
    trim = '{"source":"synthetic","scope":"temporary test copy only","files":[{"relative":"_internal/PySide6/synthetic.dll","size":3,"reason":"synthetic test"}],"candidate_bytes":3}\n'
    (source_repo / expected[-1]).write_text(trim, encoding="utf-8")
    excluded = [
        "tools/acceptance/mac_cookie_archive.py", "tools/webui_probe/README.md",
        "tools/webui_probe/P011-T02-Media.spec", "tools/webui_probe/private-report.json",
        "tools/webui_probe/assets/probe.mp4", "tools/webui_probe/build/generated.py",
        "docs/validation/2026-10-20-windows-trim-result.json",
        "docs/validation/other-trim-candidates.json", "docs/validation/live-customer.png",
    ]
    for relative in excluded:
        _write(source_repo, relative)
    _track(source_repo, *expected, *excluded)
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "PUBLIC_SOURCE_MANIFEST.json").read_text())
    assert {item["path"] for item in manifest["included"]} == set(expected)
    assert {item["path"] for item in manifest["excluded"]} == set(excluded)
    assert (output / expected[-1]).read_text(encoding="utf-8") == trim


def test_manifest_identifies_missing_acceptance_document_and_media_closure(source_repo, tmp_path):
    docs = [
        "docs/acceptance/live-douyin-final.md", "docs/release/V1_RELEASE_CHECKLIST.md",
        "docs/acceptance/v1-matrix.md", "docs/acceptance/windows-install.md",
        "docs/acceptance/macos-install.md",
    ]
    for relative in ["tests/acceptance/test_build.py", *docs,
                     "src/yijianjifa/webui/assets/app-icon.png",
                     "src/yijianjifa/webui/assets/capability-test.mp4",
                     "src/yijianjifa/webui/assets/voice_samples/synthetic.mp3"]:
        _write(source_repo, relative)
    _track(source_repo, ".")
    output = tmp_path / "candidate"

    result = _export(source_repo, output)

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "PUBLIC_SOURCE_MANIFEST.json").read_text())
    assert "known_release_closure_gaps" in manifest
    gaps = {item["path"] for item in manifest["known_release_closure_gaps"]}
    assert set(docs) <= gaps
    assert "src/yijianjifa/webui/assets/app-icon.png" in gaps
    assert "src/yijianjifa/webui/assets/capability-test.mp4" in gaps
    assert "src/yijianjifa/webui/assets/voice_samples/**/*.mp3" in gaps
    assert manifest["release_ready"] is False
    assert not any((output / path).exists() for path in docs)
