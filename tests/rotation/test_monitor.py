"""Keeping the live account's remembered usage current, and the state file."""

import time
from unittest import mock

from cswitch.claude import server
from cswitch.claude.usage import Usage, Window
from cswitch.config import Config
from cswitch.errors import CswitchError
from cswitch.rotation import monitor, state
from tests.fakes import FakeHomeTestCase


class MonitorTest(FakeHomeTestCase):
    def setUp(self):
        super().setUp()
        self.add_two_accounts()
        self.now = time.time()
        self.memory = state.State(last_active="personal")
        patcher = mock.patch.object(server, "usage_of", return_value=None)
        self.usage_of = patcher.start()
        self.addCleanup(patcher.stop)

    def observe(self, ask_anthropic: bool = True) -> str | None:
        return monitor.observe(self.home, self.store, self.memory, Config(ask_anthropic=ask_anthropic), self.now)

    def test_fresh_claude_cache_is_used_without_asking_anthropic(self):
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - 10)
        self.assertEqual(self.observe(), "personal")
        self.assertEqual(self.memory.usage["personal"].five_hour.used, 40)
        self.usage_of.assert_not_called()

    def test_stale_cache_asks_anthropic(self):
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - monitor.USAGE_MAX_AGE_SECONDS - 1)
        self.usage_of.return_value = Usage(Window(55, None), Window(6, None), self.now)
        self.observe()
        self.assertEqual(self.memory.usage["personal"].five_hour.used, 55)

    def test_stale_cache_is_kept_when_anthropic_has_no_answer(self):
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - monitor.USAGE_MAX_AGE_SECONDS - 1)
        self.observe()
        self.usage_of.assert_called_once()
        self.assertEqual(self.memory.usage["personal"].five_hour.used, 40)

    def test_anthropic_is_not_asked_when_not_allowed(self):
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - monitor.USAGE_MAX_AGE_SECONDS - 1)
        self.observe(ask_anthropic=False)
        self.usage_of.assert_not_called()

    def test_cache_about_another_account_is_ignored(self):
        self.write_claude_cache("uuid-a", 40, fetched_at=self.now - 10)
        self.observe(ask_anthropic=False)
        self.assertEqual(self.memory.usage, {})

    def test_cache_from_before_the_last_switch_is_ignored(self):
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - 10)
        self.memory.last_switch_at = self.now - 5
        self.observe(ask_anthropic=False)
        self.assertEqual(self.memory.usage, {})

    def test_login_changed_outside_cswitcher_counts_as_a_switch(self):
        self.memory.last_active = "work"
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - 10)
        self.observe(ask_anthropic=False)
        self.assertEqual((self.memory.last_active, self.memory.last_switch_at), ("personal", self.now))
        self.assertEqual(self.memory.usage, {})

    def test_the_first_account_ever_seen_is_not_a_switch(self):
        self.memory.last_active = None
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - 10)
        self.observe(ask_anthropic=False)
        self.assertEqual((self.memory.last_active, self.memory.last_switch_at), ("personal", 0.0))
        self.assertEqual(self.memory.usage["personal"].five_hour.used, 40)

    def test_older_numbers_never_replace_newer_ones(self):
        self.memory.usage["personal"] = Usage(Window(70, None), Window(9, None), self.now)
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - 30)
        self.observe(ask_anthropic=False)
        self.assertEqual(self.memory.usage["personal"].five_hour.used, 70)

    def test_limit_hit_marks_the_five_hour_window_full(self):
        self.memory.usage["personal"] = Usage(Window(60, None), Window(30, None), self.now - 300)
        monitor.mark_limit_hit(self.memory, "personal", self.now)
        marked = self.memory.usage["personal"]
        self.assertEqual((marked.five_hour.used, marked.five_hour.resets_at), (100, self.now + monitor.FIVE_HOURS))
        self.assertEqual(marked.seven_day.used, 30)

    def test_limit_hit_keeps_a_fresh_reading_that_already_shows_the_full_window(self):
        fresh = Usage(Window(40, None), Window(100, self.now + 9999), self.now - 5)
        self.memory.usage["personal"] = fresh
        monitor.mark_limit_hit(self.memory, "personal", self.now)
        self.assertEqual(self.memory.usage["personal"], fresh)

    def test_fetch_asks_anthropic_even_when_the_cache_is_fresh(self):
        self.write_claude_cache("uuid-b", 40, fetched_at=self.now - 10)
        self.usage_of.return_value = Usage(Window(40, None), Window(100, self.now + 9999), self.now)
        monitor.observe(self.home, self.store, self.memory, Config(), self.now, fetch=True)
        self.assertEqual(self.memory.usage["personal"].seven_day.used, 100)

    def test_limit_hit_ignores_a_full_window_that_has_since_reset(self):
        self.memory.usage["personal"] = Usage(Window(100, self.now - 1), Window(30, None), self.now - 3600)
        monitor.mark_limit_hit(self.memory, "personal", self.now)
        self.assertEqual(self.memory.usage["personal"].five_hour.resets_at, self.now + monitor.FIVE_HOURS)

    def test_unknown_live_login(self):
        self.log_in("uuid-unknown", "x1")
        self.assertIsNone(self.observe())


class StateFileTest(FakeHomeTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "state.json"

    def test_round_trip(self):
        memory = state.State({"team": Usage(Window(1, 2), Window(3, None), 4)}, "team", 5.0, 6.0, True, "team",
                             {"work": 7.0}, {"team": 8.0})
        state.save(self.path, memory)
        self.assertEqual(state.load(self.path), memory)

    def test_older_files_without_newer_fields_load(self):
        self.write(self.path, {"usage": {}, "last_active": "team"})
        self.assertEqual(state.load(self.path), state.State(last_active="team"))

    def test_missing_file_starts_fresh(self):
        self.assertEqual(state.load(self.path), state.State())

    def test_unreadable_file_is_an_error_not_an_empty_state(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(CswitchError):
            state.load(self.path)

    def test_malformed_entries_are_skipped_and_the_rest_kept(self):
        good = Usage(Window(1, None), Window(2, None), 3).to_dict()
        bad = {"five_hour": {"used": None, "resets_at": None}, "seven_day": {"used": 1, "resets_at": None},
               "fetched_at": 1}
        self.write(self.path, {"usage": {"team": good, "pro": bad, "x": {"oops": 1}}, "last_switch_at": 9})
        loaded = state.load(self.path)
        self.assertEqual(set(loaded.usage), {"team"})
        self.assertEqual(loaded.last_switch_at, 9)
