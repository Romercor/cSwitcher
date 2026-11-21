"""Keeping the remembered usage of the live account current: taken from Claude
Code's own saved usage data when it is fresh and clearly about this account
(fetched after the last switch), otherwise asked from Anthropic with the live
login if the config allows it. A live account that differs from last time
(e.g. a manual /login) counts as a switch, so earlier numbers can't be credited
to it; the first account ever seen does not. Older numbers never replace newer
ones. When Claude Code reports a usage limit, fresh numbers are fetched to see
which window is full; if that is not possible, the 5-hour window is assumed full."""

from __future__ import annotations

from cswitch.accounts import switcher
from cswitch.accounts.store import AccountStore
from cswitch.claude import server
from cswitch.claude.home import ClaudeHome
from cswitch.claude.login import Login
from cswitch.claude.usage import Usage, Window, from_claude_cache
from cswitch.config import Config
from cswitch.rotation.state import State

USAGE_MAX_AGE_SECONDS = 120
FIVE_HOURS = 5 * 3600


def observe(home: ClaudeHome, store: AccountStore, state: State, config: Config, now: float,
            fetch: bool = False) -> str | None:
    """Record the live account's current usage in `state`; returns the live account's name.
    `fetch` asks Anthropic even when Claude Code's own numbers are fresh."""
    live = home.read_login()
    active = switcher.active_name(home, store) if live else None
    if live is None or active is None:
        return None
    if state.last_active is not None and active != state.last_active:
        state.last_switch_at = max(state.last_switch_at, now)
    state.last_active = active
    usage = _from_claude_cache(home, live, state)
    stale = usage is None or now - usage.fetched_at > USAGE_MAX_AGE_SECONDS
    if (fetch or stale) and config.ask_anthropic:
        usage = server.usage_of(live) or usage
    known = state.usage.get(active)
    if usage is not None and (known is None or usage.fetched_at >= known.fetched_at):
        state.usage[active] = usage
    return active


def mark_limit_hit(state: State, active: str, now: float) -> None:
    """Claude Code reported a usage limit. Unless the known numbers already show a full
    window, assume the 5-hour window is full until its known reset, or for 5 hours."""
    usage = state.usage.get(active)
    if usage and max(usage.five_hour.used_at(now), usage.seven_day.used_at(now)) >= 100:
        return
    known_reset = usage.five_hour.resets_at if usage else None
    resets_at = known_reset if known_reset and known_reset > now else now + FIVE_HOURS
    seven_day = usage.seven_day if usage else Window(0.0, None)
    state.usage[active] = Usage(Window(100.0, resets_at), seven_day, now)


def _from_claude_cache(home: ClaudeHome, live: Login, state: State) -> Usage | None:
    found = from_claude_cache(home.usage_cache())
    if found is None:
        return None
    account_uuid, usage = found
    if account_uuid != live.account_uuid or usage.fetched_at < state.last_switch_at:
        return None
    return usage
