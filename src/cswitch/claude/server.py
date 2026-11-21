"""Read-only questions to Anthropic, asked with the live login only when the local
files can't answer: whose tokens are live, and fresh usage numbers. Uses the
access token Claude Code keeps fresh and never refreshes tokens itself. The token
is sent to api.anthropic.com only (redirects are refused), every call has a hard
deadline because it may run while Claude Code's refresh lock is held, and any
failure returns None so callers fall back to their local rules."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from cswitch.claude.login import Login
from cswitch.claude.usage import Usage, parse

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEADLINE_SECONDS = 5
MAX_RESPONSE_BYTES = 65536


def organization_of(login: Login) -> str | None:
    """The organization Anthropic says the login's access token belongs to."""
    body = _get_json(PROFILE_URL, login)
    organization = body.get("organization") if isinstance(body, dict) else None
    uuid = organization.get("uuid") if isinstance(organization, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


def usage_of(login: Login) -> Usage | None:
    """Current 5-hour and weekly usage of the login's account."""
    return parse(_get_json(USAGE_URL, login), time.time())


def _get_json(url: str, login: Login) -> object:
    """GET with the login's access token under a hard deadline; None on any failure."""
    if not login.access_token or (login.access_expires_at_ms or 0) <= time.time() * 1000:
        return None
    result: list[object] = [None]
    worker = threading.Thread(target=lambda: result.__setitem__(0, _fetch(url, login.access_token)), daemon=True)
    worker.start()
    worker.join(DEADLINE_SECONDS)
    return None if worker.is_alive() else result[0]


def _fetch(url: str, access_token: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "cswitcher"})
    request.add_unredirected_header("Authorization", f"Bearer {access_token}")
    opener = urllib.request.build_opener(_RefuseRedirects())
    try:
        with opener.open(request, timeout=DEADLINE_SECONDS) as response:
            return json.loads(response.read(MAX_RESPONSE_BYTES))
    except (OSError, ValueError):
        return None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
