"""User settings from `<cswitch home>/config.toml`. Every setting has a default, so
the file is optional; unknown keys and wrong types are reported instead of ignored.
`set_value` changes one setting from the command line, rewriting only its line so
the file's comments stay, and writes nothing unless the result reads back exactly.
Writes are atomic, because the hooks read this file on every check."""

from __future__ import annotations

import json
import math
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from cswitch.errors import CswitchError
from cswitch.files.jsonfile import write_text_atomic
from cswitch.paths import cswitch_home

CONFIG_FILE = "config.toml"
STRATEGIES = ("priority", "most_remaining")
PRIME_MODES = ("off", "together", "spread")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ACCOUNT_LIST_SETTINGS = ("order", "exclude", "reserve", "remote_control_accounts")

DEFAULT_TOML = """\
# cSwitcher settings. Delete a line to use its default.

# Accounts in order of preference (names from `cswitch list`). Unlisted accounts come last.
order = []

# Accounts cSwitcher never switches to on its own (`cswitch switch` still works).
exclude = []

# Switch away from an account once a window is at least this many percent used.
five_hour_threshold = 90
seven_day_threshold = 95

# Weekly usage to keep for yourself, per account: ["personal:30"] never takes personal
# above 70% of its weekly limit automatically.
reserve = []

# Which account to switch to: "priority" (first usable in `order`, and back to an
# earlier one once it has room) or "most_remaining".
strategy = "priority"

# Minimum seconds between usage checks while Claude works on a long task.
check_interval_seconds = 60

# Ask Anthropic (read-only, with the live login only) when local data is unclear:
# whose tokens are live, and fresh usage numbers when Claude Code's own are stale.
ask_anthropic = true

# Desktop notifications: every account used up, `cswitch run` finished or stopped.
notifications = true

# Accounts on which `cswitch run --remote-control` turns Remote Control on (empty: all).
remote_control_accounts = []

# Keep a usage history (usage.csv next to the log) for analysing the switching later.
usage_history = false

# Start the 5-hour windows on a schedule (after `cswitch schedule install`): "off",
# "together" (every account at prime_from, again after each reset) or "spread" (one
# account at a time, so resets come evenly spaced and you rarely wait long). Primes
# happen between prime_from and prime_until, on prime_days.
prime_mode = "off"
prime_from = "08:00"
prime_until = "20:00"
prime_days = ["mon", "tue", "wed", "thu", "fri"]
"""


@dataclass(frozen=True)
class Config:
    order: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    five_hour_threshold: float = 90.0
    seven_day_threshold: float = 95.0
    reserve: tuple[str, ...] = ()
    strategy: str = "priority"
    check_interval_seconds: float = 60.0
    ask_anthropic: bool = True
    notifications: bool = True
    remote_control_accounts: tuple[str, ...] = ()
    usage_history: bool = False
    prime_mode: str = "off"
    prime_from: str = "08:00"
    prime_until: str = "20:00"
    prime_days: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")

    def __post_init__(self) -> None:
        for name in ("five_hour_threshold", "seven_day_threshold"):
            if not 1 <= getattr(self, name) <= 100:
                raise CswitchError(f"config: {name} must be between 1 and 100.")
        if self.strategy not in STRATEGIES:
            raise CswitchError(f"config: strategy must be one of {', '.join(STRATEGIES)}.")
        for entry in self.reserve:
            if _reserved_percent(entry) is None:
                raise CswitchError(f"config: reserve entries look like 'personal:30' (no spaces), not '{entry}'.")
        reserved = [account_of(entry) for entry in self.reserve]
        if len(reserved) != len(set(reserved)):
            raise CswitchError("config: give each account at most one reserve entry.")
        if not 0 <= self.check_interval_seconds < math.inf:
            raise CswitchError("config: check_interval_seconds must be a number of seconds, 0 or more.")
        if self.prime_mode not in PRIME_MODES:
            raise CswitchError(f"config: prime_mode must be one of {', '.join(PRIME_MODES)}.")
        if clock_minutes(self.prime_from) >= clock_minutes(self.prime_until):
            raise CswitchError("config: prime_from must be earlier than prime_until.")
        unknown_days = [day for day in self.prime_days if day not in WEEKDAYS]
        if unknown_days or not self.prime_days:
            raise CswitchError(f"config: prime_days are some of {', '.join(WEEKDAYS)}.")

    def weekly_threshold(self, account: str) -> float:
        """The weekly usage at which `account` counts as full: the threshold, lowered by its reserve."""
        for entry in self.reserve:
            percent = _reserved_percent(entry)
            if account_of(entry) == account and percent is not None:
                return min(self.seven_day_threshold, 100 - percent)
        return self.seven_day_threshold


