"""Claude Code's login files: reading/writing a login, Claude's locks, path resolution."""

import os
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from cswitch.claude.home import ClaudeHome
from cswitch.claude.login import Login
from cswitch.errors import CswitchError
from cswitch.files import locks
from tests.fakes import FakeHomeTestCase


class LoginFilesTest(FakeHomeTestCase):
    def test_not_logged_in(self):
        self.assertIsNone(self.home.read_login())

    def test_clear_and_write_keep_unrelated_keys(self):
        self.write(self.home.credentials_file, {"mcpOAuth": {"server": "secret"}})
        self.log_in("uuid-a", "a1")
        login = self.home.read_login()

        self.home.clear_login()
        self.assertEqual(self.read(self.home.credentials_file), {"mcpOAuth": {"server": "secret"}})
        self.assertEqual(self.read(self.home.global_config), {"numStartups": 7})

        self.home.write_login(login)
        self.assertEqual(self.home.read_login(), login)
        self.assertEqual(self.read(self.home.credentials_file)["mcpOAuth"], {"server": "secret"})
        self.assertEqual(self.read(self.home.global_config)["numStartups"], 7)

    def test_corrupt_global_config_is_a_clear_error(self):
        self.log_in("uuid-a", "a1")
        self.home.global_config.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(CswitchError, "not valid JSON"):
            self.home.read_login()


class ClaudeLocksTest(FakeHomeTestCase):
    def assert_waits_for(self, lock_dir: Path, action) -> None:
        lock_dir.mkdir()
        self.addCleanup(lock_dir.rmdir)
        with mock.patch.object(locks, "DEFAULT_TIMEOUT_SECONDS", 0.3):
            with self.assertRaisesRegex(CswitchError, "Timed out"):
                action()

    def test_a_login_is_only_written_when_both_write_locks_are_free(self):
        self.log_in("uuid-a", "a1")
        home = replace(self.home, lock_timeout=0.3)
        lock_dir = home.config_lock[0]
        lock_dir.mkdir()
        self.addCleanup(lock_dir.rmdir)
        before = self.home.read_login()
        login = Login({"claudeAiOauth": {"refreshToken": "rt-new"}}, {"accountUuid": "uuid-new"})
        with self.assertRaises(locks.Busy):
            home.write_login(login)
        self.assertEqual(self.home.read_login(), before)

    def test_lock_timeout_bounds_a_wait(self):
        self.log_in("uuid-a", "a1")
        home = replace(self.home, lock_timeout=0.3)
        lock_dir = home.credentials_lock[0]
        lock_dir.mkdir()
        self.addCleanup(lock_dir.rmdir)
        started = time.monotonic()
        with self.assertRaises(locks.Busy):
            home.write_login(home.read_login())
        self.assertLess(time.monotonic() - started, 2)

    def test_operations_wait_for_claudes_refresh_locks(self):
        def operation():
            with self.home.locked():
                pass

        for lock_dir, _ in self.home.refresh_locks:
            with self.subTest(lock=lock_dir.name):
                self.assert_waits_for(lock_dir, operation)

    def test_writes_wait_for_claudes_credentials_and_config_locks(self):
        login = Login({"claudeAiOauth": {}, "organizationUuid": "org"}, {"organizationUuid": "org"})
        for lock_dir, _ in [self.home.credentials_lock, self.home.config_lock]:
            with self.subTest(lock=lock_dir.name):
                self.assert_waits_for(lock_dir, lambda: self.home.write_login(login))


class LocateTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(Path, "home", return_value=Path("/u"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            home = ClaudeHome.locate()
        self.assertEqual(home, ClaudeHome(Path("/u/.claude"), Path("/u/.claude.json"), Path("/u/.claude")))

    def test_config_dir_override(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/c"}, clear=True):
            home = ClaudeHome.locate()
        self.assertEqual(home, ClaudeHome(Path("/c"), Path("/c/.claude.json"), Path("/c")))

    def test_secure_storage_override(self):
        env = {"CLAUDE_CONFIG_DIR": "/c", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/s"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ClaudeHome.locate().credentials_file, Path("/s/.credentials.json"))


if __name__ == "__main__":
    unittest.main()
