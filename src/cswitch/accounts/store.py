"""Saved accounts: one JSON snapshot per account in `<cswitch home>/accounts`
(the previous version kept as `<name>.json.bak`), plus `backups/` for tokens
whose owner is uncertain: unknown live logins and snapshots replaced on weak evidence."""

from __future__ import annotations

import re
import time
from pathlib import Path

from cswitch.claude.login import Login
from cswitch.errors import CswitchError
from cswitch.files.jsonfile import read_json, write_json
from cswitch.paths import cswitch_home

NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
BACKUPS_KEPT = 50


class AccountStore:
    def __init__(self, root: Path):
        self.accounts_dir = root / "accounts"
        self.backups_dir = root / "backups"

    @classmethod
    def locate(cls) -> AccountStore:
        return cls(cswitch_home())

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.accounts_dir.glob("*.json") if NAME_PATTERN.match(p.stem))

    def all(self) -> dict[str, Login]:
        return {name: self._read(self._path(name)) for name in self.names()}

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def load(self, name: str) -> Login:
        path = self._path(name)
        if not path.exists():
            raise CswitchError(f"No saved account named '{name}'. Saved: {', '.join(self.names()) or 'none'}")
        return self._read(path)

    def save(self, name: str, login: Login) -> None:
        path = self._path(name)
        previous = read_json(path)
        if previous == login.to_dict():
            return
        if previous is not None:
            write_json(path.with_suffix(".json.bak"), previous)
        write_json(path, login.to_dict())

    def remove(self, name: str) -> None:
        """Delete a saved account; its tokens are kept in backups/ in case that was a mistake."""
        self.backup(self.load(name))
        self._path(name).unlink()
        self._path(name).with_suffix(".json.bak").unlink(missing_ok=True)

    def backup(self, login: Login) -> None:
        """Keep a copy of tokens that may belong to nobody saved. Duplicates are skipped and
        only logins whose sign-in has expired are pruned, plus the oldest beyond a hard cap."""
        existing = {path: self._read(path) for path in sorted(self.backups_dir.glob("*.json"))}
        if any(old.refresh_token == login.refresh_token for old in existing.values()):
            return
        now_ms = time.time() * 1000
        for path, old in existing.items():
            if (old.login_expires_at_ms or now_ms) < now_ms:
                path.unlink(missing_ok=True)
        write_json(self.backups_dir / f"{time.time_ns()}.json", login.to_dict())
        for path in sorted(self.backups_dir.glob("*.json"))[:-BACKUPS_KEPT]:
            path.unlink(missing_ok=True)

    def _read(self, path: Path) -> Login:
        try:
            return Login.from_dict(read_json(path))
        except (KeyError, TypeError):
            raise CswitchError(f"{path} is not a valid cSwitcher account file.") from None

    def _path(self, name: str) -> Path:
        if not NAME_PATTERN.match(name):
            raise CswitchError("Account names may only use lowercase letters, digits, '-' and '_' (max 32).")
        return self.accounts_dir / f"{name}.json"
