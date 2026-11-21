"""Window priming: other accounts are primed in an isolated copy (live login untouched,
refreshed tokens saved back), the live one in place, accounts are marked while being
primed, running windows are skipped, failures are reported without losing tokens, and
scheduled runs prime together or spread out."""

import json
import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from cswitch import priming
from cswitch.claude import headless, server
from cswitch.claude.usage import Usage, Window
from cswitch.errors import CswitchError
from cswitch.rotation import state
from tests.fakes import FakeHomeTestCase

FAKE = str(Path(__file__).with_name("fake_prime_claude.py"))


class PrimingCase(FakeHomeTestCase):
    def setUp(self):
        super().setUp()
        self.add_two_accounts()
        self.cswitch_root = self.root / ".cswitcher"
        self.state_file = self.cswitch_root / state.STATE_FILE
        self.record = self.root / "calls.jsonl"
        self.now = time.time()
        self.resets = self.now + 5 * 3600
        self.patch(headless, "claude_command", return_value=[sys.executable, FAKE])
        self.usage_of = self.patch(server, "usage_of", return_value=Usage(Window(1, self.resets), Window(20, None),
                                                                          self.now))
        env = mock.patch.dict(os.environ, {"FAKE_CLAUDE_RECORD": str(self.record),
                                           "FAKE_CLAUDE_STATE": str(self.state_file),
                                           "CLAUDE_CONFIG_DIR": str(self.home.config_dir),
                                           "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(self.home.storage_dir),
                                           "ANTHROPIC_API_KEY": "not-a-key"})
        env.start()
        self.addCleanup(env.stop)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def remember(self, **changes) -> None:
        memory = state.load(self.state_file)
        for name, value in changes.items():
            getattr(memory, name).update(value)
        state.save(self.state_file, memory)


