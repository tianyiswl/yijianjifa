from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import re
import threading
from typing import Iterable

from sqlalchemy import Engine, Connection, select
from sqlalchemy.orm import Session

from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.library.active_references import ensure_not_referenced
from yijianjifa.os_integration.paths import AppPaths
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import CategoryLink, HistoricalIdentity, MediaAsset

_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
_MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_MUTATION_LOCKS = {}
_MUTATION_LOCK_GUARD = threading.Lock()


def media_mutation_lock(engine):
    """Share file registration/deletion admission with owned download files."""
    key = str(getattr(engine, 'engine', engine).url)
    with _MUTATION_LOCK_GUARD:
        return _MUTATION_LOCKS.setdefault(key, threading.RLock())


@dataclass(frozen=True, slots=True)
class MediaImportDecision:
    asset_id: str
    reused_existing: bool
    copied_to_managed_storage: bool


@dataclass(frozen=True, slots=True)
class MediaAssetRecord:
    id: str
    kind: str
    display_name: str
    original_path: Path
    managed_path: Path | None
    content_hash: str
    missing: bool

    @property
    def effective_path(self) -> Path:
        return self.managed_path or self.original_path


class HistoryIdentityRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def identity_seen(self, content_hash: str) -> bool:
        with session_scope(self.engine) as session:
            row = session.scalar(
                select(HistoricalIdentity.id).where(HistoricalIdentity.content_hash == content_hash).limit(1)
            )
            return row is not None


