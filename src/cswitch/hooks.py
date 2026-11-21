"""Claude Code hook entry point. After tool calls and replies (throttled by
check_interval_seconds) it checks the live account's usage and switches before a
limit is reached; when a reply fails with a usage limit it switches at once.
It never raises and prints nothing, so Claude Code is never disturbed: what it
did goes to the log, and only what needs the user becomes a notification.
Claude Code's locks are waited for at most LOCK_TIMEOUT_SECONDS per set, which
keeps a run well inside the hook timeout. A usage-limit hit waits longer for
cSwitcher's own lock (LIMIT_LOCK_TIMEOUT_SECONDS), because Claude has stopped and
nothing would retry it."""

from __future__ import annotations

import json
import traceback
from dataclasses import replace
from pathlib import Path

from cswitch import log, report
from cswitch.accounts.store import AccountStore
from cswitch.claude.home import ClaudeHome
from cswitch.files import locks
from cswitch.rotation import engine

EVENTS = ("PostToolUse", "Stop", "StopFailure")
LIMIT_ERROR = "rate_limit"
LOCK_TIMEOUT_SECONDS = 3
LIMIT_LOCK_TIMEOUT_SECONDS = 8


def commands() -> dict[str, tuple[str, ...]]:
    """The cswitch arguments Claude Code runs for each event: `cswitch hook <event>`."""
    return {event: ("hook", event) for event in EVENTS}


def handle(event: str, stdin_text: str, home: ClaudeHome, store: AccountStore, root: Path, now: float) -> None:
    try:
        if event not in EVENTS or (event == "StopFailure" and _error_of(stdin_text) != LIMIT_ERROR):
            return
        limit_hit = event == "StopFailure"
        home = replace(home, lock_timeout=LOCK_TIMEOUT_SECONDS)
        timeout = LIMIT_LOCK_TIMEOUT_SECONDS if limit_hit else LOCK_TIMEOUT_SECONDS
        result = engine.tick(home, store, root, now, limit_hit=limit_hit, timeout=timeout)
        if result is not None:
            report.log_tick(root, event, result, limit_hit)
    except locks.Busy as e:
        _log_once(root, f"{event}: skipped, another cSwitcher or Claude Code step was busy ({e})")
    except Exception:
        _log_once(root, f"{event}: error " + traceback.format_exc().strip().replace("\n", " | "))


def _log_once(root: Path, message: str) -> None:
    """Log unless it repeats the last line, so a lasting problem can't flood the log."""
    last = log.tail(root, 1)
    if not (last and last[0].endswith(message)):
        log.write(root, message)


def _error_of(stdin_text: str) -> object:
    payload = json.loads(stdin_text) if stdin_text.strip() else {}
    return payload.get("error") if isinstance(payload, dict) else None

