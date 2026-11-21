"""Shared fixture: a throwaway fake Claude home and cSwitcher home per test."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from cswitch.accounts import switcher
from cswitch.accounts.store import AccountStore
from cswitch.claude.home import ClaudeHome


class FakeHomeTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        claude_dir = self.root / ".claude"
        claude_dir.mkdir()
        self.home = ClaudeHome(claude_dir, self.root / ".claude.json", claude_dir)
        self.store = AccountStore(self.root / ".cswitcher")
        self.write(self.home.global_config, {"numStartups": 7})

    def write(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def log_in(self, uuid: str, token: str, org: str | None = None, fresh: bool = False,
               plan: str = "max") -> None:
        """Simulate Claude Code writing a login for account `uuid` with tokens `token`.
        `fresh` mimics right after /login, when the token file has no organization yet."""
        org = org if org is not None else f"org-{uuid}"
        self.write_tokens(token, None if fresh else org, plan)
        self.set_identity(uuid, org)

    def set_identity(self, uuid: str, org: str) -> None:
        """Simulate the identity file (oauthAccount) alone being written, e.g. a torn switch."""
        config = self.read(self.home.global_config)
        config["oauthAccount"] = {"accountUuid": uuid, "emailAddress": f"{uuid}@example.com",
                                  "organizationUuid": org}
        self.write(self.home.global_config, config)

    def write_tokens(self, token: str, org: str | None, plan: str = "max") -> None:
        """Simulate Claude Code writing (refreshed) tokens without touching the identity file."""
        credentials = self.read(self.home.credentials_file) if self.home.credentials_file.exists() else {}
        credentials["claudeAiOauth"] = {"accessToken": f"at-{token}", "refreshToken": f"rt-{token}",
                                        "refreshTokenExpiresAt": 1797698667181, "subscriptionType": plan}
        credentials.pop("organizationUuid", None)
        if org:
            credentials["organizationUuid"] = org
        self.write(self.home.credentials_file, credentials)

    def live_refresh_token(self) -> str:
        return self.home.read_login().refresh_token

    def add_two_accounts(self, fresh: bool = False, plans: tuple[str, str] = ("max", "max")) -> None:
        """Leaves 'work' (uuid-a) and 'personal' (uuid-b, live) saved."""
        self.log_in("uuid-a", "a1", fresh=fresh, plan=plans[0])
        switcher.add_current(self.home, self.store, "work")
        switcher.sign_out_for_new_login(self.home, self.store)
        self.log_in("uuid-b", "b1", fresh=fresh, plan=plans[1])
        switcher.add_current(self.home, self.store, "personal")

    def write_claude_cache(self, uuid: str, five: float, seven: float = 5, fetched_at: float | None = None) -> None:
        """Simulate Claude Code saving usage numbers (`cachedUsageUtilization`) for account `uuid`."""
        config = self.read(self.home.global_config)
        config["cachedUsageUtilization"] = {
            "fetchedAtMs": (time.time() if fetched_at is None else fetched_at) * 1000, "accountUuid": uuid,
            "utilization": {"five_hour": {"utilization": five, "resets_at": None},
                            "seven_day": {"utilization": seven, "resets_at": None}},
        }
        self.write(self.home.global_config, config)

    def backed_up_refresh_tokens(self) -> list[str]:
        return [self.read(p)["credentials"]["claudeAiOauth"]["refreshToken"]
                for p in self.store.backups_dir.glob("*.json")]
