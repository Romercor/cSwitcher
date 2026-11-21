"""Hook entry point: switches before a limit, reacts to a usage-limit failure, stays
quiet and safe, and only notifies when every account is used up."""

import json
import threading
import time
from unittest import mock

from cswitch import hooks, log, notify
from cswitch.claude import server
from cswitch.claude.usage import Usage, Window
from cswitch.config import Config
from cswitch.rotation import engine, state
from tests.fakes import FakeHomeTestCase


class HooksTest(FakeHomeTestCase):
    def setUp(self):
        super().setUp()
        self.add_two_accounts()
        self.cswitch_root = self.root / ".cswitcher"
        self.now = time.time()
        state.save(self.cswitch_root / state.STATE_FILE, state.State(last_active="personal", last_switch_at=self.now - 600))
        for name, value in (("usage_of", None), ("organization_of", None)):
            patcher = mock.patch.object(server, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        notify_patcher = mock.patch.object(notify, "notify")
        self.notify = notify_patcher.start()
        self.addCleanup(notify_patcher.stop)

    def hook(self, event: str, payload: object = None, now: float | None = None) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload or {})
        hooks.handle(event, text, self.home, self.store, self.cswitch_root, self.now if now is None else now)

    def memory(self) -> state.State:
        return state.load(self.cswitch_root / state.STATE_FILE)

    def log_text(self) -> str:
        return "\n".join(log.tail(self.cswitch_root, 50))

    def test_switches_before_the_limit(self):
        self.write_claude_cache("uuid-b", 91)
        self.hook("PostToolUse")
        self.assertEqual(self.live_refresh_token(), "rt-a1")
        self.assertIn("switched 'personal' -> 'work'", self.log_text())
        self.notify.assert_not_called()

    def test_stays_below_the_threshold_and_logs_nothing(self):
        self.write_claude_cache("uuid-b", 50)
        self.hook("Stop")
        self.assertEqual(self.live_refresh_token(), "rt-b1")
        self.assertEqual(self.log_text(), "")

    def test_checks_are_throttled(self):
        self.write_claude_cache("uuid-b", 50)
        self.hook("PostToolUse")
        self.write_claude_cache("uuid-b", 95)
        self.hook("PostToolUse", now=self.now + 10)
        self.assertEqual(self.live_refresh_token(), "rt-b1")
        self.hook("PostToolUse", now=self.now + Config().check_interval_seconds + 1)
        self.assertEqual(self.live_refresh_token(), "rt-a1")

    def test_usage_limit_failure_switches_at_once(self):
        self.write_claude_cache("uuid-b", 50)
        self.hook("PostToolUse")
        self.hook("StopFailure", {"error": "rate_limit"}, now=self.now + 1)
        self.assertEqual(self.live_refresh_token(), "rt-a1")
        self.assertIn("'personal' hit a usage limit", self.log_text())
        self.assertEqual(self.memory().usage["personal"].five_hour.used, 100)

    def test_weekly_limit_hit_is_recognised_so_it_never_switches_back_early(self):
        self.write_claude_cache("uuid-b", 30)
        server.usage_of.return_value = Usage(Window(30, None), Window(100, self.now + 86400), self.now)
        self.hook("StopFailure", {"error": "rate_limit"})
        marked = self.memory().usage["personal"]
        self.assertEqual((marked.five_hour.used, marked.seven_day.used), (30, 100))
        self.assertEqual(self.live_refresh_token(), "rt-a1")

    def test_hook_waits_at_most_briefly_for_claudes_locks(self):
        self.write_claude_cache("uuid-b", 95)
        lock_dir = self.home.refresh_locks[0][0]
        lock_dir.mkdir()
        self.addCleanup(lock_dir.rmdir)
        started = time.monotonic()
        with mock.patch.object(hooks, "LOCK_TIMEOUT_SECONDS", 0.3):
            self.hook("PostToolUse")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.live_refresh_token(), "rt-b1")
        self.assertIn("skipped, another cSwitcher or Claude Code step was busy", self.log_text())
        self.assertNotIn("Traceback", self.log_text())

    def test_limit_hit_keeps_the_known_five_hour_reset(self):
        reset = self.now + 1800
        server.usage_of.return_value = Usage(Window(80, reset), Window(10, None), self.now)
        self.hook("StopFailure", {"error": "rate_limit"})
        marked = self.memory().usage["personal"]
        self.assertEqual((marked.five_hour.used, marked.five_hour.resets_at), (100, reset))

    def test_limit_hit_waits_longer_for_a_busy_cswitcher_lock(self):
        lock_dir = self.cswitch_root / "state.lock"
        lock_dir.mkdir()
        threading.Timer(0.5, lock_dir.rmdir).start()
        with mock.patch.object(hooks, "LOCK_TIMEOUT_SECONDS", 0.1):
            self.hook("StopFailure", {"error": "rate_limit"})
        self.assertEqual(self.live_refresh_token(), "rt-a1")

    def test_limit_right_after_a_switch_is_not_charged_to_the_new_account(self):
        memory = self.memory()
        memory.last_switch_at = self.now - 10
        state.save(self.cswitch_root / state.STATE_FILE, memory)
        self.write_claude_cache("uuid-b", 20)
        self.hook("StopFailure", {"error": "rate_limit"})
        self.assertEqual(self.memory().usage["personal"].five_hour.used, 20)
        self.assertEqual(self.live_refresh_token(), "rt-b1")
        self.assertIn("not charged to 'personal'", self.log_text())

    def test_other_failures_are_ignored(self):
        self.write_claude_cache("uuid-b", 95)
        self.hook("StopFailure", {"error": "server_error"})
        self.assertEqual(self.live_refresh_token(), "rt-b1")

    def test_all_used_up_notifies_once(self):
        memory = self.memory()
        memory.usage["work"] = self.full_usage()
        state.save(self.cswitch_root / state.STATE_FILE, memory)
        self.write_claude_cache("uuid-b", 99)
        self.hook("Stop")
        self.hook("Stop", now=self.now + 120)
        self.notify.assert_called_once()
        self.assertIn("every account is used up", self.log_text())

    def test_status_while_used_up_does_not_swallow_the_notification(self):
        memory = self.memory()
        memory.usage["work"] = self.full_usage()
        state.save(self.cswitch_root / state.STATE_FILE, memory)
        self.write_claude_cache("uuid-b", 99)
        self.assertTrue(engine.check(self.home, self.store, self.cswitch_root, self.now).decision.exhausted)
        self.hook("Stop")
        self.notify.assert_called_once()

    def test_status_does_not_reset_the_hook_throttle(self):
        self.write_claude_cache("uuid-b", 50)
        self.hook("PostToolUse")
        engine.check(self.home, self.store, self.cswitch_root, self.now + Config().check_interval_seconds + 1)
        self.write_claude_cache("uuid-b", 95)
        self.hook("PostToolUse", now=self.now + Config().check_interval_seconds + 2)
        self.assertEqual(self.live_refresh_token(), "rt-a1")

    def test_limit_hit_without_a_saved_live_account_is_logged_plainly(self):
        self.log_in("uuid-unknown", "x1")
        self.hook("StopFailure", {"error": "rate_limit"})
        self.assertIn("not charged to the live login", self.log_text())

    def test_notifications_can_be_turned_off(self):
        (self.cswitch_root / "config.toml").write_text("notifications = false\n", encoding="utf-8")
        memory = self.memory()
        memory.usage["work"] = self.full_usage()
        state.save(self.cswitch_root / state.STATE_FILE, memory)
        self.write_claude_cache("uuid-b", 99)
        self.hook("Stop")
        self.notify.assert_not_called()

    def test_errors_are_logged_never_raised(self):
        (self.cswitch_root / "config.toml").write_text("broken = [", encoding="utf-8")
        self.hook("PostToolUse")
        self.hook("StopFailure", "{not json", now=self.now + 1)
        self.assertIn("JSONDecodeError", self.log_text())

    def test_a_lasting_error_is_logged_once(self):
        (self.cswitch_root / "config.toml").write_text("broken = [", encoding="utf-8")
        for second in range(5):
            self.hook("PostToolUse", now=self.now + second)
        self.assertEqual(len(log.tail(self.cswitch_root, 50)), 1)

    def test_unrelated_events_do_nothing(self):
        self.write_claude_cache("uuid-b", 99)
        self.hook("SessionStart")
        self.assertEqual(self.live_refresh_token(), "rt-b1")

    def full_usage(self) -> Usage:
        return Usage(Window(99, self.now + 3600), Window(10, None), self.now)
