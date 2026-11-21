"""`cswitch setup`: the whole setup in one guided pass, so a new computer needs only
the install and this. It saves the accounts (each after a /login in Claude Code;
the logged-in one first, under a name), asks for the main settings until each answer
is valid or takes them from a config.toml copied from another computer (`--config`;
an existing different one is kept as config.toml.bak), installs the hooks and makes
the scheduled priming tasks match the settings. Logins can't be copied: two
computers refreshing the same tokens would log each other out. An empty answer keeps
what is there, so running it again is safe; setting changes are logged like `cswitch
set`'s, and everything can be changed later with the usual commands."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cswitch import config, hooks, log, priming, schedule
from cswitch.accounts import switcher
from cswitch.accounts.adding import Cancelled, add_account
from cswitch.accounts.store import AccountStore
from cswitch.claude import settings as claude_settings
from cswitch.claude.home import ClaudeHome
from cswitch.errors import CswitchError
from cswitch.files.jsonfile import write_text_atomic
from cswitch.rotation import engine

Ask = Callable[[str], str]
Show = Callable[[str], None]

ACCOUNT_QUESTIONS = (
    ("order", "Accounts in order of preference, most used first"),
    ("exclude", "Accounts never to switch to automatically (none: -)"),
    ("reserve", "Weekly usage to keep for yourself, e.g. {first}:20 keeps 20% of {first}'s week (none: -)"),
)
PRIME_MODE_QUESTION = ("Start the 5-hour windows on a schedule (Windows tasks that can wake the PC): off, "
                       "together (all at once) or spread (one at a time, so a fresh window comes often)")
PRIME_QUESTIONS = (
    ("prime_from", "First prime of the day (for spread start early, e.g. 04:40, so all windows run by 08:00)"),
    ("prime_until", "No new primes after"),
    ("prime_days", "Days (e.g. mon tue wed thu fri sat sun)"),
)


def run(home: ClaudeHome, store: AccountStore, config_path: Path, config_file: Path | None, ask: Ask,
        show: Show) -> None:
    show("cSwitcher setup. Press Enter to keep what is shown in [brackets].")
    if config_file is not None:
        _take_config(config_file, config_path, show)
    else:
        config.write_default(config_path)
    _accounts(home, store, config_path, ask, show)
    names = store.names()
    if not names:
        raise CswitchError("No account is saved. Log in to Claude Code (/login), then run 'cswitch setup' again.")
    if config_file is None:
        if len(names) > 1:
            for name, question in ACCOUNT_QUESTIONS:
                _setting(config_path, name, f"{question.format(first=names[0])} ({' '.join(names)})", ask, show)
        _setting(config_path, "prime_mode", PRIME_MODE_QUESTION, ask, show)
        if config.load(config_path).prime_mode != "off":
            for name, question in PRIME_QUESTIONS:
                _setting(config_path, name, question, ask, show)
    settings = config.load(config_path)
    unknown = sorted({config.account_of(entry) for key in config.ACCOUNT_LIST_SETTINGS
                      for entry in getattr(settings, key)} - set(names))
    if unknown:
        show(f"Note: the settings name accounts that aren't saved here, ignored until added: {', '.join(unknown)}")
    claude_settings.install(home.settings_file, hooks.commands())
    schedule.sync(settings, priming.accounts_to_prime(store, settings), show, install_new=True)
    show(f"Done. Accounts: {', '.join(names)}; Claude Code is logged in as "
         f"'{switcher.active_name(home, store) or 'nobody saved'}'. Start a new Claude Code chat (or reload the "
         "VS Code window) so it switches automatically. Check with: cswitch status")


def _take_config(config_file: Path, config_path: Path, show: Show) -> None:
    if not config_file.is_file():
        raise CswitchError(f"No such file: {config_file}")
    config.load(config_file)
    text = config.read_text(config_file)
    if config_path.exists() and config.read_text(config_path) != text:
        write_text_atomic(config_path.with_name(config_path.name + ".bak"), config.read_text(config_path))
        show(f"Your previous settings are kept in {config_path.name}.bak.")
    write_text_atomic(config_path, text)
    log.write(config_path.parent, f"set all settings from {config_file} (setup)")
    show(f"Settings taken from {config_file}. Name the accounts as there.")


def _accounts(home: ClaudeHome, store: AccountStore, config_path: Path, ask: Ask, show: Show) -> None:
    """Until done: name the live login if it isn't saved (it must be, before signing out
    for the next one), ask for a first /login if nobody is logged in, then add more."""
    identify = engine.owner_check(config.load(config_path))
    show(f"Saved accounts: {', '.join(store.names()) or 'none'}")
    while True:
        live = home.read_login()
        if live is not None and switcher.active_name(home, store) is None:
            _save(home, store, ask(f"Claude Code is logged in as {live.email}. A short name for it (e.g. work): "),
                  False, ask, show, identify)
        elif live is None and not store.names():
            ask("Claude Code isn't logged in. Run /login in Claude Code with your first account, then press Enter... ")
            if home.read_login() is None:
                return
        elif name := ask("Add another account? Its short name (empty: done): "):
            _save(home, store, name, True, ask, show, identify)
        else:
            return


def _save(home: ClaudeHome, store: AccountStore, name: str, new: bool, ask: Ask, show: Show,
          identify: switcher.Identify | None) -> None:
    name = name.strip().lower()
    if not name:
        return
    try:
        login = add_account(home, store, name, new, ask, identify)
    except Cancelled:
        raise
    except CswitchError as e:
        show(str(e))
        return
    show(f"Saved {login.email} as '{name}'.")


def _setting(config_path: Path, name: str, question: str, ask: Ask, show: Show) -> None:
    """Ask until the answer is valid; empty keeps the current value, '-' empties a list."""
    while answer := ask(f"{question} [{_shown(getattr(config.load(config_path), name))}]: ").strip():
        words = [] if answer == "-" else answer.replace(",", " ").split()
        try:
            value = getattr(config.set_value(config_path, name, words), name)
        except CswitchError as e:
            show(str(e))
            continue
        log.write(config_path.parent, f"set {name} = {config.format_value(value)} (setup)")
        return


def _shown(value: object) -> str:
    if isinstance(value, tuple):
        return " ".join(value) or "none"
    return str(value)