class PrimingTest(PrimingCase):
    def prime(self, *accounts: str) -> list[priming.PrimeResult]:
        return list(priming.prime(self.home, self.store, self.cswitch_root, self.now, list(accounts) or None))

    def test_another_account_is_primed_in_an_isolated_copy(self):
        [result] = self.prime("work")
        self.assertEqual((result.outcome, result.detail),
                         (priming.Outcome.PRIMED, f"resets {datetime.fromtimestamp(self.resets):%H:%M}"))
        [call] = self.calls()
        self.assertIn("haiku", call["args"])
        self.assertNotEqual(call["config_dir"], str(self.home.config_dir))
        self.assertEqual(self.live_refresh_token(), "rt-b1")
        self.assertEqual(self.store.load("work").refresh_token, "rt-a1-refreshed")
        self.assertEqual(state.load(self.state_file).usage["work"].five_hour.resets_at, self.resets)

    def test_the_live_account_is_primed_in_place(self):
        [result] = self.prime("personal")
        self.assertEqual(result.outcome, priming.Outcome.PRIMED)
        self.assertEqual(self.calls()[0]["config_dir"], str(self.home.config_dir))
        self.assertEqual(self.live_refresh_token(), "rt-b1-refreshed")
        self.assertEqual(self.store.load("work").refresh_token, "rt-a1")

    def test_an_account_is_marked_while_claude_runs_and_unmarked_after(self):
        self.prime("work")
        self.assertEqual(self.calls()[0]["marked"], ["work"])
        self.assertEqual(state.load(self.state_file).priming, {})

    def test_claude_runs_without_the_users_hooks_mcp_servers_or_login_overrides(self):
        self.prime("work")
        self.prime("personal")
        isolated, live = self.calls()
        for call in (isolated, live):
            self.assertIn("--strict-mcp-config", call["args"])
            self.assertIn('{"disableAllHooks": true}', call["args"])
        self.assertEqual(isolated["overrides"], [])
        self.assertEqual(live["overrides"], ["CLAUDE_SECURESTORAGE_CONFIG_DIR"])

    def test_by_default_every_account_not_excluded_is_primed(self):
        (self.cswitch_root / "config.toml").write_text('exclude = ["work"]\n', encoding="utf-8")
        self.assertEqual([result.account for result in self.prime()], ["personal"])

    def test_an_account_whose_window_is_running_is_skipped_until_shortly_after_its_reset(self):
        running = Usage(Window(30, self.now + 600), Window(20, None), self.now - 60)
        state.save(self.state_file, state.State({"work": running}, last_active="personal"))
        self.assertEqual(self.prime("work")[0].outcome, priming.Outcome.SKIPPED)
        just_reset = Usage(Window(30, self.now - 60), Window(20, None), self.now - 60)
        state.save(self.state_file, state.State({"work": just_reset}, last_active="personal"))
        self.assertEqual(self.prime("work")[0].outcome, priming.Outcome.SKIPPED)
        self.assertFalse(self.record.exists())

    def test_a_reset_reported_after_the_prime_wins_over_the_guess(self):
        self.remember(primed_at={"work": self.now - 3600})
        self.assertEqual(self.prime("work")[0].outcome, priming.Outcome.SKIPPED)
        reported = Usage(Window(5, self.now - 600), Window(20, None), self.now - 1800)
        self.remember(usage={"work": reported})
        self.assertEqual(self.prime("work")[0].outcome, priming.Outcome.PRIMED)

    def test_without_asking_anthropic_the_window_is_known_from_when_it_was_primed(self):
        (self.cswitch_root / "config.toml").write_text("ask_anthropic = false\n", encoding="utf-8")
        [result] = self.prime("work")
        self.assertEqual((result.outcome, result.detail), (priming.Outcome.PRIMED, "window started"))
        self.usage_of.assert_not_called()
        self.assertEqual(self.prime("work")[0].outcome, priming.Outcome.SKIPPED)

    def test_an_account_being_primed_elsewhere_is_skipped(self):
        self.remember(priming={"work": self.now - 5})
        self.assertEqual(self.prime("work")[0].outcome, priming.Outcome.SKIPPED)

    def test_a_failure_is_reported_unmarked_and_refreshed_tokens_are_still_saved(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_EXIT": "3"}):
            [result] = self.prime("work")
        self.assertEqual(result.outcome, priming.Outcome.FAILED)
        self.assertIn("exited with 3", result.detail)
        self.assertEqual(self.store.load("work").refresh_token, "rt-a1-refreshed")
        memory = state.load(self.state_file)
        self.assertEqual((memory.priming, memory.primed_at), ({}, {}))

    def test_an_interrupted_prime_is_unmarked(self):
        with mock.patch.object(priming, "_ask_isolated", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.prime("work")
        self.assertEqual(state.load(self.state_file).priming, {})

    def test_a_claude_that_doesnt_answer_is_stopped_and_its_refreshed_tokens_are_saved(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_SLEEP": "30"}), \
                mock.patch.object(priming, "CLAUDE_TIMEOUT_SECONDS", 3):
            [result] = self.prime("work")
        self.assertEqual((result.outcome, result.detail), (priming.Outcome.FAILED, "claude did not answer within 3 s"))
        self.assertEqual(self.store.load("work").refresh_token, "rt-a1-refreshed")

    def test_a_busy_cswitcher_fails_that_account_and_goes_on(self):
        self.cswitch_root.mkdir(exist_ok=True)
        (self.cswitch_root / "state.lock").mkdir()
        with mock.patch.object(priming, "LOCK_TIMEOUT_SECONDS", 0.2):
            results = self.prime("work", "personal")
        self.assertEqual([r.outcome for r in results], [priming.Outcome.FAILED] * 2)
        self.assertIn("busy", results[0].detail)

    def test_refreshed_tokens_that_cant_be_saved_are_kept_as_a_backup(self):
        with mock.patch.object(self.store, "save", side_effect=OSError("disk full")):
            [result] = self.prime("work")
        self.assertEqual((result.outcome, result.detail), (priming.Outcome.FAILED, "disk full"))
        self.assertEqual(self.backed_up_refresh_tokens(), ["rt-a1-refreshed"])

    def test_every_prime_is_logged(self):
        self.prime("work", "personal")
        log_text = (self.cswitch_root / "log.txt").read_text(encoding="utf-8")
        self.assertIn("prime: 'work' primed", log_text)
        self.assertIn("prime: 'personal' primed", log_text)


class ScheduledTest(PrimingCase):
    """Scheduled runs on Monday 23 Nov 2026 at 09:00."""

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 23, 9, 0).timestamp()

    def run_with(self, *config_lines: str) -> list[str]:
        (self.cswitch_root / "config.toml").write_text("\n".join(config_lines) + "\n", encoding="utf-8")
        return [r.account for r in priming.run_scheduled(self.home, self.store, self.cswitch_root, self.now)]

    def started(self, account: str, minutes_ago: float) -> None:
        self.remember(primed_at={account: self.now - minutes_ago * 60})

    def test_together_primes_every_account_whose_window_is_over(self):
        self.started("work", 60)
        self.assertEqual(self.run_with('prime_mode = "together"'), ["personal"])
        self.assertEqual(self.run_with('prime_mode = "together"'), [])

    def test_spread_primes_only_the_first_account_in_order(self):
        self.assertEqual(self.run_with('prime_mode = "spread"', 'order = ["work"]'), ["work"])

    def test_spread_waits_its_share_of_a_window_after_the_latest_start(self):
        due = priming.spread_minutes(2) - priming.SPREAD_TOLERANCE_SECONDS / 60
        self.started("work", due - 1)
        self.assertEqual(self.run_with('prime_mode = "spread"'), [])
        self.started("work", due)
        self.assertEqual(self.run_with('prime_mode = "spread"'), ["personal"])

    def test_spread_stops_when_another_run_is_priming_the_first_account(self):
        self.remember(priming={"personal": self.now - 5})
        self.assertEqual(self.run_with('prime_mode = "spread"'), [])
        self.assertFalse(self.record.exists())

    def test_spread_moves_on_past_an_account_that_fails(self):
        with mock.patch.object(priming, "_ask_live", side_effect=CswitchError("broken")):
            self.assertEqual(self.run_with('prime_mode = "spread"'), ["personal", "work"])
        self.assertEqual(list(state.load(self.state_file).primed_at), ["work"])

    def test_an_account_whose_week_is_used_up_is_not_primed(self):
        self.remember(usage={"personal": Usage(Window(0, None), Window(100, self.now + 86400), self.now)})
        self.assertEqual(self.run_with('prime_mode = "together"'), ["work"])

    def test_nothing_happens_outside_the_schedule(self):
        self.assertEqual(self.run_with('prime_mode = "off"'), [])
        self.assertEqual(self.run_with('prime_mode = "together"', 'prime_from = "10:00"'), [])
        self.assertEqual(self.run_with('prime_mode = "together"', 'prime_days = ["tue"]'), [])
        self.assertEqual(self.run_with('prime_mode = "together"', 'prime_from = "06:00"', 'prime_until = "08:59"'), [])
        self.assertFalse(self.record.exists())
        self.assertEqual(len(self.run_with('prime_mode = "together"', 'prime_from = "06:00"', 'prime_until = "09:00"')), 2)


class SpreadTest(unittest.TestCase):
    def test_a_window_and_a_margin_are_shared_out_between_the_accounts(self):
        self.assertEqual([priming.spread_minutes(n) for n in (1, 2, 3)], [305, 153, 102])
