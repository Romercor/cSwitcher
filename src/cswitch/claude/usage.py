"""Usage of a Claude subscription: how full the 5-hour and weekly windows are and
when they reset. Claude Code's saved usage data and Anthropic's usage endpoint
use the same shape: {"five_hour": {"utilization", "resets_at"}, "seven_day": {...}}.
A window that is null means unused; a window that can't be read makes the whole
reading unknown rather than 0%, so a format change never looks like free capacity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

WINDOWS = ("five_hour", "seven_day")


@dataclass(frozen=True)
class Window:
    used: float
    resets_at: float | None

    def __post_init__(self) -> None:
        if not _is_number(self.used) or not (self.resets_at is None or _is_number(self.resets_at)):
            raise ValueError("invalid usage window")

    def used_at(self, now: float) -> float:
        """Percent used at time `now`; a window whose reset time has passed is empty."""
        return 0.0 if self.resets_at is not None and self.resets_at <= now else self.used


@dataclass(frozen=True)
class Usage:
    five_hour: Window
    seven_day: Window
    fetched_at: float

    def __post_init__(self) -> None:
        if not _is_number(self.fetched_at):
            raise ValueError("invalid usage timestamp")

    def to_dict(self) -> dict:
        return {"five_hour": vars(self.five_hour), "seven_day": vars(self.seven_day), "fetched_at": self.fetched_at}

    @classmethod
    def from_dict(cls, data: dict) -> Usage:
        """Raises KeyError, TypeError or ValueError for anything malformed."""
        return cls(Window(**data["five_hour"]), Window(**data["seven_day"]), data["fetched_at"])


def parse(data: object, fetched_at: float) -> Usage | None:
    """Usage from Anthropic's shape, or None if it is missing or can't be read."""
    if not isinstance(data, dict) or all(data.get(key) is None for key in WINDOWS):
        return None
    windows = [_window(data.get(key)) for key in WINDOWS]
    if None in windows:
        return None
    return Usage(windows[0], windows[1], fetched_at)


def from_claude_cache(cache: object) -> tuple[str | None, Usage] | None:
    """(account uuid, usage) from Claude Code's `cachedUsageUtilization`, if usable."""
    if not isinstance(cache, dict) or not _is_number(cache.get("fetchedAtMs")):
        return None
    usage = parse(cache.get("utilization"), cache["fetchedAtMs"] / 1000)
    return (cache.get("accountUuid"), usage) if usage else None


def _window(data: object) -> Window | None:
    if data is None:
        return Window(0.0, None)
    if not isinstance(data, dict) or not _is_number(data.get("utilization")):
        return None
    return Window(float(data["utilization"]), _timestamp(data.get("resets_at")))


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
