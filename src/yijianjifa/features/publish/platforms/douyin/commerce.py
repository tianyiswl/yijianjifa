from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.commerce.stores import StoreCandidate
from yijianjifa.features.publish.executor import SubmissionEvidence
from .submission import submit_page


class CommercePageContract(Protocol):
    def set_input_files(self, selector: str, path: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def is_visible(self, selector: str) -> bool: ...
    def text_content(self, selector: str) -> str | None: ...
    def commerce_store_rows(
        self, account_id: str, scope: str, query: str, region: str
    ) -> list[StoreCandidate]: ...
    def commerce_store_available(self, account_id: str, store_id: str) -> bool: ...
    def favorite_music_ids(self, account_id: str) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class CommercePublishRequest:
    video_path: str
    title: str
    body: str
    tags: str
    store_id: str
    cover_ready: bool = True
    declaration_required: bool = False
    declaration: str | None = None
    favorite_music_id: str | None = None
    scheduled_time_text: str | None = None
    expected_platform_user_id: str | None = None


class Selectors:
    IDENTITY = "yjjf:commerce:identity"
    VIDEO = "yjjf:commerce:video"
    SCHEDULE = "yjjf:commerce:schedule"
    # Offline contract tokens. Live selectors are intentionally not claimed as
    # verified until an explicitly authorized real Douyin inspection occurs.
    AUTH_REQUIRED = "yjjf:commerce:auth-required"
    TITLE = "yjjf:commerce:title"
    BODY = "yjjf:commerce:body"
    TAGS = "yjjf:commerce:tags"
    STORE = "yjjf:commerce:store"
    DECLARATION = "yjjf:commerce:declaration"
    FAVORITE_MUSIC = "yjjf:commerce:favorite-music"
    FINAL_SUBMIT = "yjjf:commerce:final-submit"
    SCHEDULE_CONFIRMATION = "yjjf:commerce:schedule-confirmation"
    PUBLIC_SUCCESS = "yjjf:commerce:public-success"


def _allowed_scopes(snapshot: CapabilitySnapshot) -> frozenset[str]:
    value = (snapshot.location_scope or "pending").strip().lower()
    if value in {"local_national", "local/national", "local+national", "all"}:
        return frozenset({"local", "national"})
    if value == "local":
        return frozenset({"local"})
    if value == "national":
        return frozenset({"national"})
    return frozenset()


class DouyinCommerceAdapter:
    platform_id = "douyin"

    def __init__(
        self,
        page: CommercePageContract,
        *,
        account_id: str,
        capabilities: CapabilitySnapshot,
    ):
        self.page = page
        self.account_id = str(account_id)
        self.capabilities = capabilities

    def _require_commerce_available(self) -> None:
        if self.capabilities.commerce != "available":
            raise ValueError("commerce capability is not confirmed available")

    def search_stores(self, *, scope: str, query: str, region: str) -> list[StoreCandidate]:
        self._require_commerce_available()
        if scope not in _allowed_scopes(self.capabilities):
            raise ValueError("requested store scope is not available for current account")
        return list(self.page.commerce_store_rows(self.account_id, scope, query, region))

    def list_favorite_music(self) -> list[str]:
        self._require_commerce_available()
        return list(dict.fromkeys(self.page.favorite_music_ids(self.account_id)))

    def prepare_commerce(self, request: CommercePublishRequest) -> None:
        self.page.fill(Selectors.TITLE, request.title)
        self.page.fill(Selectors.BODY, request.body)
        self.page.fill(Selectors.TAGS, request.tags)
        self.page.fill(Selectors.STORE, request.store_id)
        if request.declaration:
            self.page.fill(Selectors.DECLARATION, request.declaration)
        if request.favorite_music_id:
            self.page.fill(Selectors.FAVORITE_MUSIC, request.favorite_music_id)

    def _pre_submit_error(self, request: CommercePublishRequest) -> str | None:
        try:
            self._require_commerce_available()
        except ValueError:
            return "commerce_capability_unavailable"
        if not request.store_id or not self.page.commerce_store_available(self.account_id, request.store_id):
            return "commerce_store_unavailable"
        if request.declaration_required and not (request.declaration or "").strip():
            return "commerce_declaration_required"
        if not request.cover_ready:
            return "commerce_cover_invalid"
        if request.favorite_music_id is not None:
            available_music = set(self.page.favorite_music_ids(self.account_id))
            if request.favorite_music_id not in available_music:
                return "favorite_music_not_in_current_account"
        return None

    def submit_commerce(self, request: CommercePublishRequest, *, before_final=None) -> SubmissionEvidence:
        return submit_page(self.page, Selectors, request, self.prepare_commerce, lambda: self._pre_submit_error(request), before_final=before_final)
