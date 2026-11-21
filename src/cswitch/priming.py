"""Window priming (`cswitch prime`): starts accounts' 5-hour windows with one tiny
request through the official Claude Code, so they run from a chosen time instead
of from the first real message. The live account is primed as it is; any other
account in a temporary, isolated Claude config holding a copy of its saved login,
so the live login and open chats are never touched; tokens Claude refreshes there
are saved back. While an account is primed it is marked in cSwitcher's state, so
no hook switches to it (or away from it, if it is live) meanwhile; the state lock
itself is only held for moments. An account whose window is running, as far as
cSwitcher knows, is skipped. When a prime started a window and what reset time it
produced are recorded and logged. Results come one by one.

`run_scheduled` is one run of the scheduled tasks (see schedule.py). A window
counts as known from the reset Anthropic reported, or from when cSwitcher primed
the account after that reading; it counts as over RESET_MARGIN_SECONDS after its
reset, since Anthropic resets a little late."""

from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from cswitch import config, log
from cswitch.accounts import switcher
from cswitch.accounts.store import AccountStore
from cswitch.claude import headless, server
from cswitch.claude.home import ClaudeHome
from cswitch.claude.login import Login
from cswitch.claude.usage import Usage
from cswitch.errors import CswitchError
from cswitch.files import locks
from cswitch.rotation import engine, monitor, policy, state

PROMPT = "Reply with just: OK"
MODEL = "haiku"
CLAUDE_TIMEOUT_SECONDS = 120
WORK_FOLDER = "prime"
WINDOW_SECONDS = 5 * 3600
RESET_MARGIN_MINUTES = 5
RESET_MARGIN_SECONDS = 180
SPREAD_TOLERANCE_SECONDS = 300
LOCK_TIMEOUT_SECONDS = 20


class Outcome(StrEnum):
    PRIMED = "primed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class PrimeResult:
    account: str
    outcome: Outcome
    detail: str


def prime(home: ClaudeHome, store: AccountStore, root: Path, now: float,
          accounts: list[str] | None = None) -> Iterator[PrimeResult]:
    """Prime `accounts`, by default every account `accounts_to_prime` gives."""
    settings = config.load(root / config.CONFIG_FILE)
    yield from _prime_all(home, store, root, settings, now,
                          accounts_to_prime(store, settings) if accounts is None else accounts)


def run_scheduled(home: ClaudeHome, store: AccountStore, root: Path, now: float) -> Iterator[PrimeResult]:
    """Nothing outside prime_from..prime_until on prime_days (a catch-up run may come late).
    Decided from what is known about the windows, so a late start, a missed run or a
    window started by hand is taken into account: "together" primes every account whose
    window is over, so all windows run side by side; "spread" primes only the first such
    account in order (another one if that fails), and only `spread_minutes` after the
    latest window start, less SPREAD_TOLERANCE_SECONDS (half a check, as checks come
    every 10 minutes), so the accounts take turns and their resets come evenly spaced."""
    settings = config.load(root / config.CONFIG_FILE)
    if not _in_schedule(settings, now):
        return
    memory = state.load(root / state.STATE_FILE)
    accounts = accounts_to_prime(store, settings)
    ends = {name: _window_end(memory, name, now) for name in accounts}
    idle = [name for name in accounts if _over(ends[name], now) and not _week_used_up(memory.usage.get(name), now)]
    spread = settings.prime_mode == "spread"
    if spread:
        since_latest_start = now - max((end - WINDOW_SECONDS for end in ends.values()), default=-math.inf)
        if since_latest_start < spread_minutes(len(accounts)) * 60 - SPREAD_TOLERANCE_SECONDS:
            return
    for result in _prime_all(home, store, root, settings, now, idle):
        yield result
        if spread and result.outcome is not Outcome.FAILED:
            return


def accounts_to_prime(store: AccountStore, settings: config.Config) -> list[str]:
    """Every saved account that isn't excluded, in order."""
    return [name for name in policy.ordered(store.names(), settings) if name not in settings.exclude]


