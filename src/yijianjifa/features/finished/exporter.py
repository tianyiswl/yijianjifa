from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
from typing import Iterable, Protocol


class ExportableFinished(Protocol):
    id: str
    display_name: str
    title: str
    body: str
    tags: str

    @property
    def effective_path(self) -> Path: ...


@dataclass(frozen=True, slots=True)
class ExportManifestEntry:
    finished_id: str
    video_basename: str
    metadata_basename: str | None
    title: str


def export_finished_records(
    records: Iterable[ExportableFinished],
    *,
    destination: str | Path,
    include_metadata: bool,
) -> list[ExportManifestEntry]:
    target_dir = Path(destination)
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[ExportManifestEntry] = []
    for record in records:
        source = record.effective_path
        if not source.is_file():
            raise FileNotFoundError(source)
        stem = _safe_stem(record.display_name or source.name)
        base = f"{record.id[:8]}_{stem}"
        # Exclusively reserve both names; a repeated export never overwrites
        # earlier videos, metadata, or a concurrently created file.
        counter = 0
        while True:
            suffix = f"_{counter}" if counter else ""
            video_name = f"{base}{suffix}{source.suffix.lower() or '.mp4'}"
            metadata_name = f"{base}{suffix}.json" if include_metadata else None
            owned = []
            try:
                for name in [video_name] + ([metadata_name] if metadata_name else []):
                    with (target_dir / name).open('xb'):
                        pass
                    owned.append(target_dir / name)
            except FileExistsError:
                for path in owned:
                    path.unlink()
                counter += 1
                continue
            except Exception:
                for path in owned:
                    path.unlink()
                raise
            break
        try:
            shutil.copy2(source, target_dir / video_name)
            if metadata_name:
                payload = {"finished_id": record.id, "title": record.title,
                           "body": record.body, "tags": record.tags}
                (target_dir / metadata_name).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            for path in owned:
                path.unlink(missing_ok=True)
            raise
        manifest.append(
            ExportManifestEntry(
                finished_id=record.id,
                video_basename=video_name,
                metadata_basename=metadata_name,
                title=record.title,
            )
        )
    return manifest


def _safe_stem(value: str) -> str:
    stem = Path(value).stem.strip() or "video"
    return re.sub(r"[\\/:*?\"<>|]+", "_", stem)
