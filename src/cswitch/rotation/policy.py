"""Which account should be live. Stay while the active account is below both
thresholds; with the priority strategy, switch back to an account earlier in
`order` once it has room, unless the active account was chosen by hand. Otherwise
pick the best usable account by the configured strategy; if none is usable, report
that and when the first one frees up. Accounts never seen yet count as usable,
since nothing is known against them. Excluded accounts are never switched to, but
the live one is still left before its limit. An account's reserve lowers its
weekly threshold, so the reserve is never used up automatically (an account chosen
by hand may use it). A live login that isn't a saved account is never switched
away from (it may be a /login in progress)."""

from __future__ import annotations

from dataclasses import dataclass, replace

from cswitch.claude.usage import Usage
from cswitch.config import Config

STALE_SECONDS = 600
STALE_MARGIN = 10


@dataclass(frozen=True)
class Decision:
    switch_to: str | None
    reason: str
    exhausted: bool = False
    frees_at: float | None = None
    stale: bool = False


def ordered(names: list[str], config: Config) -> list[str]:
    """Saved account names in preference order: as listed in the config, then the rest."""
    preferred = [name for name in dict.fromkeys(config.order) if name in names]
    return preferred + sorted(name for name in names if name not in preferred)


def decide(active: str | None, names: list[str], usage: dict[str, Usage], config: Config, now: float,
           chosen_by_hand: bool = False) -> Decision:
    """Stay, switch (and to which account), or report that every account is used up.
    A stale reading of the live account (or of one to switch back to) counts
    `STALE_MARGIN` points higher, so a cSwitcher that can't see well switches early
    instead of running into the limit, and doesn't switch back too soon."""
    if active not in names:
        return Decision(None, "the live login isn't a saved account, so it is left alone")
    reading = usage.get(active)
    margin = _margin(reading, now)
    stale = margin > 0
    own_limits = replace(config, reserve=()) if chosen_by_hand else config
    if not _is_full(active, reading, own_limits, now, margin):
        if chosen_by_hand:
            return Decision(None, f"'{active}' was chosen by hand; kept until it nears a limit", stale=stale)
        earlier = _earlier_in_order(active, names, usage, config, now)
        if earlier is not None:
            return Decision(earlier, f"'{earlier}' is earlier in the order and has room", stale=stale)
        return Decision(None, f"'{active}' is below the thresholds", stale=stale)
    pool = [name for name in ordered(names, config) if name == active or name not in config.exclude]
    candidates = [name for name in pool if name != active and not _is_full(name, usage.get(name), config, now)]
    if candidates:
        if config.strategy == "most_remaining":
            best = max(candidates, key=lambda name: _headroom(name, usage.get(name), config, now))
        else:
            best = candidates[0]
        return Decision(best, f"'{active}' reached a threshold", stale=stale)
    frees = [t for t in (_frees_at(name, usage.get(name), config, now) for name in pool) if t is not None]
    return Decision(None, "every usable account is over a threshold", exhausted=True,
                    frees_at=min(frees) if frees else None, stale=stale)


def in_reserve(name: str, usage: Usage | None, config: Config, now: float) -> bool:
    """Whether `name` has a reserve and its weekly usage has reached it."""
    limit = config.weekly_threshold(name)
    return usage is not None and limit < config.seven_day_threshold and usage.seven_day.used_at(now) >= limit


def _earlier_in_order(active: str, names: list[str], usage: dict[str, Usage], config: Config,
                      now: float) -> str | None:
    """With the priority strategy: the first account in `order` before the active one that has room,
    judged like the live one (stale numbers count higher), so cSwitcher can't switch back and forth."""
    if config.strategy != "priority":
        return None
    for name in config.order:
        if name == active:
            return None
        reading = usage.get(name)
        if name in names and name not in config.exclude and not _is_full(name, reading, config, now,
                                                                          _margin(reading, now)):
            return name
    return None


def _margin(reading: Usage | None, now: float) -> float:
    return STALE_MARGIN if reading is not None and now - reading.fetched_at > STALE_SECONDS else 0


def _is_full(name: str, usage: Usage | None, config: Config, now: float, margin: float = 0) -> bool:
    return usage is not None and (usage.five_hour.used_at(now) + margin >= config.five_hour_threshold
                                  or usage.seven_day.used_at(now) + margin >= config.weekly_threshold(name))


def _headroom(name: str, usage: Usage | None, config: Config, now: float) -> float:
    if usage is None:
        return min(config.five_hour_threshold, config.weekly_threshold(name))
    return min(config.five_hour_threshold - usage.five_hour.used_at(now),
               config.weekly_threshold(name) - usage.seven_day.used_at(now))


def _frees_at(name: str, usage: Usage | None, config: Config, now: float) -> float | None:
    """When a full account drops below both thresholds: the latest reset among its full windows."""
    if usage is None:
        return None
    blocking = [w for w, limit in ((usage.five_hour, config.five_hour_threshold),
                                   (usage.seven_day, config.weekly_threshold(name))) if w.used_at(now) >= limit]
    if not blocking or any(w.resets_at is None for w in blocking):
        return None
    return max(w.resets_at for w in blocking)
