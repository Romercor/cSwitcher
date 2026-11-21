"""Keeps the computer from going to sleep while `cswitch run` works, which an idle
laptop otherwise does overnight: SetThreadExecutionState on Windows, `caffeinate`
on macOS, nothing elsewhere. The display may still turn off."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake() -> Generator[None, None, None]:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        try:
            yield
        finally:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    elif sys.platform == "darwin" and shutil.which("caffeinate"):
        helper = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        try:
            yield
        finally:
            helper.terminate()
    else:
        yield
