"""Account operations: save the live login, prepare for a fresh /login, and
switch accounts. Every operation that writes runs under Claude Code's refresh
lock and first copies the live tokens into their owner's snapshot, because
Claude Code may have refreshed (rotated) them since the snapshot was taken.
When the local files can't settle whose the live tokens are, an optional
`identify` callback (asking Anthropic) is consulted, at most once per operation.
`active_name` never asks it, so read-only commands stay offline."""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from cswitch.accounts.store import AccountStore
from cswitch.claude.home import ClaudeHome
from cswitch.claude.login import Login
from cswitch.errors import CswitchError

Identify = Callable[[Login], str | None]


def add_current(home: ClaudeHome, store: AccountStore, name: str, identify: Identify | None = None) -> Login:
    with home.locked():
        live = _require_live(home)
        saved = store.all()
        owner, certain = _owner(saved, live, identify)
        if owner and owner != name:
            raise CswitchError(f"{live.email} is already saved as '{owner}' (accounts are told apart by organization).")
        if owner is None:
            if name in saved:
                raise CswitchError(f"'{name}' already holds another account. Pick a different name.")
            store.save(name, live)
        elif _save_live(store, saved, live, owner, certain) is None:
            raise CswitchError("Can't tell which account the current tokens belong to. Run /login in Claude Code again.")
        return live


def sign_out_for_new_login(home: ClaudeHome, store: AccountStore, identify: Identify | None = None) -> str | None:
    """Move the live login out of Claude Code's way without /logout, which could revoke it.
    Returns the name of the account that was signed out, or None if nobody was logged in."""
    with home.locked():
        live = home.read_login()
        if live is None:
            return None
        owner, _ = _sync_live(store, live, identify)
        if owner is None:
            raise CswitchError(f"The current login ({live.email}) is not saved. Run 'cswitch add <name>' first.")
        home.clear_login()
        return owner


def switch_to(home: ClaudeHome, store: AccountStore, name: str, identify: Identify | None = None) -> bool:
    """Make `name` the live login. Returns False if it certainly already was.
    An unknown name fails before anything changes; if writing fails halfway,
    the previous live login is put back."""
    with home.locked():
        store.load(name)
        live = home.read_login()
        owner, certain = None, False
        if live is not None:
            owner, certain = _sync_live(store, live, identify)
            if owner is None:
                store.backup(live)
        target = store.load(name)
        if live is not None and owner == name and certain and live.account == target.account:
            return False
        try:
            home.write_login(target)
        except BaseException:
            _restore(home, live)
            raise
        written = home.read_login()
        if written is None or written.refresh_token != target.refresh_token:
            raise CswitchError("A running Claude Code process overwrote the switch. Please retry.")
        return True


def active_name(home: ClaudeHome, store: AccountStore) -> str | None:
    live = home.read_login()
    return _owner(store.all(), live)[0] if live else None


def _sync_live(store: AccountStore, live: Login, identify: Identify | None) -> tuple[str | None, bool]:
    """Find the live tokens' owner and copy them into its snapshot. Returns (owner, certain);
    owner is None if the tokens belong to no saved account or can't be attributed safely."""
    saved = store.all()
    owner, certain = _owner(saved, live, identify)
    if owner is None or _save_live(store, saved, live, owner, certain) is None:
        return None, False
    return owner, certain


def _save_live(store: AccountStore, saved: dict[str, Login], live: Login, owner: str, certain: bool) -> str | None:
    """Store the live tokens as `owner`'s, keeping the saved identity if the identity
    file disagrees. On uncertain evidence the old snapshot is backed up first, and a
    different plan type means the tokens are not attributed at all (returns None)."""
    snapshot = saved[owner]
    if not certain:
        if live.subscription != snapshot.subscription:
            return None
        store.backup(snapshot)
    identity = live.account if live.organization == snapshot.organization else snapshot.account
    store.save(owner, Login(live.credentials, identity))
    return owner


def _owner(saved: dict[str, Login], live: Login, identify: Identify | None = None) -> tuple[str | None, bool]:
    """The saved account the live tokens belong to, and whether that is certain.
    Certain: the same refresh token, the organization stored with the tokens, or
    Anthropic confirming the identity file's organization. Uncertain: the identity
    file alone (it can be stale), or Anthropic naming a different saved account.
    Asking Anthropic happens only when the files don't settle it."""
    for name, login in saved.items():
        if login.refresh_token == live.refresh_token:
            return name, True
    if live.token_organization:
        return _by_organization(saved, live.token_organization), True
    if identify is not None:
        organization = identify(live)
        owner = _by_organization(saved, organization)
        if owner is not None:
            return owner, organization == live.organization
    return _by_organization(saved, live.organization), False


def _by_organization(saved: dict[str, Login], organization: str | None) -> str | None:
    if organization is None:
        return None
    return next((name for name, login in saved.items() if login.organization == organization), None)


def _restore(home: ClaudeHome, previous: Login | None) -> None:
    """Best effort: put the previous login back; the original error is what gets reported."""
    with contextlib.suppress(Exception):
        if previous is None:
            home.clear_login()
        else:
            home.write_login(previous)


def _require_live(home: ClaudeHome) -> Login:
    live = home.read_login()
    if live is None:
        raise CswitchError("Claude Code is not logged in. Run /login in Claude Code first.")
    if not live.organization:
        raise CswitchError("Claude Code's account info is missing. Start Claude Code once, then retry.")
    if not live.is_consistent:
        raise CswitchError("Claude Code's token and account files disagree. Run /login in Claude Code again.")
    return live
