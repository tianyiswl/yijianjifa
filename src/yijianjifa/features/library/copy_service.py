from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import Engine, select
from yijianjifa.features.library.active_references import ensure_not_referenced

from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Category, CategoryLink, CopyRecord


@dataclass(frozen=True, slots=True)
class CopyDraft:
    id: str
    title: str
    voice: str
    body: str
    tags: str
    full: str

    @property
    def content_tuple(self) -> tuple[str, str, str, str, str]:
        return (self.title, self.voice, self.body, self.tags, self.full)


@dataclass(frozen=True, slots=True)
class CategoryRecord:
    id: str
    kind: str
    name: str


class CopyRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, *, id: str, title: str, voice: str, body: str, tags: str, full: str) -> CopyDraft:
        values = self._normalize_fields(id, title, voice, body, tags, full)
        with session_scope(self.engine) as session:
            if session.get(CopyRecord, values.id) is not None:
                raise ValueError(f"copy id already exists: {values.id}")
            row = CopyRecord(
                id=values.id,
                title=values.title,
                voice=values.voice,
                body=values.body,
                tags=values.tags,
                full=values.full,
            )
            session.add(row)
            session.flush()
            return self._to_draft(row)

    def get(self, copy_id: str) -> CopyDraft | None:
        with session_scope(self.engine) as session:
            row = session.get(CopyRecord, copy_id)
            return None if row is None else self._to_draft(row)

    def list_by_ids(self, copy_ids: Iterable[str]) -> list[CopyDraft]:
        ids = list(dict.fromkeys(copy_ids))
        if not ids:
            return []
        with session_scope(self.engine) as session:
            rows = session.scalars(select(CopyRecord).where(CopyRecord.id.in_(ids))).all()
            by_id = {row.id: self._to_draft(row) for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    def list_all(self) -> list[CopyDraft]:
        with session_scope(self.engine) as session:
            rows = session.scalars(select(CopyRecord).order_by(CopyRecord.id)).all()
            return [self._to_draft(row) for row in rows]

    def update(self, draft: CopyDraft) -> CopyDraft:
        normalized = self._normalize_fields(
            draft.id, draft.title, draft.voice, draft.body, draft.tags, draft.full
        )
        with session_scope(self.engine) as session:
            row = session.get(CopyRecord, normalized.id)
            if row is None:
                raise KeyError(normalized.id)
            row.title = normalized.title
            row.voice = normalized.voice
            row.body = normalized.body
            row.tags = normalized.tags
            row.full = normalized.full
            session.flush()
            return self._to_draft(row)

    def delete(self, copy_id: str) -> None:
        with session_scope(self.engine) as session:
            row = session.get(CopyRecord, copy_id)
            if row is None:
                return
            ensure_not_referenced(session, [copy_id])
            session.query(CategoryLink).filter(
                CategoryLink.target_type == "copy",
                CategoryLink.target_id == copy_id,
            ).delete(synchronize_session=False)
            session.delete(row)

    def find_by_content(self, draft: CopyDraft) -> CopyDraft | None:
        with session_scope(self.engine) as session:
            row = session.scalar(
                select(CopyRecord).where(
                    CopyRecord.title == draft.title,
                    CopyRecord.voice == draft.voice,
                    CopyRecord.body == draft.body,
                    CopyRecord.tags == draft.tags,
                    CopyRecord.full == draft.full,
                ).limit(1)
            )
            return None if row is None else self._to_draft(row)

    def set_categories(self, copy_id: str, category_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(category_ids))
        with session_scope(self.engine) as session:
            if session.get(CopyRecord, copy_id) is None:
                raise KeyError(copy_id)
            session.query(CategoryLink).filter(
                CategoryLink.target_type == "copy",
                CategoryLink.target_id == copy_id,
            ).delete(synchronize_session=False)
            for category_id in ids:
                session.add(
                    CategoryLink(
                        category_id=category_id,
                        target_type="copy",
                        target_id=copy_id,
                    )
                )

    def category_ids(self, copy_id: str) -> set[str]:
        with session_scope(self.engine) as session:
            values = session.scalars(
                select(CategoryLink.category_id).where(
                    CategoryLink.target_type == "copy",
                    CategoryLink.target_id == copy_id,
                )
            ).all()
            return set(values)

    @staticmethod
    def _normalize_fields(id: str, title: str, voice: str, body: str, tags: str, full: str) -> CopyDraft:
        return CopyDraft(
            id=str(id).strip(),
            title=str(title or "").strip(),
            voice=str(voice or "").strip(),
            body=str(body or "").strip(),
            tags=str(tags or "").strip(),
            full=str(full or "").strip(),
        )

    @staticmethod
    def _to_draft(row: CopyRecord) -> CopyDraft:
        return CopyDraft(
            id=row.id,
            title=row.title,
            voice=row.voice,
            body=row.body,
            tags=row.tags,
            full=row.full,
        )


class CategoryRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, kind: str, name: str) -> CategoryRecord:
        normalized = name.strip()
        if not normalized:
            raise ValueError("category name cannot be blank")
        with session_scope(self.engine) as session:
            existing = session.scalar(
                select(Category).where(Category.kind == kind, Category.name == normalized)
            )
            if existing is not None:
                return self._to_record(existing)
            row = Category(kind=kind, name=normalized)
            session.add(row)
            session.flush()
            return self._to_record(row)

    def rename(self, category_id: str, name: str) -> CategoryRecord:
        normalized = name.strip()
        if not normalized:
            raise ValueError("category name cannot be blank")
        with session_scope(self.engine) as session:
            row = session.get(Category, category_id)
            if row is None:
                raise KeyError(category_id)
            row.name = normalized
            session.flush()
            return self._to_record(row)

    def delete(self, category_id: str) -> None:
        with session_scope(self.engine) as session:
            row = session.get(Category, category_id)
            if row is None:
                return
            session.query(CategoryLink).filter(CategoryLink.category_id == category_id).delete(
                synchronize_session=False
            )
            session.delete(row)

    def list(self, kind: str) -> list[CategoryRecord]:
        with session_scope(self.engine) as session:
            rows = session.scalars(
                select(Category).where(Category.kind == kind).order_by(Category.name)
            ).all()
            return [self._to_record(row) for row in rows]

    @staticmethod
    def _to_record(row: Category) -> CategoryRecord:
        return CategoryRecord(id=row.id, kind=row.kind, name=row.name)
