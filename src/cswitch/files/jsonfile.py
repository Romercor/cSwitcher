"""Crash-safe file I/O for JSON and small text files.

Writes go to a private (0600) temp file in the same directory and are moved
into place with os.replace, so readers never see a half-written file. On
Windows the replace can briefly fail while another process has the target
open, hence the retry.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from cswitch.errors import CswitchError

REPLACE_ATTEMPTS = 20
REPLACE_DELAY_SECONDS = 0.05


def read_json(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CswitchError(f"{path} is not valid JSON ({e}).") from None
    if not isinstance(data, dict):
        raise CswitchError(f"{path} does not contain a JSON object.")
    return data


def write_json(path: Path, data: dict) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _replace(src: str, dst: Path) -> None:
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_DELAY_SECONDS)
