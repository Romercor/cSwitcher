"""What an automatic check did, written to the activity log; only the moment every
account becomes used up is also a desktop notification. Shared by the hooks and
`cswitch run`."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cswitch import log, notify
from cswitch.rotation import engine


def log_tick(root: Path, source: str, result: engine.Tick, limit_hit: bool = False) -> None:
    decision = result.check.decision
    live = f"'{result.check.active}'" if result.check.active else "the live login"
    if result.limit_marked:
        log.write(root, f"{source}: {live} hit a usage limit")
    elif limit_hit:
        log.write(root, f"{source}: a usage limit was reported, not charged to {live} (just switched or unknown)")
    if result.switched:
        log.write(root, f"{source}: switched {live} -> '{decision.switch_to}': {decision.reason}")
    elif result.newly_exhausted:
        frees = datetime.fromtimestamp(decision.frees_at).strftime("%a %H:%M") if decision.frees_at else "unknown"
        log.write(root, f"{source}: every account is used up; first frees up {frees}")
        if result.check.settings.notifications:
            notify.notify("cSwitcher: all accounts used up", f"First account frees up: {frees}.")
