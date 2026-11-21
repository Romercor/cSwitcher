"""Desktop notifications for things that need the user. Shown by a detached helper
process (PowerShell toast on Windows, osascript on macOS, notify-send on Linux)
so the caller never waits; the text travels in environment variables, never in
the command line, so it can't be misread as code. Failure is silent."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

_WINDOWS_TOAST = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $xml.GetElementsByTagName('text')
$text.Item(0).AppendChild($xml.CreateTextNode($env:CSWITCH_TITLE)) > $null
$text.Item(1).AppendChild($xml.CreateTextNode($env:CSWITCH_MESSAGE)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
_MACOS = 'display notification (system attribute "CSWITCH_MESSAGE") with title (system attribute "CSWITCH_TITLE")'


def notify(title: str, message: str) -> None:
    command = _command()
    if command is None:
        return
    env = os.environ | {"CSWITCH_TITLE": title, "CSWITCH_MESSAGE": message}
    flags = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    try:
        subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        pass


def _command() -> list[str] | None:
    if sys.platform == "win32":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_TOAST]
    if sys.platform == "darwin":
        return ["osascript", "-e", _MACOS]
    if shutil.which("notify-send"):
        return ["sh", "-c", 'notify-send "$CSWITCH_TITLE" "$CSWITCH_MESSAGE"']
    return None
