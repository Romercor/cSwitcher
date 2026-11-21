"""A Claude Code login: the OAuth tokens plus the account identity they belong to.

Only the login keys of `.credentials.json` are part of a Login; other keys in
that file (e.g. MCP server tokens) stay untouched in the live file.
"""

from __future__ import annotations

from dataclasses import dataclass

LOGIN_KEYS = ("claudeAiOauth", "organizationUuid")


@dataclass(frozen=True)
class Login:
    credentials: dict
    account: dict

    @classmethod
    def from_dict(cls, data: dict) -> Login:
        return cls(data["credentials"], data["oauthAccount"])

    def to_dict(self) -> dict:
        return {"credentials": self.credentials, "oauthAccount": self.account}

    @property
    def account_uuid(self) -> str | None:
        return self.account.get("accountUuid")

    @property
    def email(self) -> str:
        return self.account.get("emailAddress", "?")

    @property
    def organization(self) -> str | None:
        """The account's organization per the identity file; accounts are told apart by it,
        because one person (same account uuid) can belong to several organizations."""
        return self.account.get("organizationUuid") or None

    @property
    def token_organization(self) -> str | None:
        """Organization stored next to the tokens, which survives a stale identity file.
        Claude Code doesn't always write it (it is missing right after /login)."""
        return self.credentials.get("organizationUuid") or None

    @property
    def is_consistent(self) -> bool:
        """False only when the tokens' organization is known and differs from the identity's."""
        return self.token_organization is None or self.token_organization == self.organization

    @property
    def _oauth(self) -> dict:
        return self.credentials.get("claudeAiOauth", {})

    @property
    def access_token(self) -> str | None:
        return self._oauth.get("accessToken")

    @property
    def access_expires_at_ms(self) -> int | None:
        return self._oauth.get("expiresAt")

    @property
    def refresh_token(self) -> str | None:
        return self._oauth.get("refreshToken")

    @property
    def subscription(self) -> str:
        return self._oauth.get("subscriptionType", "?")

    @property
    def login_expires_at_ms(self) -> int | None:
        return self._oauth.get("refreshTokenExpiresAt")
