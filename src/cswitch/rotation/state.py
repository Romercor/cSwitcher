"""What cSwitcher remembers between runs (`<cswitch home>/state.json`): the last
known usage of every account, so inactive accounts can be judged without asking
anyone, plus which account was live last time, when the last switch happened,
when usage was last checked, whether every account was already used up, and which
account the user chose with `cswitch switch` (cSwitcher doesn't switch back from it
and lets it use its reserve) and when the other accounts were last asked about, plus
for priming: which accounts are being primed and when each prime started, and when
each account was last primed.
A missing file starts fresh; an unreadable one is an error, so good history is
never overwritten with an empty state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cswitch.claude.usage import Usage
from cswitch.files.jsonfile import read_json, write_json

STATE_FILE = "state.json"


@dataclass
class State:
    usage: dict[str, Usage] = field(default_factory=dict)
    last_active: str | None = None
    last_switch_at: float = 0.0
    last_check_at: float = 0.0
    exhausted: bool = False
    chosen_by_hand: str | None = None
    priming: dict[str, float] = field(default_factory=dict)
    primed_at: dict[str, float] = field(default_factory=dict)
    others_checked_at: float = 0.0


def load(path: Path) -> State:
    """The saved state; malformed per-account entries are skipped, the rest is kept."""
    data = read_json(path)
    if data is None:
        return State()
    usage = {}
    for name, entry in (data.get("usage") or {}).items():
        try:
            usage[name] = Usage.from_dict(entry)
        except (KeyError, TypeError, ValueError):
            continue
    last_active, chosen = data.get("last_active"), data.get("chosen_by_hand")
    return State(usage, last_active if isinstance(last_active, str) else None,
                 _number(data.get("last_switch_at")), _number(data.get("last_check_at")),
                 data.get("exhausted") is True, chosen if isinstance(chosen, str) else None,
                 _times(data.get("priming")), _times(data.get("primed_at")), _number(data.get("others_checked_at")))


def save(path: Path, state: State) -> None:
    write_json(path, {
        "usage": {name: u.to_dict() for name, u in state.usage.items()},
        "last_active": state.last_active,
        "last_switch_at": state.last_switch_at,
        "last_check_at": state.last_check_at,
        "exhausted": state.exhausted,
        "chosen_by_hand": state.chosen_by_hand,
        "priming": state.priming,
        "primed_at": state.primed_at,
        "others_checked_at": state.others_checked_at,
    })


def _times(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {name: _number(when) for name, when in value.items() if isinstance(name, str)}


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
