"""The shared check and switch operations: state saved under cSwitcher's lock."""

import time
from unittest import mock

from cswitch.accounts import switcher
from cswitch.claude import server
from cswitch.claude.usage import Usage, Window
from cswitch.errors import CswitchError
from cswitch.rotation import engine, state
from tests.fakes import FakeHomeTestCase


class EngineTest(FakeHomeTestCase):
    def setUp(self):
        super().setUp()
        self.add_two_accounts()
        self.cswitch_root = self.root / ".cswitcher"
        patcher = mock.patch.object(server, "usage_of", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_switch_remembers_when_and_to_whom(self):
        now = time.time()
        self.assertTrue(engine.switch(self.home, self.store, self.cswitch_root, "work", now))
        memory = state.load(self.cswitch_root / state.STATE_FILE)
        self.assertEqual((memory.last_active, memory.last_switch_at), ("work", now))

    def test_check_saves_what_it_observed_and_decides(self):
        engine.switch(self.home, self.store, self.cswitch_root, "work", time.time() - 60)
        self.write_claude_cache("uuid-a", 95)
        result = engine.check(self.home, self.store, self.cswitch_root, time.time())
        self.assertEqual(result.active, "work")
        self.assertEqual(result.decision.switch_to, "personal")
        saved = state.load(self.cswitch_root / state.STATE_FILE)
        self.assertEqual(saved.usage["work"].five_hour.used, 95)

    def test_failed_tick_is_still_remembered_so_it_is_not_retried_at_once(self):
        now = time.time()
        self.write_claude_cache("uuid-b", 95)
        with mock.patch.object(switcher, "switch_to", side_effect=CswitchError("boom")) as switch_to:
            with self.assertRaises(CswitchError):
                engine.tick(self.home, self.store, self.cswitch_root, now)
            self.assertIsNone(engine.tick(self.home, self.store, self.cswitch_root, now + 5))
        switch_to.assert_called_once()
        self.assertEqual(state.load(self.cswitch_root / state.STATE_FILE).usage["personal"].five_hour.used, 95)

    def test_throttled_tick_does_not_wait_for_a_busy_lock(self):
        now = time.time()
        engine.tick(self.home, self.store, self.cswitch_root, now)
        (self.cswitch_root / "state.lock").mkdir()
        started = time.monotonic()
        self.assertIsNone(engine.tick(self.home, self.store, self.cswitch_root, now + 5, timeout=2))
        self.assertLess(time.monotonic() - started, 1)

    def test_check_time_is_saved_before_the_slow_part(self):
        now = time.time()
        seen = []
        def usage_of(_login):
            seen.append(engine.tick(self.home, self.store, self.cswitch_root, now + 1, timeout=0.1))
        with mock.patch.object(server, "usage_of", side_effect=usage_of):
            engine.tick(self.home, self.store, self.cswitch_root, now)
        self.assertEqual(seen, [None])

    def test_after_a_limit_an_account_with_a_little_room_is_used_before_waiting(self):
        now = time.time()
        nearly_full = Usage(Window(93, now + 3600), Window(10, None), now)
        state.save(self.cswitch_root / state.STATE_FILE,
                   state.State({"work": nearly_full}, last_active="personal", last_switch_at=now - 600))
        self.write_claude_cache("uuid-b", 50)
        self.assertTrue(engine.check(self.home, self.store, self.cswitch_root, now).decision.switch_to is None)
        result = engine.tick(self.home, self.store, self.cswitch_root, now, limit_hit=True)
        self.assertEqual((result.switched, result.check.decision.switch_to), (True, "work"))

    def test_the_fallback_after_a_limit_still_keeps_the_reserve(self):
        now = time.time()
        (self.cswitch_root / "config.toml").write_text('reserve = ["work:30"]\n', encoding="utf-8")
        nearly_reserved = Usage(Window(10, now + 3600), Window(75, now + 86400), now)
        state.save(self.cswitch_root / state.STATE_FILE,
                   state.State({"work": nearly_reserved}, last_active="personal", last_switch_at=now - 600))
        self.write_claude_cache("uuid-b", 50)
        result = engine.tick(self.home, self.store, self.cswitch_root, now, limit_hit=True)
        self.assertEqual((result.switched, result.check.decision.exhausted), (False, True))

    def test_an_account_being_primed_is_not_switched_to_or_away_from(self):
        now = time.time()
        path = self.cswitch_root / state.STATE_FILE
        state.save(path, state.State(last_active="personal", priming={"work": now - 5}))
        self.write_claude_cache("uuid-b", 95)
        self.assertIsNone(engine.check(self.home, self.store, self.cswitch_root, now).decision.switch_to)
        with self.assertRaisesRegex(CswitchError, "being primed"):
            engine.switch(self.home, self.store, self.cswitch_root, "work", now)
        state.save(path, state.State(last_active="personal", priming={"personal": now - 5}))
        self.assertIsNone(engine.check(self.home, self.store, self.cswitch_root, now).decision.switch_to)
        with self.assertRaisesRegex(CswitchError, "'personal' is being primed"):
            engine.switch(self.home, self.store, self.cswitch_root, "work", now)
        state.save(path, state.State(last_active="personal", priming={"work": now - engine.PRIME_MARK_SECONDS - 1}))
        self.assertEqual(engine.check(self.home, self.store, self.cswitch_root, now).decision.switch_to, "work")

    def test_an_account_being_primed_isnt_removed_and_doesnt_make_everything_look_used_up(self):
        now = time.time()
        path = self.cswitch_root / state.STATE_FILE
        state.save(path, state.State(last_active="personal", priming={"work": now - 5}))
        with self.assertRaisesRegex(CswitchError, "being primed"):
            engine.remove(self.store, self.cswitch_root, "work", now)
        self.write_claude_cache("uuid-b", 95)
        decision = engine.check(self.home, self.store, self.cswitch_root, now).decision
        self.assertEqual((decision.switch_to, decision.exhausted), (None, False))
        self.assertIn("waiting for work to be primed", decision.reason)

    def test_a_manual_switch_is_not_undone_until_the_account_must_be_left(self):
        now = time.time()
        (self.cswitch_root / "config.toml").write_text('order = ["personal", "work"]\n', encoding="utf-8")
        engine.switch(self.home, self.store, self.cswitch_root, "work", now - 120)
        self.write_claude_cache("uuid-a", 20)
        self.assertFalse(engine.tick(self.home, self.store, self.cswitch_root, now).switched)
        self.write_claude_cache("uuid-a", 95)
        result = engine.tick(self.home, self.store, self.cswitch_root, now + 120)
        self.assertEqual((result.switched, result.check.decision.switch_to), (True, "personal"))
        self.assertIsNone(state.load(self.cswitch_root / state.STATE_FILE).chosen_by_hand)

    def test_reserve_reached_only_when_everything_is_used_up_and_not_chosen_by_hand(self):
        now = time.time()
        (self.cswitch_root / "config.toml").write_text('reserve = ["personal:30"]\n', encoding="utf-8")
        in_reserve = Usage(Window(10, None), Window(75, now + 86400), now)
        path = self.cswitch_root / state.STATE_FILE
        state.save(path, state.State({"personal": in_reserve}, last_active="personal", exhausted=True))
        self.assertTrue(engine.reserve_reached(self.cswitch_root, now))
        state.save(path, state.State({"personal": in_reserve}, last_active="personal", exhausted=False))
        self.assertFalse(engine.reserve_reached(self.cswitch_root, now))
        state.save(path, state.State({"personal": in_reserve}, last_active="personal", exhausted=True,
                                     chosen_by_hand="personal"))
        self.assertFalse(engine.reserve_reached(self.cswitch_root, now))
        engine.forget_hand_choice(self.cswitch_root)
        self.assertTrue(engine.reserve_reached(self.cswitch_root, now))
        (self.cswitch_root / "config.toml").write_text("reserve = [", encoding="utf-8")
        self.assertFalse(engine.reserve_reached(self.cswitch_root, now))

    def test_busy_lock_times_out_without_touching_state(self):
        self.cswitch_root.mkdir(parents=True, exist_ok=True)
        (self.cswitch_root / "state.lock").mkdir()
        with self.assertRaisesRegex(CswitchError, "Timed out"):
            engine.check(self.home, self.store, self.cswitch_root, time.time(), timeout=0.3)
        self.assertFalse((self.cswitch_root / state.STATE_FILE).exists())
