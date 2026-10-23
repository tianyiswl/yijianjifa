from __future__ import annotations

from contextlib import contextmanager
from datetime import timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Iterable, Mapping
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import Engine, func, or_, select

from yijianjifa.features.image_posts.models import ImagePost, ImagePostImage, ImagePostImageRecord, ImagePostRecord
from yijianjifa.os_integration.paths import AppPaths
from yijianjifa.storage.db import session_scope

# Storage safeguards, not a claim about any platform's submission limits.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_GROUP_IMAGES = 100
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "BMP"}


def natural_key(value: str | Path):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r"(\d+)", Path(value).name))


def source_path(value: str | Path) -> Path:
    path = Path(value).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError(f"{path.name}：不支持符号链接，请选择原始图片")
    if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"{path.name}：请选择 JPG、PNG、WebP 或 BMP 图片")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"{path.name}：图片大小超过 25 MB")
    return path.resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_image(value: str | Path):
    path = source_path(value)
    try:
        opened = Image.open(path)
    except Image.DecompressionBombError as error:
        raise ValueError(f"{path.name}：图片损坏或像素尺寸过大")
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise ValueError(f"{path.name}：图片损坏，无法读取") from error
    with opened:
        if opened.format not in SUPPORTED_IMAGE_FORMATS:
            raise ValueError(f"{path.name}：图片格式无法读取")
        if opened.width <= 0 or opened.height <= 0 or opened.width * opened.height > MAX_IMAGE_PIXELS:
            raise ValueError(f"{path.name}：图片损坏或像素尺寸过大")
        if getattr(opened, "n_frames", 1) != 1:
            raise ValueError(f"{path.name}：请选择静态图片")
        try:
            opened.load()
            # A detached, decoded copy; EXIF orientation cannot be applied twice
            # by the browser after the managed image is re-encoded.
            image = ImageOps.exif_transpose(opened)
        except (OSError, SyntaxError, ValueError) as error:
            raise ValueError(f"{path.name}：图片损坏，无法读取") from error
    return path, image


def _content_hash(images: Iterable[ImagePostImage]) -> str:
    payload = [{"hash": image.content_hash, "width": image.width, "height": image.height} for image in images]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


