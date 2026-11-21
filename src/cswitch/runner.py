"""`cswitch run`: carries one task to the end with nobody present. A headless Claude
session works on it while the installed hooks switch accounts before limits. If a
turn still fails on a usage limit, cSwitcher switches at once (or accepts the
switch a hook just made) or, when every account is used up, waits until the first
frees up, then sends "continue". If every account is used up and the live one
reaches its reserve (as the hooks see it), Claude is stopped and the run waits the
same way instead of spending the reserve. Other failures, a crashed Claude and a Claude
silent for SILENCE_SECONDS are retried after growing pauses. The run gives up after
GIVE_UP_SECONDS without progress or of failed account checks, or after MAX_RESTARTS
restarts in a row. Progress means a
tool call or a finished turn, never an error message. The run ends when a turn has
finished, no background task is left and Claude starts nothing new within
GRACE_SECONDS; the user is notified then, and whenever the run stops early."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from cswitch import config, log, notify, report
from cswitch.accounts.store import AccountStore
from cswitch.claude.headless import IDLE_EVENT
from cswitch.claude.home import ClaudeHome
from cswitch.errors import CswitchError
from cswitch.rotation import engine

CONTINUE = "continue"
UNATTENDED = ("You are running unattended through cSwitcher: nobody will read or answer questions. "
              "Never ask; make sensible decisions yourself and keep working until the task is completely done.")
FIRST_PAUSE_SECONDS = 30
MAX_PAUSE_SECONDS = 30 * 60
GIVE_UP_SECONDS = 6 * 3600
MAX_RESTARTS = 5
SILENCE_SECONDS = 30 * 60
GRACE_SECONDS = 30
MAX_WAIT_SECONDS = 30 * 60
RESET_MARGIN_SECONDS = 180
LIMIT_TEXT = re.compile(r"usage limit|hit your (?:\w+ )?limit|(?:5-hour|weekly|session) limit", re.IGNORECASE)
MAIN_WINDOWS = ("five_hour", "seven_day")
FINISHED_TASKS = ("completed", "failed", "killed", "stopped", "cancelled")


class Outcome(StrEnum):
    DONE = "done"
    LIMIT = "usage limit"
    ERROR = "error"
    EXITED = "Claude Code exited"
    SILENT = "no activity"
    RESERVE = "reserve reached"


RESTART_OUTCOMES = (Outcome.EXITED, Outcome.SILENT)


class Session(Protocol):
    def send_user(self, text: str) -> None: ...
    def next_event(self, silence_seconds: float) -> dict | None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Turn:
    """How a turn ended. `started`: Claude wrote anything, so the task reached the
    session and a restart resumes it; `progressed`: it called a tool or finished."""
    outcome: Outcome
    started: bool
    progressed: bool
    detail: str = ""


def run(task: str | None, start: Callable[[bool], Session], home: ClaudeHome, store: AccountStore,
        root: Path, show: Callable[[str], None]) -> int:
    """Work on `task`, or continue the session if None; `start(resume)` starts Claude.
    Returns the exit code. A usage limit straight after another one, with no progress
    in between, gets a pause before continuing, like any other failure."""
    in_session = task is None
    session = start(in_session)
    first_turn = True
    stalled = restarts = 0
    last_progress = time.time()
    try:
        while True:
            session.send_user(CONTINUE if in_session or task is None else task)
            turn = _read_turn(session, show, lambda: engine.reserve_reached(root, time.time()))
            in_session = in_session or turn.started
            if turn.progressed:
                stalled, last_progress = 0, time.time()
            else:
                stalled += 1
            restarts = restarts + 1 if turn.outcome in RESTART_OUTCOMES else 0
            if turn.outcome is Outcome.DONE:
                _finish(root, show, "finished", turn.detail)
                return 0
            if first_turn and turn.outcome is Outcome.EXITED and not turn.started:
                _finish(root, show, "could not start Claude Code (see the message above)", turn.detail)
                return 1
            first_turn = False
            if turn.outcome is Outcome.RESERVE:
                session.close()
                say(root, show, f"{turn.outcome}: {turn.detail}")
                _wait_for_room(home, store, root, show, _tick(home, store, root, force=True))
                session, last_progress = start(in_session), time.time()
                continue
            if turn.outcome is Outcome.LIMIT and _make_room(home, store, root, show):
                last_progress = time.time()
                if stalled <= 1:
                    continue
            if time.time() - last_progress > GIVE_UP_SECONDS or restarts > MAX_RESTARTS:
                why = f"{restarts} restarts in a row" if restarts > MAX_RESTARTS else "hours without progress"
                _finish(root, show, f"gave up after {why}", turn.detail)
                return 1
            _pause(root, show, turn, stalled)
            if turn.outcome in RESTART_OUTCOMES:
                session.close()
                session = start(in_session)
    except Exception as e:
        _finish(root, show, "stopped by an error", str(e))
        raise
    finally:
        session.close()


def _read_turn(session: Session, show: Callable[[str], None], reserve_reached: Callable[[], bool]) -> Turn:
    """Show Claude's work until the turn ends: a failed result, a successful one once no
    background task is left and Claude hasn't started another turn within GRACE_SECONDS,
    or (checked at each tool call) the live account reaching its reserve."""
    started = progressed = succeeded = settling = background = limit_signs = False
    detail = ""

    def end(outcome: Outcome, text: str) -> Turn:
        return Turn(outcome, started, progressed, text)

    while (event := session.next_event(GRACE_SECONDS if settling else SILENCE_SECONDS)) is not None:
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == IDLE_EVENT:
            if succeeded:
                return end(Outcome.DONE, detail if settling else f"{detail} (background work still running)")
            return end(Outcome.SILENT, f"silent for {SILENCE_SECONDS // 60} min")
        started = True
        if kind == "assistant":
            succeeded = settling = False
            limit_signs |= event.get("error") == "rate_limit"
            if not event.get("error") and _show_assistant(event, show):
                progressed = True
                if reserve_reached():
                    return end(Outcome.RESERVE, "the live account reached its reserve")
        elif subtype == "permission_denied":
            show(f"  ! refused {event.get('tool_name') or 'an action'}: {event.get('message') or ''}".rstrip(": "))
        elif subtype == "background_tasks_changed":
            background = _any_running(event.get("tasks"))
            settling = succeeded and not background
        elif kind == "rate_limit_event":
            limit_signs |= _window_used_up(event.get("rate_limit_info"))
        elif kind == "result":
            detail = _result_detail(event)
            if event.get("is_error") or subtype != "success":
                limit = limit_signs or bool(LIMIT_TEXT.search(detail))
                return end(Outcome.LIMIT if limit else Outcome.ERROR, detail)
            succeeded = progressed = True
            limit_signs = False
            if not background:
                return end(Outcome.DONE, detail)
    return end(Outcome.EXITED, "the process ended")


def _make_room(home: ClaudeHome, store: AccountStore, root: Path, show: Callable[[str], None]) -> bool:
    """After a usage limit: switch (or accept a hook's switch of the last minute), or wait
    until an account frees up. False if neither is possible right now."""
    result = _tick(home, store, root, limit_hit=True)
    if result is None or result.switched or result.recently_switched:
        return result is not None
    if not result.check.decision.exhausted:
        return False
    _wait_for_room(home, store, root, show, result)
    return True


def _wait_for_room(home: ClaudeHome, store: AccountStore, root: Path, show: Callable[[str], None],
                   result: engine.Tick | None) -> None:
    """Given the last check, wait until an account has room (switching to it): until the
    first reset, re-checking at least every MAX_WAIT_SECONDS. A failed check is retried
    after RESET_MARGIN_SECONDS, doubling up to MAX_WAIT_SECONDS; after GIVE_UP_SECONDS
    of nothing but failed checks the run stops."""
    failing_since: float | None = None
    failures = 0
    while result is None or (not result.switched and result.check.decision.exhausted):
        now = time.time()
        if result is None:
            failures += 1
            failing_since = now if failing_since is None else failing_since
            if now - failing_since > GIVE_UP_SECONDS:
                raise CswitchError("the accounts couldn't be checked for hours (see cswitch log)")
            until = now + min(RESET_MARGIN_SECONDS * 2 ** (failures - 1), MAX_WAIT_SECONDS)
            show(f"[cswitch] couldn't check the accounts; trying again at {datetime.fromtimestamp(until):%H:%M}")
        else:
            failing_since, failures = None, 0
            frees_at = result.check.decision.frees_at
            until = max(min(now + MAX_WAIT_SECONDS, (frees_at or math.inf) + RESET_MARGIN_SECONDS),
                        now + RESET_MARGIN_SECONDS)
            say(root, show, f"every account is used up; waiting until {datetime.fromtimestamp(until):%a %H:%M}")
        time.sleep(until - now)
        result = _tick(home, store, root, force=True)


def _tick(home: ClaudeHome, store: AccountStore, root: Path, limit_hit: bool = False,
          force: bool = False) -> engine.Tick | None:
    """One engine check; a failure is logged and the run carries on, as the hooks do."""
    try:
        result = engine.tick(home, store, root, time.time(), limit_hit=limit_hit, force=force)
    except Exception as e:
        log.write(root, f"run: check failed: {type(e).__name__}: {e}")
        return None
    if result is not None:
        report.log_tick(root, "run", result, limit_hit)
    return result


def _pause(root: Path, show: Callable[[str], None], turn: Turn, stalled: int) -> None:
    seconds = min(FIRST_PAUSE_SECONDS * 2 ** max(stalled - 1, 0), MAX_PAUSE_SECONDS)
    say(root, show, f"{turn.outcome}: {turn.detail[:200]} - retrying in {seconds} s")
    time.sleep(seconds)


def _window_used_up(info: object) -> bool:
    """A rate-limit event saying the 5-hour or weekly window is used up (other windows don't count)."""
    if not isinstance(info, dict):
        return False
    if info.get("status") == "rejected" and info.get("rateLimitType") in MAIN_WINDOWS:
        return True
    windows = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    for name in MAIN_WINDOWS:
        window = windows.get(name)
        if isinstance(window, dict):
            used = window.get("utilization")
            if window.get("status") == "rejected" or (isinstance(used, (int, float)) and used >= 1):
                return True
    return False


def _any_running(tasks: object) -> bool:
    return isinstance(tasks, list) and any(
        isinstance(task, dict) and task.get("status") not in FINISHED_TASKS for task in tasks)


def _result_detail(event: dict) -> str:
    detail = str(event.get("result") or event.get("subtype") or "")
    denials = event.get("permission_denials")
    if isinstance(denials, list) and denials:
        tools = sorted({str(d.get("tool_name", "?")) for d in denials if isinstance(d, dict)})
        detail += (f" ({len(denials)} actions were refused: {', '.join(tools)}; "
                   "allow them with --permission-mode or -- --allowedTools)")
    return detail


def _show_assistant(event: dict, show: Callable[[str], None]) -> bool:
    """Print Claude's text and one line per tool call; True if it called a tool."""
    used_tool = False
    for block in (event.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            show(str(block["text"]).strip())
        elif block.get("type") == "tool_use":
            used_tool = True
            show(f"  > {block.get('name')} {_summary(block.get('input'))}".rstrip())
    return used_tool


def _summary(tool_input: object) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "pattern", "url", "description"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().splitlines()[0][:100]
    return ""


def say(root: Path, show: Callable[[str], None], text: str) -> None:
    """Tell the user and write it to the activity log."""
    show(f"[cswitch] {text}")
    log.write(root, f"run: {text[:300]}")


def _finish(root: Path, show: Callable[[str], None], what: str, detail: str) -> None:
    say(root, show, f"{what}: {detail}")
    try:
        wanted = config.load(root / config.CONFIG_FILE).notifications
    except CswitchError:
        wanted = True
    if wanted:
        notify.notify(f"cSwitcher: run {what}", detail[:250] or what)
