"""The operations the command line, the hooks and the runner share: `check` (refresh
the live account's usage and decide, for `cswitch status`), `tick` (one automatic
step: throttled check, switch if needed, notice when every account has just been
used up), `switch` (by hand: switch, remember when, so older usage numbers are never
credited to the new account, and remember the choice so automatic switching leaves
it alone until `forget_hand_choice`) and `remove` (delete a saved account). All run
under cSwitcher's own state lock (`locked_state`, also used by priming), so hooks
from several Claude sessions don't overwrite each other's memory or switch twice,
and a running hook can't save a removed account back. The lock is always taken
before Claude Code's. `reserve_reached` is a quick lock-free look at the state.
An account being primed (marked in the state, at most PRIME_MARK_SECONDS) is never
switched to or away from, since a copy of its login is in use. Usage numbers that
changed under the lock go to the optional usage history."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from cswitch import config, history
from cswitch.accounts import switcher
from cswitch.accounts.store import AccountStore
from cswitch.claude import server
from cswitch.claude.home import ClaudeHome
from cswitch.errors import CswitchError
from cswitch.files import locks
from cswitch.rotation import monitor, policy, state

LOCK_STALE_SECONDS = 60
LOCK_TIMEOUT_SECONDS = 20
RECENT_SWITCH_SECONDS = 60
PRIME_MARK_SECONDS = 600


@dataclass(frozen=True)
class Check:
    settings: config.Config
    memory: state.State
    active: str | None
    decision: policy.Decision


@dataclass(frozen=True)
class Tick:
    check: Check
    switched: bool
    newly_exhausted: bool
    limit_marked: bool = False
    recently_switched: bool = False


def check(home: ClaudeHome, store: AccountStore, root: Path, now: float,
          timeout: float = LOCK_TIMEOUT_SECONDS) -> Check:
    settings = config.load(root / config.CONFIG_FILE)
    with locked_state(root, timeout) as memory:
        active = monitor.observe(home, store, memory, settings, now)
        return Check(settings, memory, active, _decide(active, store, memory, settings, now))


def tick(home: ClaudeHome, store: AccountStore, root: Path, now: float, *, limit_hit: bool = False,
         force: bool = False, timeout: float = LOCK_TIMEOUT_SECONDS) -> Tick | None:
    """None if the last tick was under check_interval_seconds ago, unless `force`d or
    Claude Code just reported a usage limit (`limit_hit`), which is recorded and acted on at once.
    The interval is checked without the lock first and the check time is saved before
    the slow part, so parallel hooks return at once instead of queueing for the lock.
    A limit reported right after a switch is not charged to the new account: it most
    likely belongs to the one switched away from. After a limit, when every account is
    over its threshold, one with any room left at all is used before giving up."""
    settings = config.load(root / config.CONFIG_FILE)
    urgent = limit_hit or force
    if not urgent and _too_soon(_peek(root), settings, now):
        return None
    with locked_state(root, timeout) as memory:
        if not urgent and _too_soon(memory, settings, now):
            return None
        memory.last_check_at = now
        state.save(root / state.STATE_FILE, memory)
        active = monitor.observe(home, store, memory, settings, now, fetch=limit_hit)
        recently_switched = now - memory.last_switch_at <= RECENT_SWITCH_SECONDS
        limit_marked = bool(limit_hit and active and not recently_switched)
        if limit_marked:
            monitor.mark_limit_hit(memory, active, now)
        decision = _decide(active, store, memory, settings, now)
        if limit_hit and decision.exhausted:
            to_the_end = replace(settings, five_hour_threshold=100.0, seven_day_threshold=100.0)
            decision = _decide(active, store, memory, to_the_end, now)
        switched = decision.switch_to is not None and _switch(home, store, memory, decision.switch_to,
                                                              owner_check(settings), now)
        newly_exhausted = decision.exhausted and not memory.exhausted
        memory.exhausted = decision.exhausted
    return Tick(Check(settings, memory, active, decision), switched, newly_exhausted, limit_marked, recently_switched)


def switch(home: ClaudeHome, store: AccountStore, root: Path, name: str, now: float,
           timeout: float = LOCK_TIMEOUT_SECONDS) -> bool:
    settings = config.load(root / config.CONFIG_FILE)
    with locked_state(root, timeout) as memory:
        busy = being_primed(memory, now) & {name, switcher.active_name(home, store)}
        if busy:
            raise CswitchError(f"'{busy.pop()}' is being primed right now; try again in a minute.")
        return _switch(home, store, memory, name, owner_check(settings), now, by_hand=True)


def remove(store: AccountStore, root: Path, name: str, now: float, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
    with locked_state(root, timeout) as memory:
        if name in being_primed(memory, now):
            raise CswitchError(f"'{name}' is being primed right now; try again in a minute.")
        store.remove(name)
        for remembered in (memory.usage, memory.primed_at, memory.priming):
            remembered.pop(name, None)
        if memory.chosen_by_hand == name:
            memory.chosen_by_hand = None


def forget_hand_choice(root: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
    """Let automatic switching (order, reserve) apply again to an account chosen by hand."""
    with locked_state(root, timeout) as memory:
        memory.chosen_by_hand = None


def reserve_reached(root: Path, now: float) -> bool:
    """Whether, as the hooks last saw it, every account is used up and the live one has
    reached its reserve, so work should pause instead of spending the reserve."""
    memory = _peek(root)
    try:
        settings = config.load(root / config.CONFIG_FILE)
    except (CswitchError, OSError):
        return False
    if memory is None or memory.last_active is None or not memory.exhausted:
        return False
    live = memory.last_active
    return live != memory.chosen_by_hand and policy.in_reserve(live, memory.usage.get(live), settings, now)


def being_primed(memory: state.State, now: float) -> set[str]:
    return {name for name, since in memory.priming.items() if 0 <= now - since < PRIME_MARK_SECONDS}


def _switch(home: ClaudeHome, store: AccountStore, memory: state.State, name: str,
            identify_owner: switcher.Identify | None, now: float, by_hand: bool = False) -> bool:
    switched = switcher.switch_to(home, store, name, identify_owner)
    if switched:
        memory.last_active = name
        memory.last_switch_at = now
    if by_hand:
        memory.chosen_by_hand = name
    elif switched:
        memory.chosen_by_hand = None
    return switched


def _decide(active: str | None, store: AccountStore, memory: state.State, settings: config.Config,
            now: float) -> policy.Decision:
    busy = being_primed(memory, now)
    if active in busy:
        return policy.Decision(None, f"'{active}' is being primed")
    names = [name for name in store.names() if name not in busy]
    decision = policy.decide(active, names, memory.usage, settings, now, active == memory.chosen_by_hand)
    if decision.exhausted and busy:
        return policy.Decision(None, f"waiting for {', '.join(sorted(busy))} to be primed")
    return decision


def _too_soon(memory: state.State | None, settings: config.Config, now: float) -> bool:
    return memory is not None and now - memory.last_check_at < settings.check_interval_seconds


def _peek(root: Path) -> state.State | None:
    """The state without taking the lock; None if it can't be read right now (e.g. mid-write)."""
    try:
        return state.load(root / state.STATE_FILE)
    except (OSError, CswitchError):
        return None


def owner_check(settings: config.Config) -> switcher.Identify | None:
    """How uncertain token ownership is settled: by asking Anthropic, unless the config forbids it."""
    return server.organization_of if settings.ask_anthropic else None


@contextmanager
def locked_state(root: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Generator[state.State, None, None]:
    """Load the state under the lock, let the caller change it, then save it, also when
    the caller fails, so the check time and anything learned before the failure count."""
    root.mkdir(parents=True, exist_ok=True)
    with locks.hold([(root / "state.lock", LOCK_STALE_SECONDS)], timeout):
        memory = state.load(root / state.STATE_FILE)
        before, live_before = dict(memory.usage), memory.last_active
        try:
            yield memory
        finally:
            state.save(root / state.STATE_FILE, memory)
            history.note(root, before, memory.usage, {live_before, memory.last_active})
