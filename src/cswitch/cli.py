"""Command-line entry point: `cswitch <command>`."""

from __future__ import annotations

import argparse
import math
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from cswitch import awake, config, hooks, log, priming, runner, schedule, wizard
from cswitch.accounts import switcher
from cswitch.accounts.adding import add_account
from cswitch.accounts.store import AccountStore
from cswitch.claude import headless, settings
from cswitch.claude.remote_control import RemoteControl
from cswitch.claude.home import ClaudeHome
from cswitch.claude.usage import Window
from cswitch.errors import CswitchError
from cswitch.paths import cswitch_home
from cswitch.rotation import engine, policy


def cmd_setup(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    try:
        wizard.run(home, store, config.config_path(), args.config, input, print)
    except (KeyboardInterrupt, EOFError):
        raise CswitchError("Setup stopped. Run 'cswitch setup' again to continue; what is done stays.") from None


def cmd_add(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    login = add_account(home, store, args.name, args.new, input, _owner_check())
    print(f"Saved {login.email} as '{args.name}'.")
    _refresh_schedule(store)


def cmd_list(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    saved = store.all()
    if not saved:
        print("No saved accounts. Log in to Claude Code and run 'cswitch add <name>'.")
        return
    active = switcher.active_name(home, store)
    for name, login in saved.items():
        marker = "*" if name == active else " "
        print(f"{marker} {name:<16} {login.email:<32} {login.subscription:<6} login until {_date(login.login_expires_at_ms)}")


def cmd_switch(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    if engine.switch(home, store, cswitch_home(), args.name, time.time()):
        log.write(cswitch_home(), f"switched to '{args.name}' by hand")
        print(f"Switched to '{args.name}'.")
    else:
        print(f"'{args.name}' is already active.")


def cmd_status(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    names = store.names()
    if not names:
        print("No saved accounts. Log in to Claude Code and run 'cswitch add <name>'.")
        return
    now = time.time()
    engine.look_around(store, cswitch_home(), now, force=True)
    result = engine.check(home, store, cswitch_home(), now)
    for name in policy.ordered(names, result.settings):
        usage = result.memory.usage.get(name)
        marker = "*" if name == result.active else " "
        notes = "  (excluded)" if name in result.settings.exclude else ""
        weekly = result.settings.weekly_threshold(name)
        notes += f"  (keeps {100 - weekly:g}% weekly)" if weekly < result.settings.seven_day_threshold else ""
        if usage is None:
            print(f"{marker} {name:<16} no usage seen yet{notes}")
        else:
            five, seven = _format_window(usage.five_hour, now), _format_window(usage.seven_day, now)
            print(f"{marker} {name:<16} 5h {five:<24} 7d {seven:<24} seen {_when(usage.fetched_at, now)}{notes}")
    decision = result.decision
    limits = f"thresholds: 5h {result.settings.five_hour_threshold:g}%, 7d {result.settings.seven_day_threshold:g}%"
    stale = " The live account's numbers are stale, so it is judged 10 points higher." if decision.stale else ""
    if decision.switch_to:
        print(f"Would switch to '{decision.switch_to}': {decision.reason} ({limits}).{stale}")
    elif decision.exhausted:
        frees = f"; first frees up {_when(decision.frees_at, now)}" if decision.frees_at else "; no reset time known"
        print(f"No usable account: {decision.reason}{frees} ({limits}).{stale}")
    else:
        print(f"Stays: {decision.reason} ({limits}).{stale}")
    if schedule.installed():
        settings = result.settings
        print(f"Scheduled priming: {settings.prime_mode}, between {settings.prime_from} and {settings.prime_until} "
              f"on {', '.join(settings.prime_days)}.")


def cmd_remove(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    live = switcher.active_name(home, store) == args.name
    engine.remove(store, cswitch_home(), args.name, time.time())
    path = config.config_path()
    current = config.load(path)
    for key in PRUNED_ON_REMOVE:
        entries = getattr(current, key)
        kept = [entry for entry in entries if config.account_of(entry) != args.name]
        if kept != list(entries):
            config.set_value(path, key, kept)
    note = " Claude Code stays logged in with it until you switch." if live else ""
    print(f"Removed '{args.name}'; its tokens are kept in backups.{note}")
    _refresh_schedule(store)


def cmd_config(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    path = config.config_path()
    created = config.write_default(path)
    print(f"{'Created' if created else 'Config file'}: {path}")
    for line in config.describe(config.load(path)):
        print(f"  {line}")
    print("Change one with: cswitch set <setting> <value...>")


def cmd_set(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    value = getattr(config.set_value(config.config_path(), args.setting, args.values), args.setting)
    log.write(cswitch_home(), f"set {args.setting} = {config.format_value(value)}")
    print(f"{args.setting} = {config.format_value(value)}")
    if args.setting in config.ACCOUNT_LIST_SETTINGS:
        known = store.names()
        unknown = [account for account in map(config.account_of, value) if account not in known]
        if unknown:
            print(f"Note: not saved accounts, ignored until added: {', '.join(unknown)}")
    if args.setting in SCHEDULE_SETTINGS:
        _refresh_schedule(store)
    if args.setting == "prime_mode" and value != "off" and not schedule.installed():
        print("Start it with: cswitch schedule install")


def cmd_exclude(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    path = config.config_path()
    excluded = list(config.load(path).exclude)
    if args.excluding:
        store.load(args.name)
        excluded = excluded if args.name in excluded else [*excluded, args.name]
        state = "excluded from"
    elif args.name in excluded:
        excluded.remove(args.name)
        state = "back in"
    else:
        print(f"'{args.name}' wasn't excluded.")
        return
    config.set_value(path, "exclude", excluded)
    print(f"'{args.name}' is {state} automatic switching. Excluded now: {', '.join(excluded) or 'none'}")
    _refresh_schedule(store)


def cmd_install(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    settings.install(home.settings_file, hooks.commands())
    config.write_default(config.config_path())
    print(f"Installed in {home.settings_file}. New Claude Code chats now switch accounts automatically.")
    print("Settings: cswitch config  |  Activity: cswitch log")


def cmd_uninstall(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    settings.uninstall(home.settings_file)
    print("cSwitcher's hooks were removed from Claude Code's settings. Saved accounts are kept.")
    if schedule.installed() and schedule.uninstall():
        print("Scheduled priming removed too.")


def cmd_run(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> int:
    if (args.task is None) == (args.resume is None):
        raise CswitchError("Give either a task or --resume <session id>.")
    if not store.names():
        raise CswitchError("No saved accounts. Log in to Claude Code and run 'cswitch add <name>'.")
    reserved = [a for a in args.claude_args if a.split("=")[0] in RESERVED_CLAUDE_OPTIONS]
    if reserved:
        raise CswitchError(f"cswitch run sets {', '.join(reserved)} itself; leave it out after --.")
    if not settings.installed(home.settings_file):
        print("Note: cSwitcher's hooks aren't installed ('cswitch install'), so it only switches after a limit is hit.")
    session_id = args.resume or str(uuid.uuid4())
    cwd = Path.cwd()
    root = cswitch_home()
    engine.forget_hand_choice(root)
    claude_args = ["--permission-mode", args.permission_mode, "--append-system-prompt", runner.UNATTENDED,
                   *args.claude_args]
    resume_hint = f"continue later from this folder with: cswitch run --resume {session_id}"
    blocked: set[str] = set()

    def start(resume: bool) -> runner.Session:
        session = headless.Session(session_id, resume, cwd, claude_args)
        if not args.remote_control:
            return session
        return RemoteControl(session, lambda: _remote_control_account(home, store), f"cswitch: {cwd.name}",
                             lambda text: runner.say(root, print, text), blocked)

    print(f"Session {session_id} in {cwd}. Stop with Ctrl-C; {resume_hint}")
    log.write(root, f"run: session {session_id} started in {cwd}")
    try:
        with awake.keep_awake():
            return runner.run(args.task, start, home, store, root, print, args.background_hours * 3600)
    except KeyboardInterrupt:
        print(f"\nStopped; {resume_hint}")
        log.write(root, f"run: session {session_id} stopped by the user")
        return 130


def cmd_prime(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> int:
    if args.scheduled:
        return _prime_scheduled(home, store)
    unknown = [name for name in args.names if not store.exists(name)]
    if unknown:
        raise CswitchError(f"No saved account named {', '.join(unknown)}. Saved: {', '.join(store.names()) or 'none'}")
    got_results = failed = False
    with awake.keep_awake():
        for result in priming.prime(home, store, cswitch_home(), time.time(), args.names or None):
            print(f"{result.account:<16} {result.outcome}: {result.detail}", flush=True)
            got_results, failed = True, failed or result.outcome is priming.Outcome.FAILED
    if not got_results:
        print("Nothing to prime: no saved accounts, or all are excluded.")
    return 1 if failed else 0


def _prime_scheduled(home: ClaudeHome, store: AccountStore) -> int:
    """Run by the scheduled task, without a console: problems go to the log."""
    root = cswitch_home()
    try:
        with awake.keep_awake():
            results = list(priming.run_scheduled(home, store, root, time.time()))
    except Exception as e:
        log.write(root, f"prime: scheduled run failed: {type(e).__name__}: {e}")
        return 1
    return 1 if any(result.outcome is priming.Outcome.FAILED for result in results) else 0


def cmd_schedule(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    if args.action == "uninstall":
        print("Scheduled priming removed." if schedule.uninstall() else "Scheduled priming wasn't installed.")
        return
    settings = config.load(config.config_path())
    if settings.prime_mode == "off":
        raise CswitchError("Set prime_mode first: cswitch set prime_mode spread (or together)")
    accounts = priming.accounts_to_prime(store, settings)
    if not accounts:
        raise CswitchError("No accounts to prime: none saved, or all excluded.")
    schedule.sync(settings, accounts, print, install_new=True)


def _refresh_schedule(store: AccountStore) -> None:
    """Keep installed scheduled priming in step with the settings and accounts it was made from;
    the change itself is already saved, whatever happens here."""
    try:
        settings = config.load(config.config_path())
        schedule.sync(settings, priming.accounts_to_prime(store, settings), print, install_new=False)
    except (CswitchError, OSError) as e:
        print(f"Done, but updating scheduled priming failed ({e}). Run 'cswitch schedule install' again.")


def cmd_log(args: argparse.Namespace, home: ClaudeHome, store: AccountStore) -> None:
    lines = log.tail(cswitch_home(), args.lines)
    print("\n".join(lines) if lines else "Nothing logged yet.")


def _positive(text: str) -> float:
    value = float(text)
    if not 0 < value < math.inf:
        raise argparse.ArgumentTypeError(f"must be a positive number, not {text}")
    return value


def _remote_control_account(home: ClaudeHome, store: AccountStore) -> str | None:
    """The live account, if `cswitch run --remote-control` may turn Remote Control on for it."""
    try:
        live = switcher.active_name(home, store)
        allowed = config.load(config.config_path()).remote_control_accounts
    except (CswitchError, OSError):
        return None
    return live if live is not None and (not allowed or live in allowed) else None


def _owner_check() -> switcher.Identify | None:
    return engine.owner_check(config.load(config.config_path()))


def _read_stdin() -> str:
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


def _date(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d") if ms else "?"


def _format_window(window: Window, now: float) -> str:
    used = f"{window.used_at(now):.0f}%"
    return f"{used} (resets {_when(window.resets_at, now)})" if window.resets_at and window.resets_at > now else used


def _when(timestamp: float, now: float) -> str:
    """A short local time; with the date once it is more than 6 days away."""
    pattern = "%a %H:%M" if abs(timestamp - now) < 6 * 86400 else "%d %b %H:%M"
    return datetime.fromtimestamp(timestamp).strftime(pattern)


PRUNED_ON_REMOVE = ("order", "exclude", "reserve")
SCHEDULE_SETTINGS = ("exclude", "prime_mode", "prime_from", "prime_until", "prime_days")
RESERVED_CLAUDE_OPTIONS = ("-p", "--print", "-r", "--resume", "-c", "--continue", "--session-id",
                           "--input-format", "--output-format")
GETTING_STARTED = """getting started:
  1. log in to Claude Code, then:   cswitch setup   (accounts, settings, hooks, priming)
  2. watch it:                      cswitch status, cswitch log
  another computer:                 cswitch setup --config <copy of config.toml>
  long unattended task:             cswitch run "fix all failing tests"
settings: cswitch config, cswitch set <setting> <value...>, cswitch exclude <name>"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cswitch", description="Switch Claude Code between saved accounts.",
                                     epilog=GETTING_STARTED, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="set everything up in one guided pass (also on a new computer)")
    setup.add_argument("--config", type=Path, metavar="FILE", help="take the settings from this config.toml")
    setup.set_defaults(handler=cmd_setup)
    add = commands.add_parser("add", help="save the account Claude Code is logged in with")
    add.add_argument("name", type=str.lower)
    add.add_argument("--new", action="store_true", help="sign out first so you can /login with another account")
    add.set_defaults(handler=cmd_add)

    commands.add_parser("list", help="show saved accounts (* = active)").set_defaults(handler=cmd_list)

    switch = commands.add_parser("switch", help="make a saved account the active login")
    switch.add_argument("name", type=str.lower)
    switch.set_defaults(handler=cmd_switch)

    remove = commands.add_parser("remove", help="delete a saved account (its tokens are kept in backups)")
    remove.add_argument("name", type=str.lower)
    remove.set_defaults(handler=cmd_remove)

    for name, excluded, text in (("exclude", True, "never switch to this account automatically"),
                                 ("include", False, "let automatic switching use this account again")):
        pool = commands.add_parser(name, help=text)
        pool.add_argument("name", type=str.lower)
        pool.set_defaults(handler=cmd_exclude, excluding=excluded)

    status = commands.add_parser("status", help="show each account's usage and what cSwitcher would do")
    status.set_defaults(handler=cmd_status)
    config_cmd = commands.add_parser("config", help="show the settings (creates the config file if missing)")
    config_cmd.set_defaults(handler=cmd_config)
    set_cmd = commands.add_parser("set", help="change a setting, e.g. 'set five_hour_threshold 85' or 'set order team pro'")
    set_cmd.add_argument("setting", help="a name shown by 'cswitch config'")
    set_cmd.add_argument("values", nargs="*", help="the new value; for account lists a list of names (none clears it)")
    set_cmd.set_defaults(handler=cmd_set)

    run = commands.add_parser("run", help="work on a task unattended until it is done (switches, waits, continues)",
                              epilog="follows order and reserve even after 'cswitch switch'; more claude options go "
                                     'after --, e.g.: cswitch run "fix the tests" -- --model opus')
    run.add_argument("task", nargs="?", help="what Claude should do, in the current folder")
    run.add_argument("--resume", metavar="SESSION_ID", help="continue an earlier run instead")
    run.add_argument("--remote-control", action="store_true",
                     help="follow the run from claude.ai/code or the Claude app (see remote_control_accounts)")
    run.add_argument("--permission-mode", default="acceptEdits",
                     help="what Claude may do without asking (default: acceptEdits, i.e. edit files)")
    run.add_argument("--background-hours", type=_positive, default=runner.BACKGROUND_SECONDS / 3600, metavar="HOURS",
                     help="how long to wait for background work (e.g. a long job) still running when Claude "
                          "has finished; then it is stopped (default: %(default)g)")
    run.set_defaults(handler=cmd_run)

    prime = commands.add_parser("prime", help="start the 5-hour windows now with one tiny Haiku request each "
                                              "(all accounts not excluded, or the named ones)")
    prime.add_argument("names", nargs="*", type=str.lower, metavar="NAME", help="accounts to prime")
    prime.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    prime.set_defaults(handler=cmd_prime)

    schedule_cmd = commands.add_parser("schedule", help="prime windows on a schedule (settings: prime_mode, "
                                                        "prime_from, prime_until, prime_days)")
    schedule_cmd.add_argument("action", choices=("install", "uninstall"))
    schedule_cmd.set_defaults(handler=cmd_schedule)

    commands.add_parser("install", help="let Claude Code switch accounts automatically").set_defaults(handler=cmd_install)
    commands.add_parser("uninstall", help="remove cSwitcher's hooks again").set_defaults(handler=cmd_uninstall)
    log_cmd = commands.add_parser("log", help="show what cSwitcher did recently")
    log_cmd.add_argument("-n", "--lines", type=int, default=30, help="how many lines (default 30)")
    log_cmd.set_defaults(handler=cmd_log)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:
        return _run_hook(argv[1:])
    argv, claude_args = _split_passthrough(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if claude_args and args.command != "run":
        parser.error("options after -- are only for 'cswitch run'")
    args.claude_args = claude_args
    try:
        return args.handler(args, ClaudeHome.locate(), AccountStore.locate()) or 0
    except (CswitchError, OSError) as e:
        print(f"cswitch: {e}", file=sys.stderr)
        return 1


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """cswitch's own arguments, and the claude options after `--`."""
    if "--" not in argv:
        return argv, []
    split = argv.index("--")
    return argv[:split], argv[split + 1:]


def _run_hook(args: list[str]) -> int:
    """Claude Code hooks bypass argument parsing and always exit 0: exit code 2 would
    make Claude Code block the action, so no input may ever produce it."""
    try:
        hooks.handle(args[0] if args else "", _read_stdin(), ClaudeHome.locate(), AccountStore.locate(),
                     cswitch_home(), time.time())
    except BaseException:
        pass
    return 0
