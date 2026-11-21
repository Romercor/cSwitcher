"""Adding an account interactively (`cswitch add`, `cswitch setup`): save the live
login under a name, or first sign Claude Code out (without /logout, which could
revoke the tokens) and wait until the user has run /login with the other account.
If that wait is cancelled, the previous login is put back, so Claude Code is never
left signed out by accident."""

from __future__ import annotations

from collections.abc import Callable

from cswitch.accounts import switcher
from cswitch.accounts.store import AccountStore
from cswitch.claude.home import ClaudeHome
from cswitch.claude.login import Login
from cswitch.errors import CswitchError


class Cancelled(CswitchError):
    pass


def add_account(home: ClaudeHome, store: AccountStore, name: str, new: bool, ask: Callable[[str], str],
                identify: switcher.Identify | None) -> Login:
    if new:
        if store.exists(name):
            raise CswitchError(f"'{name}' already exists. Pick a different name.")
        previous = switcher.sign_out_for_new_login(home, store, identify)
        try:
            ask("Claude Code is now signed out. Run /login in Claude Code with that account, then press Enter here... ")
            while (login := home.read_login()) is None or not login.account:
                ask("Not logged in yet. Finish /login in Claude Code, then press Enter... ")
        except (KeyboardInterrupt, EOFError):
            if previous is None:
                raise Cancelled("Cancelled; Claude Code is signed out.") from None
            switcher.switch_to(home, store, previous, identify)
            raise Cancelled(f"Cancelled; Claude Code is logged in as '{previous}' again.") from None
    return switcher.add_current(home, store, name, identify)
