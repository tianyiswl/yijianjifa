from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yijianjifa.features.publish.schedule import ScheduleRule


class FavoriteMusicChoice(BaseModel):
    """Account-scoped platform music selection.

    This describes a platform-side choice only. It never represents audio that
    should be mixed into the finished video's soundtrack.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    mode: Literal["none", "fixed", "random"] = "none"
    ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_ids(self) -> "FavoriteMusicChoice":
        if self.mode == "none" and self.ids:
            raise ValueError("none music choice cannot contain ids")
        if self.mode == "fixed" and len(self.ids) != 1:
            raise ValueError("fixed music choice requires exactly one id")
        if self.mode == "random" and not self.ids:
            raise ValueError("random music choice requires at least one id")
        return self

    @classmethod
    def none(cls) -> "FavoriteMusicChoice":
        return cls()

    @classmethod
    def fixed(cls, music_id: str) -> "FavoriteMusicChoice":
        return cls(mode="fixed", ids=(str(music_id),))

    @classmethod
    def random(cls, ids: tuple[str, ...] | list[str]) -> "FavoriteMusicChoice":
        return cls(mode="random", ids=tuple(ids))


class CommerceDraft(BaseModel):
    """Mutable-by-replacement draft for one commerce publishing batch.

    The schema intentionally has one scalar ``account_id``. This prevents a
    legacy multi-account commerce shape from reaching task confirmation.
    """

    model_config = ConfigDict(frozen=True, strict=True, arbitrary_types_allowed=True, extra="forbid")

    account_id: str | None = None
    schedule: ScheduleRule | None = None
    selected_video_ids: tuple[str, ...] = ()
    store_pool: tuple[str, ...] = ()
    store_assignments: dict[str, str] = Field(default_factory=dict)
    manual_store_overrides: dict[str, str] = Field(default_factory=dict)
    music_choice: FavoriteMusicChoice = Field(default_factory=FavoriteMusicChoice.none)
    declarations: dict[str, str] = Field(default_factory=dict)
    per_video_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_account(self) -> "CommerceDraft":
        if self.account_id is not None and not self.account_id.strip():
            raise ValueError("account_id cannot be blank")
        return self

    def switch_account(self, new_id: str, *, confirmed: bool) -> "CommerceDraft":
        """Return a draft for another account after explicit confirmation.

        Video selection and the common schedule are account-independent and are
        preserved. Store, music, declarations and per-video account settings
        must be chosen again for the new account.
        """

        if not confirmed:
            return self
        new_id = str(new_id).strip()
        if not new_id:
            raise ValueError("new account id is required")
        if new_id == self.account_id:
            return self
        return self.model_copy(
            deep=True,
            update={
                "account_id": new_id,
                "store_pool": (),
                "store_assignments": {},
                "manual_store_overrides": {},
                "music_choice": FavoriteMusicChoice.none(),
                "declarations": {},
                "per_video_overrides": {},
            },
        )
