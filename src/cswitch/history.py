"""Optional usage history (`usage_history = true`): one CSV line in
`<cswitch home>/usage.csv` whenever an account's usage numbers change, so how fast
windows fill up, how long every account was used up, and how well the thresholds,
reserve and priming mode fit can be analysed later, together with log.txt (switches,
waits, primes, setting changes). Columns: when the numbers were read, account,
whether it was live (1/0; at a switch, both accounts count as live), 5-hour %, 5-hour
reset, weekly %, weekly reset. After a usage limit, the account is written as 100%
used even though that is cSwitcher's assumption, not a reading. Size-capped like the
log: past MAX_BYTES the file becomes usage.csv.old, so at most about twice that is
kept. Writing never fails and never waits long."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cswitch import config
from cswitch.claude.usage import Usage
from cswitch.errors import CswitchError
from cswitch.files.append import append_line

HISTORY_FILE = "usage.csv"
MAX_BYTES = 2_000_000
HEADER = "read_at,account,live,five_hour_percent,five_hour_resets,weekly_percent,weekly_resets"


def note(root: Path, before: dict[str, Usage], after: dict[str, Usage], live: set[str | None]) -> None:
    """Write a line for every account whose numbers, as written, differ between `before` and `after`."""
    try:
        changed = [(name, numbers) for name, usage in after.items()
                   if (numbers := _numbers(usage)) != _numbers(before.get(name))]
        if not changed or not config.load(root / config.CONFIG_FILE).usage_history:
            return
        lines = [f"{_time(after[name].fetched_at)},{name},{int(name in live)},{numbers}" for name, numbers in changed]
    except (CswitchError, OSError, ValueError, OverflowError):
        return
    for line in lines:
        append_line(root / HISTORY_FILE, line, MAX_BYTES, header=HEADER)


def _numbers(usage: Usage | None) -> str | None:
    if usage is None:
        return None
    return (f"{usage.five_hour.used:g},{_time(usage.five_hour.resets_at)},"
            f"{usage.seven_day.used:g},{_time(usage.seven_day.resets_at)}")


def _time(timestamp: float | None) -> str:
    return "" if timestamp is None else datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
