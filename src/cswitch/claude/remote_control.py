"""Remote Control for a headless Claude session (`cswitch run --remote-control`): lets
the user follow and steer the run from claude.ai/code or the Claude app. Remote
Control belongs to the signed-in account, so an account switch ends it ("terminal"
bridge failure); it is then turned on again, as a new session under the new account.
Other bridge failures are left to Claude, which recovers from them by itself. It is
only turned on while an account that allows it is live (organizations can disable
it), checked every CHECK_SECONDS, which also paces retries after a refusal; a
policy block is announced once and not retried on that account. The runner never
sees these events, nor turns started while it was pausing (by the user, remotely):
before the runner speaks, such a turn is let finish (up to DRAIN_SECONDS) and dropped,
except the latest list of background tasks, which the runner gets first."""

from __future__ import annotations

import time
from collections.abc import Callable

from cswitch.claude.headless import IDLE_EVENT, Session

CHECK_SECONDS = 60
DRAIN_SECONDS = 30 * 60


class RemoteControl:
    def __init__(self, session: Session, allowed_account: Callable[[], str | None], name: str,
                 say: Callable[[str], None]):
        """Wrap `session`. `allowed_account()` names the live account if Remote Control may be used on it."""
        self._session, self._allowed_account, self._name, self._say = session, allowed_account, name, say
        self._enabled_for: str | None = None
        self._pending: dict[str, str] = {}
        self._last_problem = ""
        self._checked_at = 0.0
        self._background: dict | None = None

    def send_user(self, text: str) -> None:
        if self._pending:
            self._drain()
        self._session.send_user(text)

    def close(self) -> None:
        self._session.close()

    def next_event(self, silence_seconds: float) -> dict | None:
        """Like Session.next_event, minus Remote Control's own events; checks the account every CHECK_SECONDS."""
        if self._background is not None:
            event, self._background = self._background, None
            return event
        deadline = time.monotonic() + silence_seconds
        while True:
            if time.monotonic() - self._checked_at >= CHECK_SECONDS:
                self._check_account()
            remaining = deadline - time.monotonic()
            event = self._session.next_event(max(0.0, min(CHECK_SECONDS, remaining)))
            if event is None:
                return None
            if event.get("type") == IDLE_EVENT and time.monotonic() < deadline:
                continue
            if not self._consume(event):
                return event

    def _drain(self) -> None:
        """Drop what turns started during the runner's pause left behind, letting a running one finish."""
        busy, deadline = False, time.monotonic() + DRAIN_SECONDS
        while time.monotonic() < deadline:
            event = self._session.next_event(CHECK_SECONDS if busy else 0)
            if event is None or (event.get("type") == IDLE_EVENT and not busy):
                return
            if event.get("type") == IDLE_EVENT:
                self._check_account()
            elif not self._consume(event):
                busy = event.get("type") != "result" and (busy or event.get("type") == "assistant")
                if event.get("subtype") == "background_tasks_changed":
                    self._background = event

    def _check_account(self) -> None:
        """Turn Remote Control on if an allowed account is live and it isn't on for that account yet."""
        self._checked_at = time.monotonic()
        account = self._allowed_account()
        if account is not None and account != self._enabled_for:
            self._enabled_for = account
            request_id = f"cswitch-rc-{len(self._pending) + 1}"
            self._pending[request_id] = account
            self._session.send({"type": "control_request", "request_id": request_id,
                                "request": {"subtype": "remote_control", "enabled": True, "name": self._name}})

    def _consume(self, event: dict) -> bool:
        """React to Remote Control's own events; True if the event is not for the runner."""
        if event.get("type") == "control_response":
            response = event.get("response") or {}
            account = self._pending.get(str(response.get("request_id")))
            if account is None:
                return False
            if response.get("subtype") == "success":
                self._last_problem = ""
                self._say(f"Remote Control on for '{account}': {(response.get('response') or {}).get('session_url')}")
            else:
                self._enabled_for = None
                self._problem(f"Remote Control unavailable for '{account}': {response.get('error')}")
            return True
        if event.get("type") == "system" and event.get("subtype") == "bridge_state":
            state = event.get("state")
            if state == "policy_disabled":
                self._problem(f"Remote Control is disabled for '{self._enabled_for}': {event.get('detail') or state}")
            elif state == "failed" and event.get("failure_kind") == "terminal":
                self._enabled_for = None
                self._problem(f"Remote Control ended: {event.get('detail') or state}")
            return True
        return False

    def _problem(self, text: str) -> None:
        """Announce a problem once, not on every retry."""
        if text != self._last_problem:
            self._last_problem = text
            self._say(text)
