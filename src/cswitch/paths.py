"""cSwitcher's own folder (`~/.cswitcher`, or `$CSWITCH_HOME`): saved accounts, logs, config."""

import os
from pathlib import Path


def cswitch_home() -> Path:
    override = os.environ.get("CSWITCH_HOME")
    return Path(override).expanduser() if override else Path.home() / ".cswitcher"
