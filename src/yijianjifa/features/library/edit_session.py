from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from yijianjifa.features.library.copy_service import CopyDraft, CopyRepository


class SwitchDecision(str, Enum):
    SAVE_AND_CONTINUE = "save_and_continue"
    DISCARD = "discard"
    KEEP_EDITING = "keep_editing"


@dataclass(frozen=True, slots=True)
class SwitchPrompt:
    target_id: str
    choices: set[SwitchDecision]


class CopyEditSession:
    """UI-independent copy editor state with explicit unsaved-change decisions."""

    _EDITABLE_FIELDS = {"title", "voice", "body", "tags", "full"}

    def __init__(self, repository: CopyRepository):
        self.repository = repository
        self._base: CopyDraft | None = None
        self._values: dict[str, str] = {}
        self._pending_target: str | None = None

    @property
    def current(self) -> CopyDraft | None:
        if self._base is None:
            return None
        return CopyDraft(
            id=self._base.id,
            title=self._values["title"],
            voice=self._values["voice"],
            body=self._values["body"],
            tags=self._values["tags"],
            full=self._values["full"],
        )

    @property
    def dirty(self) -> bool:
        if self._base is None:
            return False
        current = self.current
        assert current is not None
        return current.content_tuple != self._base.content_tuple

    def open(self, copy_id: str) -> CopyDraft:
        record = self.repository.get(copy_id)
        if record is None:
            raise KeyError(copy_id)
        self._load(record)
        self._pending_target = None
        return record

    def update_field(self, field: str, value: str) -> None:
        if self._base is None:
            raise RuntimeError("no copy is open")
        if field not in self._EDITABLE_FIELDS:
            raise ValueError(f"field is not editable: {field}")
        self._values[field] = str(value)

    def save(self) -> CopyDraft:
        current = self.current
        if current is None:
            raise RuntimeError("no copy is open")
        saved = self.repository.update(current)
        self._load(saved)
        return saved

    def request_open(self, copy_id: str) -> SwitchPrompt | None:
        if self._base is None or self._base.id == copy_id:
            self.open(copy_id)
            return None
        if not self.dirty:
            self.open(copy_id)
            return None
        self._pending_target = copy_id
        return SwitchPrompt(
            target_id=copy_id,
            choices={
                SwitchDecision.SAVE_AND_CONTINUE,
                SwitchDecision.DISCARD,
                SwitchDecision.KEEP_EDITING,
            },
        )

    def resolve_switch(self, decision: SwitchDecision) -> CopyDraft:
        if self._pending_target is None:
            raise RuntimeError("no pending switch")
        target = self._pending_target
        if decision is SwitchDecision.KEEP_EDITING:
            self._pending_target = None
            current = self.current
            if current is None:
                raise RuntimeError("no copy is open")
            return current
        if decision is SwitchDecision.SAVE_AND_CONTINUE:
            self.save()
        elif decision is not SwitchDecision.DISCARD:
            raise ValueError(decision)
        self._pending_target = None
        return self.open(target)

    def _load(self, record: CopyDraft) -> None:
        self._base = record
        self._values = {
            "title": record.title,
            "voice": record.voice,
            "body": record.body,
            "tags": record.tags,
            "full": record.full,
        }
