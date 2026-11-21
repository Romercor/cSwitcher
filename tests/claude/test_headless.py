"""The headless Claude session (against a stand-in program) and finding the claude executable."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cswitch.claude import headless
from cswitch.errors import CswitchError

FAKE_CLAUDE = str(Path(__file__).with_name("fake_claude.py"))


class SessionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)
        patcher = mock.patch.object(headless, "claude_command", return_value=[sys.executable, FAKE_CLAUDE])
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, resume: bool) -> headless.Session:
        session = headless.Session("id-1", resume, self.cwd, ["--permission-mode", "acceptEdits"])
        self.addCleanup(session.close)
        return session

    def test_messages_go_in_and_events_come_out(self):
        session = self.start(resume=False)
        self.assertEqual(session.next_event(60)["args"][-4:], ["--session-id", "id-1", "--permission-mode", "acceptEdits"])
        session.send_user('a & b "quoted"')
        replies = [session.next_event(60), session.next_event(60)]
        self.assertEqual([e["type"] for e in replies], ["assistant", "result"])
        self.assertEqual(replies[1]["result"], 'a & b "quoted"')

    def test_resume_uses_the_resume_flag(self):
        self.assertIn("--resume", self.start(resume=True).next_event(60)["args"])

    def test_silence_is_reported_as_an_idle_event(self):
        session = self.start(resume=False)
        self.assertEqual([session.next_event(0.6)["type"], session.next_event(0.6)["type"]],
                         ["system", headless.IDLE_EVENT])

    def test_events_end_when_claude_exits(self):
        session = self.start(resume=False)
        session.close()
        events = iter(lambda: session.next_event(60), None)
        self.assertNotIn("assistant", [event["type"] for event in events])
        session.send_user("ignored after exit")


class ClaudeEnvTest(unittest.TestCase):
    def test_login_overrides_are_dropped_and_a_config_dir_isolates_the_login(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x", "CLAUDECODE": "1",
                                            "CLAUDE_SECURESTORAGE_CONFIG_DIR": "s", "PATH": "p"}):
            live, isolated = headless.claude_env(), headless.claude_env(Path("copy"))
        self.assertEqual((live.get("ANTHROPIC_API_KEY"), live.get("CLAUDECODE"), live["PATH"]), (None, None, "p"))
        self.assertEqual(live["CLAUDE_SECURESTORAGE_CONFIG_DIR"], "s")
        self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", isolated)
        self.assertEqual(isolated["CLAUDE_CONFIG_DIR"], "copy")


class ClaudeCommandTest(unittest.TestCase):
    def test_npm_shim_is_bypassed_for_the_real_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "claude.cmd"
            executable = Path(tmp) / headless.NPM_EXECUTABLE
            executable.parent.mkdir(parents=True)
            executable.touch()
            with mock.patch("shutil.which", return_value=str(shim)):
                self.assertEqual(headless.claude_command(), [str(executable)])
                executable.unlink()
                with self.assertRaisesRegex(CswitchError, "safely"):
                    headless.claude_command()

    def test_a_real_executable_is_used_as_found(self):
        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            self.assertEqual(headless.claude_command(), ["/usr/local/bin/claude"])

    def test_missing_claude_is_a_clear_error(self):
        with mock.patch("shutil.which", return_value=None), self.assertRaisesRegex(CswitchError, "not found"):
            headless.claude_command()


if __name__ == "__main__":
    unittest.main()
