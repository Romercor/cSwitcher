"""Claude Code's on-disk login layout.

A login lives in two files: the tokens in `<storage dir>/.credentials.json` and
the `oauthAccount` identity in the global config. We take the same locks Claude
Code takes (verified in its 2.1.3xx binary): the token-refresh locks around a
whole operation, plus the credentials-write and config-write locks, taken together
around writing a login, so tokens and identity can't end up from different accounts. `lock_timeout` bounds the wait for each set of locks (None: the locks
module's default), so a caller with a time limit, like a hook, can't be stuck.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from cswitch.claude.login import LOGIN_KEYS, Login
from cswitch.files import locks
from cswitch.files.jsonfile import read_json, write_json
from cswitch.files.locks import Lock


@dataclass(frozen=True)
class ClaudeHome:
    config_dir: Path
    global_config: Path
    storage_dir: Path
    lock_timeout: float | None = None

    @classmethod
    def locate(cls) -> ClaudeHome:
        """Resolve paths the way Claude Code does, honoring its env overrides."""
        default_dir = Path.home() / ".claude"
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        config_dir = Path(override).expanduser() if override else default_dir
        global_config = config_dir / ".claude.json" if override else Path.home() / ".claude.json"
        storage = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
        if storage is None:
            storage_dir = config_dir
        else:
            storage_dir = Path(storage).expanduser() if storage else default_dir
        return cls(config_dir, global_config, storage_dir)

    @classmethod
    def isolated(cls, folder: Path) -> ClaudeHome:
        """A self-contained Claude config in `folder`, for running Claude as another login."""
        return cls(folder, folder / ".claude.json", folder)

    @property
    def credentials_file(self) -> Path:
        return self.storage_dir / ".credentials.json"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def refresh_locks(self) -> list[Lock]:
        return [
            (self.config_dir / ".oauth_refresh.lock", 60),
            (Path(f"{self.config_dir.resolve()}.lock"), 60),
        ]

    @property
    def credentials_lock(self) -> Lock:
        return (self.storage_dir / ".storage-write.lock", 15)

    @property
    def config_lock(self) -> Lock:
        return (Path(f"{self.global_config}.lock"), 10)

    def locked(self) -> AbstractContextManager[None]:
        """Hold Claude Code's token-refresh locks for a whole read-sync-write operation."""
        return locks.hold(self.refresh_locks, self.lock_timeout)

    def read_login(self) -> Login | None:
        credentials = self._read_credentials()
        if "claudeAiOauth" not in credentials:
            return None
        login_part = {key: credentials[key] for key in LOGIN_KEYS if key in credentials}
        account = (read_json(self.global_config) or {}).get("oauthAccount") or {}
        return Login(login_part, account)

    def usage_cache(self) -> dict | None:
        """The usage numbers Claude Code last fetched for the active account."""
        return (read_json(self.global_config) or {}).get("cachedUsageUtilization")

    def write_login(self, login: Login) -> None:
        with locks.hold([self.credentials_lock, self.config_lock], self.lock_timeout):
            self._update_credentials(login.credentials)
            self._set_account(login.account)

    def clear_login(self) -> None:
        with locks.hold([self.credentials_lock, self.config_lock], self.lock_timeout):
            self._update_credentials({})
            self._set_account(None)

    def _read_credentials(self) -> dict:
        return read_json(self.credentials_file) or {}

    def _update_credentials(self, login_part: dict) -> None:
        """Replace the login keys of the credentials store, keeping everything else."""
        credentials = {k: v for k, v in self._read_credentials().items() if k not in LOGIN_KEYS}
        write_json(self.credentials_file, credentials | login_part)

    def _set_account(self, account: dict | None) -> None:
        config = read_json(self.global_config) or {}
        if account is None:
            config.pop("oauthAccount", None)
        else:
            config["oauthAccount"] = account
        write_json(self.global_config, config)
