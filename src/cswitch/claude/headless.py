"""A long-running headless Claude Code session: `claude -p` with stream-json in and
out, so cSwitcher can send messages and read every event Claude writes. Events are
read on a thread into a queue, so waiting for them never blocks Ctrl-C, and a long
silence or an exited Claude is noticed even if a child process keeps the output
pipe open. On Windows the npm `claude.cmd` shim is bypassed for the real executable,
because a .cmd runs through cmd.exe, which would reinterpret characters like &.
Claude always starts without env overrides that would make it use another login
(an API key) or treat itself as nested in the Claude session cSwitcher was run from.
`ask_once` sends one quick request, without the user's hooks, MCP servers or a saved
session, as the live login or the one in a given Claude config."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from cswitch.errors import CswitchError

POLL_SECONDS = 0.5
EXIT_GRACE_SECONDS = 2
EXIT_WAIT_SECONDS = 10
IDLE_EVENT = "cswitch_idle"
NPM_EXECUTABLE = Path("node_modules/@anthropic-ai/claude-code/bin/claude.exe")
DROPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
               "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
               "CLAUDE_CODE_SSE_PORT")
QUIET_OPTIONS = ["--settings", '{"disableAllHooks": true}', "--strict-mcp-config", "--no-session-persistence"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def claude_command() -> list[str]:
    found = shutil.which("claude")
    if found is None:
        raise CswitchError("Claude Code ('claude') was not found on PATH.")
    if Path(found).suffix.lower() in (".cmd", ".bat"):
        executable = Path(found).parent / NPM_EXECUTABLE
        if not executable.exists():
            raise CswitchError(f"Can't start {found} safely: {executable} is missing.")
        return [str(executable)]
    return [found]


def claude_env(config_dir: Path | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in DROPPED_ENV}
    if config_dir is not None:
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def ask_once(prompt: str, model: str, cwd: Path, config_dir: Path | None, timeout: float) -> None:
    """Raises CswitchError if Claude fails or doesn't answer in time (then it is stopped with
    its child processes). Output goes to a file, not a pipe, so nothing left behind can block."""
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as output:
        try:
            process = subprocess.Popen([*claude_command(), "-p", prompt, "--model", model, *QUIET_OPTIONS],
                                       cwd=cwd, env=claude_env(config_dir), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        except OSError as e:
            raise CswitchError(f"could not start claude: {e}") from None
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            raise CswitchError(f"claude did not answer within {timeout:g} s") from None
        except BaseException:
            _kill(process)
            raise
        if code != 0:
            output.seek(0)
            raise CswitchError(f"claude exited with {code}: {output.read().strip()[-300:]}")


class Session:
    def __init__(self, session_id: str, resume: bool, cwd: Path, claude_args: list[str]):
        """Start Claude in `cwd`, as a new session with this id or resuming it."""
        self._process = subprocess.Popen(
            [*claude_command(), "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--resume" if resume else "--session-id", session_id, *claude_args],
            cwd=cwd, env=claude_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
        self._events: queue.Queue[dict | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def send_user(self, text: str) -> None:
        self.send({"type": "user", "message": {"role": "user", "content": text}})

    def send(self, message: dict) -> None:
        """Write one message; if Claude has exited, events() reports that instead."""
        try:
            if self._process.stdin is not None:
                self._process.stdin.write(json.dumps(message) + "\n")
                self._process.stdin.flush()
        except (OSError, ValueError):
            pass

    def next_event(self, silence_seconds: float) -> dict | None:
        """The next event Claude writes, an IDLE_EVENT after `silence_seconds` without any, or None once it has exited."""
        deadline, exited_at = time.monotonic() + silence_seconds, None
        while True:
            try:
                event = self._events.get(timeout=POLL_SECONDS)
            except queue.Empty:
                now = time.monotonic()
                if self._process.poll() is not None:
                    exited_at = exited_at or now
                    if now - exited_at > EXIT_GRACE_SECONDS:
                        return None
                if now > deadline:
                    return {"type": IDLE_EVENT}
                continue
            if event is None:
                self._events.put(None)
            return event

    def close(self) -> None:
        """Let Claude finish writing (it exits once its input ends), else stop it and its child processes."""
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=EXIT_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill(self._process)
        self._reader.join(timeout=2)
        if not self._reader.is_alive() and self._process.stdout is not None:
            self._process.stdout.close()

    def _read(self) -> None:
        if self._process.stdout is None:
            return
        for line in self._process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self._events.put(event)
        self._events.put(None)


def _kill(process: subprocess.Popen) -> None:
    """Stop a process; on Windows with its child processes, which may hold its output pipe open."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True,
                       creationflags=NO_WINDOW)
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
