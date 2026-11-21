# cSwitcher guide

The full reference: every command, setting and behaviour in detail. For a quick overview
see the [README](../README.md). cSwitcher works with the Claude Code CLI and the VS Code
extension, on Windows today. Using several subscriptions must be in line with Anthropic's
terms for your accounts.

## Install

Needs Claude Code (for `cswitch run`, the CLI too: `claude --version` works in PowerShell)
and [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`; it brings its own Python;
`pipx` works the same with Python 3.11+). Installing from a repository URL also needs git.

```
uv tool install git+<repository url>     # or: uv tool install <path to a copy of this folder>
uv tool update-shell                     # once: puts cswitch on your PATH; open a new window
```

If an older install put `<cSwitcher folder>\.venv\Scripts` on your PATH, remove that entry, so
the new `cswitch` is the one found.

Updating later: `uv tool upgrade cswitcher` (or `uv tool install --reinstall <path>`).

## Set up

Log in to Claude Code with one of your accounts (`/login`), then in PowerShell:

```
cswitch setup
```

It saves that account, then each other one (it signs Claude Code out, you `/login` with the
next account and press Enter; cancelling puts the previous login back), asks for the order,
excluded accounts, reserve and priming (Enter keeps what is shown), installs the hooks and
makes the scheduled priming tasks match. Running it again is safe.
Start a new Claude Code chat (or reload the VS Code window) afterwards: hooks are read when
a chat starts. `cswitch status` then shows every account's usage and what cSwitcher would do.

By hand instead: `cswitch add work`, `cswitch add personal --new` for each other account,
`cswitch install`, `cswitch set ...`, `cswitch schedule install`.

**Another computer:** install as above, then `cswitch setup --config <copy of config.toml>`
(from `%USERPROFILE%\.cswitcher` on the old one; it holds no secrets) and log in to each account
when asked. Logins can't be copied: two computers refreshing the same tokens would log each
other out. Power settings for waking (see priming below) are per computer.

## Everyday use

Nothing to do: while Claude works, cSwitcher checks the live account (at most once a
minute) and switches before a threshold. Every open chat continues on the new account from
its next message. If Claude hits a limit anyway, it switches at once.

| Command | What it does |
|---|---|
| `cswitch status` | usage per account and the current decision |
| `cswitch list` | saved accounts (`*` = live) |
| `cswitch switch <name>` | switch by hand; cSwitcher keeps that account until it nears a limit |
| `cswitch log` | what cSwitcher did, e.g. overnight |
| `cswitch prime [name...]` | start the 5-hour windows now (all accounts not excluded, or the named ones), so they reset earlier; one tiny Haiku request each, skipped where a window is running as far as cSwitcher last saw |
| `cswitch exclude <name>` / `include <name>` | keep an account out of / back in automatic switching |
| `cswitch remove <name>` | delete a saved account (its tokens are kept in backups) |
| `cswitch config` | show all settings |
| `cswitch set <setting> <value...>` | change a setting |
| `cswitch uninstall` | remove the hooks again (saved accounts stay) |

## Overnight: `cswitch run`

A VS Code chat stops when every account is used up, and nothing can type "continue" into
it. For guaranteed nights, run the task from PowerShell instead:

```
cd my-project
cswitch run "fix all failing tests and update the docs"
```

- Switches before limits (with the hooks installed) and right after a limit is hit.
- When every account is used up, waits until the first one resets, then continues.
- Retries errors and a crashed Claude with growing pauses; gives up (and notifies you)
  after 6 hours without progress, or when Claude crashes (or stays silent for 30 minutes)
  6 times in a row.
- Keeps the computer awake; stops when the task is done and notifies you.
- Follows `order` and `reserve` even if you chose an account by hand with `cswitch switch`.
- Uses your subscription logins: `ANTHROPIC_API_KEY` and similar overrides are not passed on to Claude.
- Prints a session id at the start (also in `cswitch log`): Ctrl-C stops the run, and
  `cswitch run --resume <id>` from the same folder continues it.

**Permissions.** Nobody can approve actions at night, so Claude may only do what it is
allowed in advance. The default `--permission-mode acceptEdits` lets it edit files in the
project; anything else must be allowed explicitly, using Claude Code's own rule syntax:

```
cswitch run "fix the tests" -- --allowedTools "Bash(pytest *)" "Bash(git diff *)"
```

Refused actions don't stop the run (Claude works around them); they are shown as they
happen and listed when the run ends. Options after `--` go to Claude Code. Only use
`--permission-mode bypassPermissions` in a folder you can throw away: nobody is watching.

Notifications ("every account is used up", "run finished") need Windows notifications on in
*Settings → System → Notifications*, with **Windows PowerShell** allowed and Focus Assist off.

**Remote Control.** `cswitch run --remote-control` lets you follow and steer the run from
claude.ai/code or the Claude app. Remote Control belongs to the logged-in account: after
each switch it comes back within about a minute of Claude working again, as a new session
under the new account
(the link is printed and logged), so to see it you must be signed in there as that account.
Organizations (e.g. Team plans) can disable it; list only the accounts that allow it:
`cswitch set remote_control_accounts personal other-pro`. The run itself keeps going on
the other accounts, just without Remote Control.

## Windows on a schedule: `cswitch prime`

A 5-hour window starts with an account's first message. `cswitch prime` starts them
earlier with one tiny Haiku request per account, so they reset sooner: an account primed
at 05:00 still has its whole allowance at 08:00, and a fresh one at 10:00. On a schedule:

```
cswitch set prime_mode spread      # or: together
cswitch set prime_from 04:40
cswitch schedule install
```

- `together` keeps every account's window running: all are primed at `prime_from`, and
  each again within about 15 minutes after it resets (e.g. 08:00-13:00, then 13:10-18:10).
- `spread` primes one account at a time, in `order`, a window's length divided by the
  number of accounts apart (3 accounts: about every 1 h 40 min), so resets come evenly
  spaced and the wait when everything is used up is short. Start it early: with 3
  accounts, `prime_from = "04:40"` has all three windows running by 08:00.
- Two Windows scheduled tasks: one checks every 10 minutes between `prime_from` and
  `prime_until` on `prime_days` (Mon-Fri by default) and primes what is due, so a late
  start, a window you started yourself or a missed run (computer asleep or off) is taken
  into account; the other wakes the computer for the morning's first prime(s) (later
  primes happen when the computer is awake anyway). They run only while you are logged
  on to Windows.
- Waking a sleeping computer needs wake timers allowed in the power plan (`cswitch
  schedule install` warns if they aren't). To allow them only when plugged in:
  `powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1`, then
  `powercfg /setactive SCHEME_CURRENT`.
- `cswitch set`, `exclude`, `add` and `remove` update the tasks; after editing
  `config.toml` by hand, run `cswitch schedule install` again. `cswitch schedule
  uninstall` removes them. Excluded accounts and accounts whose week is used up are not
  primed. Priming doesn't change weekly limits.
- These are automated requests; on Pro plans, Anthropic's consumer terms only allow
  automated access where they explicitly permit it, so decide for yourself.

## Settings

`cswitch config` shows them, `cswitch set` changes them (stored in
`%USERPROFILE%\.cswitcher\config.toml`).

| Setting | Default | Meaning |
|---|---|---|
| `order` | `[]` | preferred accounts first; the rest follow. With `priority`, cSwitcher switches back to an earlier account once it has room again (not from an account you chose by hand) |
| `exclude` | `[]` | never switched to automatically |
| `five_hour_threshold` | `90` | switch away at this 5-hour usage (%) |
| `seven_day_threshold` | `95` | switch away at this weekly usage (%) |
| `reserve` | `[]` | keep part of an account's week for yourself: `personal:30` stops automatic use at 70% weekly; when everything else is full, a run waits instead of using it (open chats keep going; hooks can't pause them). `cswitch switch personal` still uses it |
| `strategy` | `priority` | `priority` (first usable in `order`) or `most_remaining` |
| `check_interval_seconds` | `60` | minimum time between checks |
| `ask_anthropic` | `true` | read usage and token ownership from Anthropic when local data is unclear |
| `notifications` | `true` | desktop notifications |
| `remote_control_accounts` | `[]` | accounts where `run --remote-control` turns it on (empty: all) |
| `usage_history` | `false` | keep `usage.csv` for analysing the switching later (see below) |
| `prime_mode` | `off` | scheduled priming: `off`, `together` or `spread` (see above) |
| `prime_from` / `prime_until` | `08:00` / `20:00` | scheduled primes happen between these times |
| `prime_days` | `["mon", ..., "fri"]` | days with scheduled priming |

## How it works, and what it never does

- A login is two files: `%USERPROFILE%\.claude\.credentials.json` (tokens) and
  `oauthAccount` in `%USERPROFILE%\.claude.json`.
  cSwitcher swaps them safely while Claude Code is running; a running Claude picks up the
  new login on its next request.
- Usage comes from Claude Code's own saved numbers, or (read-only, with the live login)
  from Anthropic's usage endpoint.
- `cswitch prime` runs Claude for another account in a temporary copy of that login (open
  chats keep theirs) and saves refreshed tokens back. Automatic switching pauses while
  it does, usually for a few seconds per account.
- It never uses `/logout` (that revokes the token), never sends tokens anywhere except
  Anthropic's own API, and keeps a backup of any tokens whose owner is uncertain.
- Saved logins are unencrypted token files in `%USERPROFILE%\.cswitcher` (`accounts\`,
  `backups\`), readable by anything running as your Windows user, just like Claude Code's
  own `.credentials.json`. Activity, including setting changes, is in
  `%USERPROFILE%\.cswitcher\log.txt`. With `usage_history` on, `usage.csv` gets a line whenever
  an account's usage changes (time, account, live, 5-hour and weekly % and resets). Each file is
  capped at about 1-2 MB plus one older file. Open a copy in Excel: Excel locks the file, and
  lines can't be written meanwhile.
- The `CSWITCH_HOME` environment variable moves cSwitcher's folder (set it permanently,
  e.g. `setx CSWITCH_HOME D:\cswitcher`, because the hooks need it too); cSwitcher follows
  `CLAUDE_CONFIG_DIR` if you set it for Claude Code.

## Removing it

`cswitch uninstall` (hooks and scheduled tasks), then `cswitch switch <your usual account>`,
then `uv tool uninstall cswitcher` and delete `%USERPROFILE%\.cswitcher`.

## Development

```
py -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m unittest
```
