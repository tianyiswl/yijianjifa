"""Verify collected license bytes; never decide legal or distribution readiness.

Reads only explicit manifests and their text files. Optional ZIP verification
reads the unique bundled licenses/manifest.json and its referenced members;
it does not extract, execute, upload or inspect user data in the archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LICENSES = ROOT / "packaging/resources/licenses"


def relative_name(value: str) -> str:
    # Existing Windows recognition inventories use native backslashes.
    # Normalize those separators while rejecting Windows drives and traversal.
    if PureWindowsPath(value).drive:
        raise ValueError("Unsafe license manifest path")
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts
            or path.as_posix() != value):
        raise ValueError("Unsafe license manifest path")
    return value


def verify_files(directory: Path) -> dict:
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    seen = set()
    for row in manifest["files"]:
        name = relative_name(row["file"])
        if name in seen:
            raise ValueError("Duplicate license manifest path: " + name)
        seen.add(name)
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise ValueError("License file is missing or escapes its directory: " + name)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("License SHA-256 mismatch: " + name)
        if "size_bytes" in row and len(data) != row["size_bytes"]:
            raise ValueError("License size mismatch: " + name)
    return {"checked_files": len(seen), "all_hashes_match": True,
            "manifest_release_ready": manifest.get("release_ready"),
            "scope": "Collected file integrity only; no distribution approval."}


def verify_archive(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        names = [entry.filename for entry in entries]
        if len(set(names)) != len(names):
            raise ValueError("Archive contains duplicate members")
        candidates = [name for name in names if name.endswith("/licenses/manifest.json")]
        if len(candidates) != 1:
            raise ValueError("Expected one bundled license manifest")
        manifest_name = candidates[0]
        manifest = json.loads(bundle.read(manifest_name))
        prefix = manifest_name.rsplit("/", 1)[0] + "/"
        seen = {}
        for row in manifest["files"]:
            name = relative_name(row["file"])
            if name in seen and seen[name] != row["sha256"]:
                raise ValueError("Conflicting bundled license manifest path: " + name)
            seen[name] = row["sha256"]
            data = bundle.read(prefix + name)
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Bundled license SHA-256 mismatch: " + name)
        return {"checked_entries": len(manifest["files"]), "checked_files": len(seen),
                "duplicate_manifest_references": len(manifest["files"]) - len(seen),
                "all_hashes_match": True,
                "manifest_release_ready": manifest.get("release_ready"),
                "scope": "Referenced license text bytes only; not native dependency closure."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Optional already-downloaded target ZIP")
    args = parser.parse_args()
    result = {"materials": verify_files(LICENSES),
              "supplements": verify_files(LICENSES / "release-supplements"),
              "release_ready": False,
              "reason": "Byte verification cannot clear the documented distribution gates."}
    if args.archive is not None:
        result["archive_materials"] = verify_archive(args.archive)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
