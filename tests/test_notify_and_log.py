"""Notifications go to a detached helper with the text in the environment; the log
appends lines, rotates, and never fails; the computer is kept awake during a run."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cswitch import awake, log, notify
from cswitch.files import append


class NotifyTest(unittest.TestCase):
    def test_text_travels_in_environment_not_command_line(self):
        with mock.patch("subprocess.Popen") as popen:
            notify.notify("Title $(evil)", "Body; rm -rf /")
        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("evil", " ".join(command))
        self.assertEqual((env["CSWITCH_TITLE"], env["CSWITCH_MESSAGE"]), ("Title $(evil)", "Body; rm -rf /"))

    def test_failure_is_silent(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("no powershell")):
            notify.notify("t", "m")


class LogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_lines_are_appended_with_a_timestamp(self):
        log.write(self.root, "first")
        log.write(self.root, "second")
        lines = log.tail(self.root, 10)
        self.assertEqual([line.split("  ", 1)[1] for line in lines], ["first", "second"])

    def test_file_rotates_past_its_size_cap(self):
        path = self.root / "x.txt"
        append.append_line(path, "a" * 50, max_bytes=40)
        append.append_line(path, "b", max_bytes=40)
        self.assertEqual(path.read_text(encoding="utf-8"), "b\n")
        self.assertTrue((self.root / "x.txt.old").exists())

    def test_a_header_starts_every_new_file_also_after_rotating(self):
        path = self.root / "x.csv"
        append.append_line(path, "a" * 50, max_bytes=40, header="h")
        append.append_line(path, "b", max_bytes=40, header="h")
        self.assertEqual(path.read_text(encoding="utf-8"), "h\nb\n")
        self.assertTrue((self.root / "x.csv.old").read_text(encoding="utf-8").startswith("h\n"))

    def test_busy_lock_drops_the_line_instead_of_failing(self):
        (self.root / "x.lock").mkdir()
        with mock.patch.object(append, "LOCK_TIMEOUT_SECONDS", 0.2):
            append.append_line(self.root / "x.txt", "dropped", max_bytes=1000)
        self.assertFalse((self.root / "x.txt").exists())

    def test_tail_without_log(self):
        self.assertEqual(log.tail(self.root, 5), [])


class KeepAwakeTest(unittest.TestCase):
    def test_keeps_windows_awake_only_while_running(self):
        calls = []
        kernel32 = mock.Mock(SetThreadExecutionState=calls.append)
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch("ctypes.windll", mock.Mock(kernel32=kernel32), create=True):
            with awake.keep_awake():
                self.assertEqual(calls, [awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED])
        self.assertEqual(calls[-1], awake.ES_CONTINUOUS)

    def test_does_nothing_elsewhere(self):
        with mock.patch.object(sys, "platform", "linux"), awake.keep_awake():
            pass
