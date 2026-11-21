"""cSwitcher's activity log (`<cswitch home>/log.txt`): what the hooks did and why,
so an unattended night can be reviewed in the morning. Writing never fails."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cswitch.files.append import append_line

LOG_FILE = "log.txt"
MAX_LOG_BYTES = 1_000_000


def write(root: Path, message: str) -> None:
    append_line(root / LOG_FILE, f"{datetime.now().isoformat(timespec='seconds')}  {message}", MAX_LOG_BYTES)


def tail(root: Path, lines: int) -> list[str]:
    path = root / LOG_FILE
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
