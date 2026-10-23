from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
import re


@dataclass(frozen=True, slots=True)
class PlatformLimits:
    min_hours: float
    max_days: float
    reject_short: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleRule:
    mode: Literal["interval", "daily", "exact"]
    start: datetime
    interval_hours: float
    daily_count: int | None = None
    days: int | None = None
    daily_times: tuple[str, ...] | None = None

    def __post_init__(self):
        if self.daily_times is None:
            return
        if (self.mode != "daily" or type(self.daily_times) is not tuple
                or not 1 <= len(self.daily_times) <= 200
                or len(self.daily_times) != self.daily_count
                or any(type(value) is not str or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None for value in self.daily_times)
                or len(set(self.daily_times)) != len(self.daily_times)):
            raise ValueError("daily times must contain one distinct HH:MM clock per daily item")

    @classmethod
    def interval(cls, *, start: datetime, hours: float) -> "ScheduleRule":
        return cls(mode="interval", start=start, interval_hours=float(hours))

    @classmethod
    def daily(
        cls,
        *,
        start: datetime,
        daily_count: int,
        days: int,
        interval_hours: float,
    ) -> "ScheduleRule":
        return cls(
            mode="daily",
            start=start,
            interval_hours=float(interval_hours),
            daily_count=int(daily_count),
            days=int(days),
        )


@dataclass(frozen=True, slots=True)
class ScheduleItem:
    index: int
    local_time: datetime


@dataclass(frozen=True, slots=True)
class ValidatedScheduleItem:
    index: int
    local_time: datetime
    required_choice: frozenset[str] = frozenset()
    silent_fallback: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleValidationResult:
    items: tuple[ValidatedScheduleItem, ...]
    invalid_indexes: list[int]

    @property
    def ok(self) -> bool:
        return not self.invalid_indexes and all(not item.required_choice for item in self.items)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def build_schedule(rule: ScheduleRule, *, count: int, now: datetime) -> list[ScheduleItem]:
    _require_aware(rule.start, "schedule start")
    _require_aware(now, "now")
    if count < 0:
        raise ValueError("count cannot be negative")
    if rule.interval_hours <= 0:
        raise ValueError("interval hours must be positive")
    if count == 0:
        return []

    if rule.mode == "exact":
        return [ScheduleItem(index=i, local_time=rule.start) for i in range(count)]

    if rule.mode == "interval":
        return [
            ScheduleItem(index=i, local_time=rule.start + timedelta(hours=rule.interval_hours * i))
            for i in range(count)
        ]

    if rule.mode != "daily":
        raise ValueError(f"unsupported schedule mode: {rule.mode}")
    daily_count = int(rule.daily_count or 0)
    days = int(rule.days or 0)
    if daily_count < 1 or days < 1:
        raise ValueError("daily schedule requires positive daily_count and days")
    if count > daily_count * days:
        raise ValueError("requested count exceeds daily schedule capacity")

    clocks = sorted(rule.daily_times) if rule.daily_times is not None else None
    items: list[ScheduleItem] = []
    for i in range(count):
        day_index, within_day = divmod(i, daily_count)
        if clocks is not None:
            hour, minute = map(int, clocks[within_day].split(":"))
            when = (rule.start + timedelta(days=day_index)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            when = rule.start + timedelta(days=day_index, hours=within_day * rule.interval_hours)
        items.append(ScheduleItem(index=i, local_time=when))
    return items


def validate_platform_window(
    items: list[ScheduleItem] | tuple[ScheduleItem, ...],
    *,
    now: datetime,
    limits: PlatformLimits,
) -> ScheduleValidationResult:
    _require_aware(now, "now")
    minimum = now + timedelta(hours=limits.min_hours)
    maximum = now + timedelta(days=limits.max_days)
    checked: list[ValidatedScheduleItem] = []
    invalid: list[int] = []

    for item in items:
        _require_aware(item.local_time, "schedule item time")
        when = item.local_time
        if when < now:
            invalid.append(item.index)
            checked.append(
                ValidatedScheduleItem(
                    index=item.index,
                    local_time=when,
                    error_code="scheduled_time_in_past",
                )
            )
        elif when < minimum and limits.reject_short:
            invalid.append(item.index)
            checked.append(ValidatedScheduleItem(index=item.index, local_time=when, error_code="before_schedule_window"))
        elif when < minimum:
            checked.append(
                ValidatedScheduleItem(
                    index=item.index,
                    local_time=when,
                    required_choice=frozenset({"software_schedule", "publish_now", "change_time"}),
                    silent_fallback=False,
                )
            )
        elif when > maximum:
            invalid.append(item.index)
            checked.append(
                ValidatedScheduleItem(
                    index=item.index,
                    local_time=when,
                    error_code="beyond_platform_window",
                )
            )
        else:
            checked.append(ValidatedScheduleItem(index=item.index, local_time=when))

    return ScheduleValidationResult(tuple(checked), invalid)
