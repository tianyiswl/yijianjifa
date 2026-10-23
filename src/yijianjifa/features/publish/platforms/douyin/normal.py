from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from yijianjifa.features.publish.executor import SubmissionEvidence
from .submission import submit_page


class PageContract(Protocol):
    def set_input_files(self, selector: str, path: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def is_visible(self, selector: str) -> bool: ...
    def text_content(self, selector: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class NormalPublishRequest:
    video_path: str
    title: str
    body: str
    tags: str
    scheduled_time_text: str | None = None
    expected_platform_user_id: str | None = None


class Selectors:
    IDENTITY = "yjjf:identity"
    VIDEO = "yjjf:video"
    SCHEDULE = "yjjf:schedule"
    # All live selectors stay isolated here so page changes do not leak into the
    # domain state machine. These are contract tokens for offline tests until a
    # real authorized page inspection confirms production selectors.
    AUTH_REQUIRED = "yjjf:auth-required"
    TITLE = "yjjf:title"
    BODY = "yjjf:body"
    TAGS = "yjjf:tags"
    FINAL_SUBMIT = "yjjf:final-submit"
    SCHEDULE_CONFIRMATION = "yjjf:schedule-confirmation"
    PUBLIC_SUCCESS = "yjjf:public-success"


class DouyinNormalAdapter:
    platform_id = "douyin"

    def __init__(self, page: PageContract):
        self.page = page

    def prepare_normal(self, request: NormalPublishRequest) -> None:
        self.page.fill(Selectors.TITLE, request.title)
        self.page.fill(Selectors.BODY, request.body)
        self.page.fill(Selectors.TAGS, request.tags)

    def submit_normal(self, request: NormalPublishRequest, *, before_final=None) -> SubmissionEvidence:
        return submit_page(self.page, Selectors, request, self.prepare_normal, before_final=before_final)