class MediaStorageService:
    def __init__(self, engine: Engine, app_paths: AppPaths, *, kind: str):
        if kind not in {"video", "music"}:
            raise ValueError("kind must be 'video' or 'music'")
        self.engine = engine
        self.app_paths = app_paths
        self.kind = kind
        self.history = HistoryIdentityRepository(engine)

    @property
    def supported_extensions(self) -> set[str]:
        return _VIDEO_EXTENSIONS if self.kind == "video" else _MUSIC_EXTENSIONS

    def import_media(
        self,
        path: str | Path,
        *,
        keep_copy: bool,
        category_ids: Iterable[str],
    ) -> MediaAssetRecord:
        with media_mutation_lock(self.engine):
            return self._import_media_locked(path, keep_copy=keep_copy, category_ids=category_ids)

    def _import_media_locked(self, path, *, keep_copy, category_ids):
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in self.supported_extensions:
            raise ValueError(f"unsupported {self.kind} file: {source.suffix}")

        content_hash = sha256_file(source)
        with session_scope(self.engine) as session:
            asset = session.scalar(
                select(MediaAsset).where(
                    MediaAsset.kind == self.kind,
                    MediaAsset.content_hash == content_hash,
                )
            )
            if asset is None:
                managed_path = self._copy_into_managed_storage(source, content_hash) if keep_copy else None
                asset = MediaAsset(
                    kind=self.kind,
                    display_name=source.name,
                    original_path=str(source),
                    managed_path=str(managed_path) if managed_path else None,
                    content_hash=content_hash,
                    missing=False,
                )
                session.add(asset)
                session.flush()
            else:
                # Duplicate import only adds categories; it never relinks or migrates.
                asset.missing = not Path(asset.managed_path or asset.original_path).exists()

            self._remember_identity(session, content_hash)
            self._append_categories(session, asset.id, category_ids)
            session.flush()
            return self._to_record(asset)

    def import_prepared(self, connection: Connection, path: Path, sha256: str, category_ids: Iterable[str]) -> MediaAssetRecord:
        """Register a trusted, preverified owned video within the caller's write.

        Caller holds media_mutation_lock and verifies identity/hash/probe before
        opening the transaction. This entry never copies, hashes or probes.
        It is backend-only and deliberately keeps the owned file as original.
        """
        if (self.kind != 'video' or not isinstance(connection, Connection) or not connection.in_transaction()
                or not isinstance(path, Path) or not path.is_absolute() or path.suffix.lower() not in _VIDEO_EXTENSIONS
                or type(sha256) is not str or not re.fullmatch(r'[a-f0-9]{64}', sha256)):
            raise ValueError('invalid prepared video')
        with Session(bind=connection, expire_on_commit=False) as session:
            asset = session.scalar(select(MediaAsset).where(MediaAsset.kind == 'video', MediaAsset.content_hash == sha256))
            if asset is None:
                asset = MediaAsset(kind='video', display_name=path.name, original_path=str(path),
                                   managed_path=None, content_hash=sha256, missing=False)
                session.add(asset); session.flush()
            self._remember_identity(session, sha256)
            self._append_categories(session, asset.id, category_ids)
            session.flush()
            return self._to_record(asset)

    def import_folder(
        self,
        path: str | Path,
        *,
        recursive: bool = True,
        keep_copy: bool,
        category_ids: Iterable[str],
    ) -> list[MediaAssetRecord]:
        folder = Path(path).expanduser().resolve()
        if not folder.is_dir():
            raise NotADirectoryError(folder)
        iterator = folder.rglob("*") if recursive else folder.glob("*")
        files = sorted(
            p for p in iterator if p.is_file() and p.suffix.lower() in self.supported_extensions
        )
        return [
            self.import_media(p, keep_copy=keep_copy, category_ids=category_ids)
            for p in files
        ]

    def get(self, asset_id: str) -> MediaAssetRecord | None:
        with session_scope(self.engine) as session:
            asset = session.get(MediaAsset, asset_id)
            return None if asset is None else self._to_record(asset)

    def category_ids(self, asset_id: str) -> set[str]:
        with session_scope(self.engine) as session:
            values = session.scalars(
                select(CategoryLink.category_id).where(
                    CategoryLink.target_type == self.kind,
                    CategoryLink.target_id == asset_id,
                )
            ).all()
            return set(values)

    def append_categories(self, asset_id: str, category_ids: Iterable[str]) -> None:
        with session_scope(self.engine) as session:
            if session.get(MediaAsset, asset_id) is None:
                raise KeyError(asset_id)
            self._append_categories(session, asset_id, category_ids)

    def set_categories(self, asset_id: str, category_ids: Iterable[str]) -> None:
        with session_scope(self.engine) as session:
            if session.get(MediaAsset, asset_id) is None:
                raise KeyError(asset_id)
            session.query(CategoryLink).filter(
                CategoryLink.target_type == self.kind,
                CategoryLink.target_id == asset_id,
            ).delete(synchronize_session=False)
            self._append_categories(session, asset_id, category_ids)

    def list_assets(
        self,
        *,
        search: str = "",
        category_ids: Iterable[str] | None = None,
        missing: bool | None = None,
    ) -> list[MediaAssetRecord]:
        selected_categories = list(dict.fromkeys(category_ids or []))
        with session_scope(self.engine) as session:
            statement = select(MediaAsset).where(MediaAsset.kind == self.kind)
            if search.strip():
                statement = statement.where(
                    MediaAsset.display_name.ilike(f"%{search.strip()}%")
                )
            if missing is not None:
                statement = statement.where(MediaAsset.missing == missing)
            if selected_categories:
                statement = (
                    statement.join(
                        CategoryLink,
                        (CategoryLink.target_id == MediaAsset.id)
                        & (CategoryLink.target_type == self.kind),
                    )
                    .where(CategoryLink.category_id.in_(selected_categories))
                    .distinct()
                )
            rows = session.scalars(statement.order_by(MediaAsset.created_at, MediaAsset.id)).all()
            return [self._to_record(row) for row in rows]

    def rename(self, asset_id: str, display_name: str) -> MediaAssetRecord:
        from .display_names import name_only
        normalized = name_only(display_name)
        if not normalized:
            raise ValueError("display name cannot be blank")
        with session_scope(self.engine) as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                raise KeyError(asset_id)
            asset.display_name = normalized
            session.flush()
            return self._to_record(asset)

    def delete_media_record(self, asset_id: str, *, delete_managed_file: bool = True) -> None:
        managed: Path | None = None
        with session_scope(self.engine) as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                return
            ensure_not_referenced(session, [asset.id, asset.content_hash,
                                           asset.original_path, asset.managed_path])
            if asset.managed_path:
                managed = Path(asset.managed_path)
            session.query(CategoryLink).filter(
                CategoryLink.target_type == self.kind,
                CategoryLink.target_id == asset.id,
            ).delete(synchronize_session=False)
            session.delete(asset)
        if delete_managed_file and managed is not None and managed.exists():
            # Only files stored under the application's managed-media root are owned.
            try:
                managed.resolve().relative_to(self.app_paths.managed_media_dir.resolve())
            except ValueError as exc:
                raise RuntimeError("refusing to delete non-managed file") from exc
            managed.unlink()

    def refresh_missing_state(self, asset_id: str) -> MediaAssetRecord:
        with session_scope(self.engine) as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                raise KeyError(asset_id)
            effective = Path(asset.managed_path) if asset.managed_path else Path(asset.original_path)
            asset.missing = not effective.exists()
            session.flush()
            return self._to_record(asset)

    def _copy_into_managed_storage(self, source: Path, content_hash: str) -> Path:
        kind_dir = self.app_paths.managed_media_dir / self.kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        target = kind_dir / f"{content_hash}{source.suffix.lower()}"
        if not target.exists():
            shutil.copy2(source, target)
        return target

    def _remember_identity(self, session, content_hash: str) -> None:
        found = session.scalar(
            select(HistoricalIdentity).where(
                HistoricalIdentity.identity_kind == f"media:{self.kind}",
                HistoricalIdentity.content_hash == content_hash,
            )
        )
        if found is None:
            session.add(
                HistoricalIdentity(
                    identity_kind=f"media:{self.kind}",
                    content_hash=content_hash,
                )
            )

    def _append_categories(self, session, asset_id: str, category_ids: Iterable[str]) -> None:
        for category_id in dict.fromkeys(category_ids):
            found = session.scalar(
                select(CategoryLink).where(
                    CategoryLink.category_id == category_id,
                    CategoryLink.target_type == self.kind,
                    CategoryLink.target_id == asset_id,
                )
            )
            if found is None:
                session.add(
                    CategoryLink(
                        category_id=category_id,
                        target_type=self.kind,
                        target_id=asset_id,
                    )
                )

    @staticmethod
    def _to_record(asset: MediaAsset) -> MediaAssetRecord:
        return MediaAssetRecord(
            id=asset.id,
            kind=asset.kind,
            display_name=asset.display_name,
            original_path=Path(asset.original_path),
            managed_path=Path(asset.managed_path) if asset.managed_path else None,
            content_hash=asset.content_hash,
            missing=asset.missing,
        )
