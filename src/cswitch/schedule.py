"""The Windows scheduled tasks behind `prime_mode`, both running `cswitch prime
--scheduled` as the user who installed them (only while that user is logged on):
one every CHECK_MINUTES from prime_from to prime_until on prime_days, catching up
on a run missed while the computer slept or was off, and one that wakes the
computer for the morning's first prime(s), if the power plan allows wake timers
(the wake times are planned ones: they can't follow a first prime that came late).
Each run decides what is due, so the tasks only hold the times; `sync` makes them
match the settings whenever those may have changed. Shell-only overrides of
cSwitcher's or Claude's folders are refused, since the tasks wouldn't see them.
macOS is not supported yet."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from cswitch import config, priming
from cswitch.errors import CswitchError
from cswitch.paths import cswitch_home

CHECK_TASK = "cSwitcher prime"
WAKE_TASK = "cSwitcher prime wake"
CHECK_MINUTES = 10
FOLDER_VARIABLES = ("CSWITCH_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR")
DAY_NAMES = dict(zip(config.WEEKDAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                                         "Sunday")))

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts Claude 5-hour windows on schedule (cswitch prime).</Description></RegistrationInfo>
  <Triggers>{triggers}
  </Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>{catch_up}</StartWhenAvailable>
    <WakeToRun>{wake}</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{command}</Command><Arguments>-I -m cswitch prime --scheduled</Arguments><WorkingDirectory>{folder}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""
TRIGGER_XML = """
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>{repetition}
      <ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>
    </CalendarTrigger>"""
WAKE_TIMERS_OFF = """Note: wake timers are off in the power plan, so a sleeping PC won't wake up for this.
To allow them when plugged in:
  powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
  powercfg /setactive SCHEME_CURRENT"""
REPETITION_XML = """
      <Repetition><Interval>PT{interval}M</Interval><Duration>PT{duration}M</Duration></Repetition>"""


def install(settings: config.Config, account_count: int) -> None:
    _windows_only()
    shell_only = [name for name in FOLDER_VARIABLES if _shell_only(name)]
    if shell_only:
        raise CswitchError(f"{', '.join(shell_only)} is set only in this shell, so the scheduled task wouldn't see "
                           "it. Set it permanently (setx) or remove it, then try again.")
    cswitch_home().mkdir(parents=True, exist_ok=True)
    today = date.today()
    _register(WAKE_TASK, wake_task_xml(settings, account_count, today))
    _register(CHECK_TASK, check_task_xml(settings, today))


def uninstall() -> bool:
    """Remove the tasks; False if they weren't installed."""
    _windows_only()
    found = installed()
    for name in (CHECK_TASK, WAKE_TASK):
        if _exists(name):
            _schtasks("/delete", "/tn", name, "/f")
    return found


def installed() -> bool:
    return sys.platform == "win32" and _exists(CHECK_TASK)


def wake_times(settings: config.Config, account_count: int) -> list[str]:
    """When the wake task runs: at prime_from, and when spread, at each first-round prime."""
    start, until = config.clock_minutes(settings.prime_from), config.clock_minutes(settings.prime_until)
    step = priming.spread_minutes(account_count)
    count = account_count if settings.prime_mode == "spread" else 1
    return [config.clock_text(m) for m in (start + k * step for k in range(count)) if m <= until]


def sync(settings: config.Config, accounts: list[str], show: Callable[[str], None], install_new: bool) -> None:
    """Make the tasks match the settings: (re)install them while priming is on and there are
    accounts to prime (when not installed yet, only with `install_new`), else remove them."""
    if settings.prime_mode == "off" or not accounts:
        if installed():
            uninstall()
            why = "prime_mode is off" if settings.prime_mode == "off" else "no accounts are left to prime"
            show(f"Scheduled priming removed: {why}. To start it again: cswitch schedule install")
        return
    if not (install_new or installed()):
        return
    install(settings, len(accounts))
    show(f"Scheduled priming: {describe(settings, accounts)}")
    if install_new and wake_timers_allowed() is False:
        show(WAKE_TIMERS_OFF)


def describe(settings: config.Config, accounts: list[str]) -> str:
    minutes = priming.spread_minutes(len(accounts))
    how = f"one account at a time, about every {minutes // 60} h {minutes % 60:02d} min" \
        if settings.prime_mode == "spread" else "all together"
    return (f"{', '.join(accounts)}, {how}, between {settings.prime_from} and {settings.prime_until} on "
            f"{', '.join(settings.prime_days)}; wakes the PC at {', '.join(wake_times(settings, len(accounts)))}.")


def wake_timers_allowed() -> bool | None:
    """Whether the power plan lets timers wake the computer when plugged in; None if unknown.
    Only "enable" (1) counts: "important wake timers only" (2) ignores scheduled tasks."""
    if sys.platform != "win32":
        return None
    done = subprocess.run(["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"],
                          capture_output=True, text=True, encoding="oem", errors="replace")
    values = re.findall(r"0x([0-9a-fA-F]+)", done.stdout)
    return int(values[0], 16) == 1 if done.returncode == 0 and values else None


def check_task_xml(settings: config.Config, today: date) -> str:
    duration = config.clock_minutes(settings.prime_until) - config.clock_minutes(settings.prime_from) + 1
    repetition = REPETITION_XML.format(interval=CHECK_MINUTES, duration=duration)
    return _task_xml(_trigger(settings, settings.prime_from, today, repetition), catch_up=True, wake=False)


def wake_task_xml(settings: config.Config, account_count: int, today: date) -> str:
    triggers = "".join(_trigger(settings, at, today, "") for at in wake_times(settings, account_count))
    return _task_xml(triggers, catch_up=False, wake=True)


def _trigger(settings: config.Config, at: str, today: date, repetition: str) -> str:
    days = "".join(f"<{DAY_NAMES[day]}/>" for day in settings.prime_days)
    return TRIGGER_XML.format(start=f"{today:%Y-%m-%d}T{at}:00", repetition=repetition, days=days)


def _task_xml(triggers: str, catch_up: bool, wake: bool) -> str:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    return TASK_XML.format(triggers=triggers, catch_up=str(catch_up).lower(), wake=str(wake).lower(),
                           user=escape(user), command=escape(str(_background_python())),
                           folder=escape(str(cswitch_home())))


def _background_python() -> Path:
    """pythonw.exe next to the running Python, so no console window pops up."""
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return windowless if windowless.exists() else Path(sys.executable)


def _shell_only(name: str) -> bool:
    """Whether a variable is set here but not (or differently) in the permanent environment."""
    value = os.environ.get(name)
    if value is None:
        return False
    import winreg

    for root, path in ((winreg.HKEY_CURRENT_USER, "Environment"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, path) as key:
                permanent = os.path.expandvars(winreg.QueryValueEx(key, name)[0])
        except OSError:
            continue
        return os.path.normcase(permanent) != os.path.normcase(value)
    return True


def _register(name: str, xml: str) -> None:
    with tempfile.TemporaryDirectory(prefix="cswitch-task-") as temp:
        task_file = Path(temp) / "task.xml"
        task_file.write_text(xml, encoding="utf-16")
        _schtasks("/create", "/tn", name, "/xml", str(task_file), "/f")


def _exists(name: str) -> bool:
    return subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True).returncode == 0


def _schtasks(*args: str) -> None:
    done = subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="oem", errors="replace")
    if done.returncode != 0:
        raise CswitchError(f"schtasks failed: {(done.stderr or done.stdout).strip()}")


def _windows_only() -> None:
    if sys.platform != "win32":
        raise CswitchError("Scheduled priming is only supported on Windows so far.")