def account_of(entry: str) -> str:
    """The account an entry of an account-list setting is about ('personal:20' -> 'personal')."""
    return entry.partition(":")[0]


def clock_minutes(text: str) -> int:
    """Minutes after midnight of a 24-hour time like '04:40'."""
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text)
    if match is None:
        raise CswitchError(f"config: times look like '04:40' (24-hour), not '{text}'.")
    return int(match[1]) * 60 + int(match[2])


def clock_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def config_path() -> Path:
    return cswitch_home() / CONFIG_FILE


def load(path: Path) -> Config:
    try:
        text = read_text(path)
    except FileNotFoundError:
        return Config()
    return _build(_toml(text, path))


def read_text(path: Path) -> str:
    """The file's text (UTF-8, with or without a byte order mark)."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise CswitchError(f"{path} isn't UTF-8 text (PowerShell's > writes UTF-16); save it as UTF-8.") from None


def set_value(path: Path, name: str, words: list[str]) -> Config:
    """Change one setting from command-line words; returns the new settings. The old
    value may be invalid (that is how it gets repaired); a trailing comment on its line is dropped."""
    if name not in _names():
        raise CswitchError(f"Unknown setting '{name}'. Settings: {', '.join(_names())}")
    write_default(path)
    text = read_text(path)
    others = {key: value for key, value in _toml(text, path).items() if key != name}
    new = _build(others, **{name: _from_words(name, words)})
    line = f"{name} = {format_value(getattr(new, name))}"
    text, found = re.subn(rf"^{name}[ \t]*=.*$", lambda _: line, text, count=1, flags=re.MULTILINE)
    if not found:
        text = f"{text.rstrip()}\n{line}\n" if text.strip() else f"{line}\n"
    try:
        unchanged_elsewhere = _build(_toml(text, path)) == new
    except CswitchError:
        unchanged_elsewhere = False
    if not unchanged_elsewhere:
        raise CswitchError(f"Couldn't change {name} safely in {path} (is it written over several lines?). "
                           "Please edit it by hand.")
    write_text_atomic(path, text)
    return new


def describe(settings: Config) -> list[str]:
    return [f"{field.name} = {format_value(getattr(settings, field.name))}" for field in fields(settings)]


def format_value(value: object) -> str:
    """A setting's value written as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        return "[" + ", ".join(json.dumps(v) for v in value) + "]"
    if isinstance(value, str):
        return json.dumps(value)
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def write_default(path: Path) -> bool:
    """Create a commented config file if there is none. Returns True if it was created."""
    if path.exists():
        return False
    write_text_atomic(path, DEFAULT_TOML)
    return True


def _toml(text: str, path: Path) -> dict:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CswitchError(f"{path}: {e}") from None
    unknown = sorted(set(data) - set(_names()))
    if unknown:
        raise CswitchError(f"{path}: unknown setting(s): {', '.join(unknown)}")
    return data


def _build(data: dict, **typed: object) -> Config:
    return Config(**{name: _checked(name, value) for name, value in data.items()}, **typed)


def _names() -> list[str]:
    return [f.name for f in fields(Config)]


def _default(name: str) -> object:
    return next(f.default for f in fields(Config) if f.name == name)


def _from_words(name: str, words: list[str]) -> object:
    """Command-line words as the setting's type; lists may be given as words or comma-separated."""
    default = _default(name)
    if isinstance(default, tuple):
        return _name_list(part for word in words for part in word.split(","))
    if len(words) != 1:
        raise CswitchError(f"{name} takes exactly one value.")
    word = words[0]
    if isinstance(default, bool):
        if word.lower() in ("true", "on", "yes"):
            return True
        if word.lower() in ("false", "off", "no"):
            return False
        raise CswitchError(f"{name} must be true or false.")
    if isinstance(default, float):
        try:
            return float(word)
        except ValueError:
            raise CswitchError(f"{name} must be a number.") from None
    return word.lower()


def _checked(name: str, value: object) -> object:
    """The value converted to the setting's type (judged by its default); wrong types are errors."""
    default = _default(name)
    if isinstance(default, tuple) and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return _name_list(value)
    if isinstance(default, bool) and isinstance(value, bool):
        return value
    if isinstance(default, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(default, str) and isinstance(value, str):
        return value
    raise CswitchError(f"config: {name} has the wrong type ({type(value).__name__}).")


def _reserved_percent(entry: str) -> float | None:
    """The percent in a reserve entry like 'personal:30'; None if malformed."""
    name, _, percent = entry.partition(":")
    try:
        value = float(percent)
    except ValueError:
        return None
    return value if name and name == name.strip() and 0 < value < 100 else None


def _name_list(names) -> tuple[str, ...]:
    """Lowercased, trimmed, without blanks or repeats."""
    return tuple(dict.fromkeys(name.strip().lower() for name in names if name.strip()))