class ImagePostService:
    def __init__(self, engine: Engine, paths: AppPaths):
        self.engine = engine
        self.paths = paths
        self.root = paths.data_dir / "image_posts"
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _work_session(self, session=None):
        """A supplied session belongs to the caller's atomic import/receipt unit."""
        if session is not None:
            yield session
        else:
            with session_scope(self.engine) as owned:
                yield owned

    def _copy_images(self, files: Iterable[str | Path], post_id: str, offset=0):
        values = sorted(list(files), key=natural_key)
        if not values or len(values) + offset > MAX_GROUP_IMAGES:
            raise ValueError(f"一篇图文需要 1 至 {MAX_GROUP_IMAGES} 张图片")
        target = self.root / post_id
        target.mkdir(parents=True, exist_ok=True)
        copied = []
        try:
            for position, value in enumerate(values, start=offset):
                source, image = inspect_image(value)
                image_id = str(uuid.uuid4())
                fmt = "PNG" if "A" in image.getbands() or "transparency" in image.info else "JPEG"
                dest = target / f"{image_id}.{'png' if fmt == 'PNG' else 'jpg'}"
                thumb = target / f"{image_id}.thumb.png"
                copied.extend((dest, thumb))
                with image:
                    with image.convert("RGBA" if fmt == "PNG" else "RGB") as normalized:
                        normalized.save(dest, format=fmt, quality=95)
                        with normalized.copy() as preview:
                            preview.thumbnail((420, 420), Image.Resampling.LANCZOS)
                            preview.save(thumb, format="PNG")
                    row = ImagePostImage(id=image_id, post_id=post_id, position=position, path=str(dest), thumbnail_path=str(thumb), content_hash=sha256_file(dest), width=image.width, height=image.height, format=fmt, original_name=source.name)
                yield row
        except Exception:
            self._remove_paths(copied)
            raise

    def import_group(self, files: Iterable[str | Path], *, display_name="", title="", body="", tags="", _session=None) -> ImagePostRecord:
        values = list(files)
        post_id = str(uuid.uuid4())
        try:
            images = list(self._copy_images(values, post_id))
            name = str(display_name or Path(values[0]).stem).strip()
            if not name or len(name) > 255:
                raise ValueError("图文名称需要 1 至 255 个字符")
            row = ImagePost(id=post_id, display_name=name, title=str(title or ""), body=str(body or ""), tags=str(tags or ""), content_hash=_content_hash(images))
            with self._work_session(_session) as session:
                session.add(row)
                session.flush()
                session.add_all(images)
                session.flush()
                record = self._record(row, images)
            return record
        except Exception:
            self._remove_directory(post_id)
            raise

    def get(self, post_id: str, *, _session=None) -> ImagePostRecord | None:
        with self._work_session(_session) as session:
            row = session.get(ImagePost, post_id)
            return None if row is None else self._record(row, self._images(session, post_id))

    def list(self, query="", offset=0, limit=20) -> tuple[list[ImagePostRecord], int]:
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 500))
        statement = select(ImagePost)
        term = str(query or "").strip()
        if term:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(or_(*(getattr(ImagePost, field).like(pattern, escape="\\") for field in ("display_name", "title", "body", "tags"))))
        with session_scope(self.engine) as session:
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            rows = session.scalars(statement.order_by(ImagePost.created_at.desc(), ImagePost.id.desc()).offset(offset).limit(limit)).all()
            return [self._record(row, self._images(session, row.id)) for row in rows], total

    def list_by_ids(self, post_ids: Iterable[str]) -> list[ImagePostRecord]:
        return [record for post_id in dict.fromkeys(post_ids) if (record := self.get(post_id)) is not None]

    def save(self, post_id: str, values: Mapping, *, _session=None) -> ImagePostRecord:
        if _session is not None and "image_ids" in values:
            # Shared confirmations change metadata only. Image-file deletion
            # must remain after the owning transaction has committed.
            raise ValueError("事务内文案确认不能修改图片顺序")
        removed = []
        with self._work_session(_session) as session:
            row = session.get(ImagePost, post_id)
            if row is None:
                raise KeyError(post_id)
            images = self._images(session, post_id)
            if "image_ids" in values:
                ids = list(values["image_ids"])
                by_id = {image.id: image for image in images}
                if not ids or len(ids) != len(set(ids)) or not set(ids) <= set(by_id):
                    raise ValueError("图片顺序无效：请保留至少一张本篇图片，且不要重复选择")
                retained = [by_id[image_id] for image_id in ids]
                for image in images:
                    if image.id not in ids:
                        removed.extend((Path(image.path), Path(image.thumbnail_path)))
                        session.delete(image)
                    else:
                        image.position = -image.position - 1
                session.flush()
                for index, image in enumerate(retained):
                    image.position = index
                images = retained
                row.content_hash = _content_hash(images)
            for field in ("display_name", "title", "body", "tags"):
                if field in values:
                    value = str(values[field] or "")
                    if field == "display_name":
                        value = value.strip()
                        if not value or len(value) > 255:
                            raise ValueError("图文名称需要 1 至 255 个字符")
                    setattr(row, field, value)
            session.flush()
            result = self._record(row, images)
        self._remove_paths(removed)
        return result

    def add_images(self, post_id: str, files: Iterable[str | Path]) -> ImagePostRecord:
        new_images = []
        try:
            with session_scope(self.engine) as session:
                row = session.get(ImagePost, post_id)
                if row is None:
                    raise KeyError(post_id)
                images = self._images(session, post_id)
                new_images = list(self._copy_images(files, post_id, len(images)))
                session.add_all(new_images)
                images.extend(new_images)
                row.content_hash = _content_hash(images)
                session.flush()
                result = self._record(row, images)
            return result
        except Exception:
            self._remove_paths([Path(value) for image in new_images for value in (image.path, image.thumbnail_path)])
            raise

    def delete(self, ids: Iterable[str] | str) -> None:
        values = list(dict.fromkeys([ids] if isinstance(ids, str) else ids))
        removed = []
        with session_scope(self.engine) as session:
            for post_id in values:
                row = session.get(ImagePost, post_id)
                if row is None:
                    continue
                for image in self._images(session, post_id):
                    session.delete(image)
                session.delete(row)
                removed.append(post_id)
        for post_id in removed:
            self._remove_directory(post_id)

    def _images(self, session, post_id):
        return list(session.scalars(select(ImagePostImage).where(ImagePostImage.post_id == post_id).order_by(ImagePostImage.position)).all())

    @staticmethod
    def _record(row, images):
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return ImagePostRecord(row.id, row.display_name, row.title, row.body, row.tags, row.content_hash, created, tuple(ImagePostImageRecord(image.id, image.position, Path(image.path), image.content_hash, image.width, image.height, image.format, image.original_name, Path(image.thumbnail_path)) for image in images))

    def _remove_paths(self, paths):
        root = self.root.resolve()
        for path in paths:
            path = Path(path)
            if path.resolve().is_relative_to(root) and not path.is_symlink():
                path.unlink(missing_ok=True)

    def _remove_directory(self, post_id):
        directory = self.root / post_id
        if directory.resolve().parent == self.root.resolve() and not directory.is_symlink():
            shutil.rmtree(directory, ignore_errors=True)
