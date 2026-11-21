"""Appending lines to a private, size-capped text file from several processes at
once. Windows has no atomic append, so each write takes a short lock; if the lock
stays busy the line is dropped rather than delaying the caller."""

from __future__ import annotations

import os
from pathlib import Path

from cswitch.errors import CswitchError
from cswitch.files import locks

LOCK_STALE_SECONDS = 5
LOCK_TIMEOUT_SECONDS = 1


def append_line(path: Path, line: str, max_bytes: int, header: str | None = None) -> None:
    """Append one line; once the file exceeds `max_bytes` it becomes `<name>.old` first
    (unless that is open elsewhere, e.g. in Excel). A new file starts with `header`, if given."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locks.hold([(path.with_suffix(".lock"), LOCK_STALE_SECONDS)], timeout=LOCK_TIMEOUT_SECONDS):
            if path.exists() and path.stat().st_size > max_bytes:
                try:
                    path.replace(path.with_name(path.name + ".old"))
                except OSError:
                    pass
            text = line.rstrip("\n") + "\n"
            if header is not None and (not path.exists() or path.stat().st_size == 0):
                text = header + "\n" + text
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.write(fd, text.encode("utf-8"))
            finally:
                os.close(fd)
    except (OSError, CswitchError):
        pass