def spread_minutes(account_count: int) -> int:
    """Minutes between primes when spread: a window plus a margin, shared out between the accounts."""
    return math.ceil((WINDOW_SECONDS // 60 + RESET_MARGIN_MINUTES) / max(account_count, 1))


def _in_schedule(settings: config.Config, now: float) -> bool:
    moment = datetime.fromtimestamp(now)
    minutes = moment.hour * 60 + moment.minute
    return (settings.prime_mode != "off" and config.WEEKDAYS[moment.weekday()] in settings.prime_days
            and config.clock_minutes(settings.prime_from) <= minutes <= config.clock_minutes(settings.prime_until))


def _window_end(memory: state.State, account: str, now: float) -> float:
    """When the account's 5-hour window ends, as far as cSwitcher knows (0: none known);
    an account being primed counts as primed when that began."""
    if account in engine.being_primed(memory, now):
        return memory.priming[account] + WINDOW_SECONDS
    known, primed = memory.usage.get(account), memory.primed_at.get(account)
    if known is not None and (primed is None or known.fetched_at >= primed):
        return known.five_hour.resets_at or 0.0
    return primed + WINDOW_SECONDS if primed is not None else 0.0


def _over(window_end: float, now: float) -> bool:
    return window_end + RESET_MARGIN_SECONDS <= now


def _week_used_up(usage: Usage | None, now: float) -> bool:
    return usage is not None and usage.seven_day.used_at(now) >= 100


def _prime_all(home: ClaudeHome, store: AccountStore, root: Path, settings: config.Config, now: float,
               accounts: list[str]) -> Iterator[PrimeResult]:
    (root / WORK_FOLDER).mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    for account in dict.fromkeys(accounts):
        try:
            result = _prime(home, store, root, settings, account, now + time.monotonic() - began)
        except locks.Busy:
            result = PrimeResult(account, Outcome.FAILED, "cSwitcher is busy; try again")
        log.write(root, f"prime: '{account}' {result.outcome}: {result.detail}")
        yield result


def _prime(home: ClaudeHome, store: AccountStore, root: Path, settings: config.Config,
           account: str, now: float) -> PrimeResult:
    """The mark is removed whatever happens; when and what was primed is recorded only on success."""
    with engine.locked_state(root, LOCK_TIMEOUT_SECONDS) as memory:
        end = _window_end(memory, account, now)
        if not _over(end, now):
            return PrimeResult(account, Outcome.SKIPPED, f"window running until {_time(end)}")
        live = switcher.active_name(home, store) == account
        memory.priming[account] = now
    login = usage = None
    try:
        login = _ask_live(home, root) if live else _ask_isolated(store, account, root)
        usage = server.usage_of(login) if settings.ask_anthropic else None
    except (CswitchError, OSError) as e:
        return PrimeResult(account, Outcome.FAILED, str(e))
    finally:
        _finish(root, account, now if login is not None else None, usage)
    resets = usage.five_hour.resets_at if usage is not None else None
    return PrimeResult(account, Outcome.PRIMED, f"resets {_time(resets)}" if resets else "window started")


def _finish(root: Path, account: str, started: float | None = None, usage: Usage | None = None) -> None:
    """Unmark the account; after a prime, remember when it started and the usage it produced."""
    with engine.locked_state(root, LOCK_TIMEOUT_SECONDS) as memory:
        memory.priming.pop(account, None)
        if started is not None:
            memory.primed_at[account] = started
        monitor.remember(memory, account, usage)


def _ask_live(home: ClaudeHome, root: Path) -> Login:
    headless.ask_once(PROMPT, MODEL, root / WORK_FOLDER, config_dir=None, timeout=CLAUDE_TIMEOUT_SECONDS)
    login = home.read_login()
    if login is None:
        raise CswitchError("Claude Code is not logged in.")
    return login


def _ask_isolated(store: AccountStore, account: str, root: Path) -> Login:
    """Ask as `account` in a temporary copy of its login; a temporary folder that can't be
    deleted (e.g. a virus scan holds a file) is logged, since it holds that copy."""
    saved = store.load(account)
    temp = tempfile.TemporaryDirectory(prefix="cswitch-prime-", ignore_cleanup_errors=True)
    try:
        with temp:
            copy = ClaudeHome.isolated(Path(temp.name))
            copy.write_login(saved)
            try:
                headless.ask_once(PROMPT, MODEL, root / WORK_FOLDER, config_dir=copy.config_dir,
                                  timeout=CLAUDE_TIMEOUT_SECONDS)
            finally:
                _save_refreshed(store, account, saved, copy)
            return copy.read_login() or saved
    finally:
        if Path(temp.name).exists():
            log.write(root, f"prime: couldn't delete {temp.name}, which holds a copy of '{account}'; delete it by hand")


def _save_refreshed(store: AccountStore, account: str, saved: Login, copy: ClaudeHome) -> None:
    """Save the login back if Claude refreshed it in the copy; if saving fails, keep it as
    a backup so the tokens aren't lost."""
    after = copy.read_login()
    if after is None or after == saved:
        return
    try:
        store.save(account, after)
    except (CswitchError, OSError):
        store.backup(after)
        raise


def _time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")
