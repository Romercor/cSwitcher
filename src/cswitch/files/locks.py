"""Cross-process locks compatible with proper-lockfile (the npm library Claude Code
uses): a lock is a directory that exists while held, and counts as abandoned
once its mtime is older than the lock's stale threshold. The timeout covers taking
all the given locks together; running out raises `Busy`."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from cswitch.errors import CswitchError

DEFAULT_TIMEOUT_SECONDS = 15
POLL_SECONDS = 0.2

Lock = tuple[Path, float]


class Busy(CswitchError):
    pass


@contextmanager
def hold(locks: list[Lock], timeout: float | None = None) -> Generator[None, None, None]:
    deadline = time.monotonic() + (DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout)
    held: list[Path] = []
    try:
        for lock_dir, stale_seconds in locks:
            _acquire(lock_dir, stale_seconds, deadline)
            held.append(lock_dir)
        yield
    finally:
        for lock_dir in reversed(held):
            _release(lock_dir)


def _acquire(lock_dir: Path, stale_seconds: float, deadline: float) -> None:
    while True:
        try:
            lock_dir.mkdir()
            return
        except FileExistsError:
            pass
        except FileNotFoundError:
            raise CswitchError(f"Folder not found: {lock_dir.parent}") from None
        if _is_stale(lock_dir, stale_seconds):
            _break_stale(lock_dir)
            continue
        if time.monotonic() > deadline:
            raise Busy(f"Timed out waiting for lock: {lock_dir}")
        time.sleep(POLL_SECONDS)


def _is_stale(lock_dir: Path, stale_seconds: float) -> bool:
    try:
        return time.time() - lock_dir.stat().st_mtime > stale_seconds
    except FileNotFoundError:
        return False


def _break_stale(lock_dir: Path) -> None:
    """Rename before deleting, so two processes breaking the same stale lock don't
    delete each other's freshly taken one (same small window as proper-lockfile)."""
    grave = lock_dir.with_name(f"{lock_dir.name}.stale-{os.getpid()}-{time.time_ns()}")
    try:
        lock_dir.rename(grave)
    except FileNotFoundError:
        return
    shutil.rmtree(grave, ignore_errors=True)


def _release(lock_dir: Path) -> None:
    try:
        lock_dir.rmdir()
    except FileNotFoundError:
        pass
