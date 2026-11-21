"""cSwitcher's hook commands in Claude Code's user settings. Our entries are
recognised by the `-m cswitch` marker in their command, so installing or removing
them never touches the user's own entries.
A symlinked settings.json (dotfiles) is resolved first, so its target is
updated instead of the link being replaced. Claude Code reads hooks when a
session starts, so changes apply to new sessions. The hook timeout is well above
a hook's worst case (about 35 s of lock waits and network on a usage-limit hit)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from cswitch.errors import CswitchError
from cswitch.files.jsonfile import read_json, write_json

_MARKER = " -m cswitch "
_HOOK_TIMEOUT_SECONDS = 60
_SHELL_SAFE = re.compile(r"^[A-Za-z0-9_./:~-]+$")


def install(settings_file: Path, hooks: dict[str, tuple[str, ...]]) -> None:
    """Replace all cSwitcher entries with the given hooks ({event: cswitch args})."""
    settings_file = settings_file.resolve()
    settings = read_json(settings_file) or {}
    _remove_ours(settings)
    if hooks:
        all_hooks = settings.setdefault("hooks", {})
        if not isinstance(all_hooks, dict):
            raise CswitchError(f"Unexpected 'hooks' format in {settings_file}; fix it by hand first.")
        for event, args in hooks.items():
            groups = all_hooks.setdefault(event, [])
            if not isinstance(groups, list):
                raise CswitchError(f"Unexpected format of hooks.{event} in {settings_file}; fix it by hand first.")
            command = {"type": "command", "command": _command(args), "timeout": _HOOK_TIMEOUT_SECONDS}
            groups.append({"hooks": [command]})
    write_json(settings_file, settings)


def installed(settings_file: Path) -> bool:
    """Whether any cSwitcher hook is in the settings."""
    all_hooks = (read_json(settings_file.resolve()) or {}).get("hooks")
    for groups in all_hooks.values() if isinstance(all_hooks, dict) else []:
        for group in groups if isinstance(groups, list) else []:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(entries, list) and any(_is_ours(entry) for entry in entries):
                return True
    return False


def uninstall(settings_file: Path) -> None:
    """Remove every cSwitcher entry; the file is only rewritten if something changed."""
    settings_file = settings_file.resolve()
    settings = read_json(settings_file)
    if settings is not None and _remove_ours(settings):
        write_json(settings_file, settings)


def _remove_ours(settings: dict) -> bool:
    """Drop our entries (including a status line from early versions), plus groups and
    events that only held ours. Unknown shapes stay untouched."""
    changed = False
    if _is_ours(settings.get("statusLine")):
        del settings["statusLine"]
        changed = True
    all_hooks = settings.get("hooks")
    if not isinstance(all_hooks, dict):
        return changed
    for event, groups in list(all_hooks.items()):
        if not isinstance(groups, list):
            continue
        kept = [group for group in (_without_ours(g) for g in groups) if group is not None]
        if kept != groups:
            changed = True
            if kept:
                all_hooks[event] = kept
            else:
                del all_hooks[event]
    if changed and not all_hooks:
        del settings["hooks"]
    return changed


def _without_ours(group: object) -> object | None:
    """The group minus our hooks; None if it held only ours."""
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return group
    kept = [h for h in group["hooks"] if not _is_ours(h)]
    if len(kept) == len(group["hooks"]):
        return group
    return {**group, "hooks": kept} if kept else None


def _command(args: tuple[str, ...]) -> str:
    """A command line that runs cSwitcher with this interpreter in both Git Bash and PowerShell."""
    return f"{_shell_safe_path(Path(sys.executable))}{_MARKER}{' '.join(args)}"


def _shell_safe_path(path: Path) -> str:
    """Unquoted paths are the only form both shells execute. Paths with spaces or
    non-ASCII characters (e.g. a Cyrillic user name) fall back to the 8.3 short name."""
    text = path.as_posix()
    if not _SHELL_SAFE.match(text) and sys.platform == "win32":
        text = Path(_short_windows_path(str(path))).as_posix()
    if not _SHELL_SAFE.match(text):
        raise CswitchError(f"cSwitcher's Python path has spaces or special characters: {path}. "
                           "Install cSwitcher in a folder without them.")
    return text


def _short_windows_path(path: str) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else path


def _is_ours(entry: object) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("command"), str) and _MARKER in entry["command"]
