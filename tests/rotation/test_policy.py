"""Choosing the live account: stay, switch by strategy, or report when one frees up."""

import unittest

from cswitch.claude.usage import Usage, Window
from cswitch.config import Config
from cswitch.rotation.policy import STALE_SECONDS, decide, ordered

NOW = 1000.0
NAMES = ["other", "personal", "work"]


def usage(five: float, seven: float, five_reset: float | None = None, seven_reset: float | None = None) -> Usage:
    return Usage(Window(five, five_reset), Window(seven, seven_reset), NOW)


class PolicyTest(unittest.TestCase):
    def test_order_follows_config_then_name(self):
        self.assertEqual(ordered(NAMES, Config(order=("work", "ghost"))), ["work", "other", "personal"])

    def test_stays_below_thresholds(self):
        decision = decide("work", NAMES, {"work": usage(89, 94)}, Config(), NOW)
        self.assertIsNone(decision.switch_to)

    def test_switches_at_either_threshold(self):
        for full in (usage(90, 0), usage(0, 95)):
            with self.subTest(full=full):
                decision = decide("work", NAMES, {"work": full}, Config(order=("work", "personal")), NOW)
                self.assertEqual(decision.switch_to, "personal")

    def test_priority_skips_full_accounts(self):
        memory = {"work": usage(95, 10), "other": usage(10, 96)}
        decision = decide("work", NAMES, memory, Config(order=("work", "other", "personal")), NOW)
        self.assertEqual(decision.switch_to, "personal")

    def test_most_remaining_picks_largest_headroom(self):
        memory = {"work": usage(95, 10), "other": usage(10, 80), "personal": usage(30, 20)}
        decision = decide("work", NAMES, memory, Config(strategy="most_remaining"), NOW)
        self.assertEqual(decision.switch_to, "personal")

    def test_a_reset_window_makes_an_account_usable_again(self):
        memory = {"work": usage(95, 10), "other": usage(99, 10, five_reset=NOW - 1), "personal": usage(99, 99)}
        decision = decide("work", NAMES, memory, Config(), NOW)
        self.assertEqual(decision.switch_to, "other")

    def test_unsaved_live_login_is_left_alone(self):
        for active in (None, "someone-else"):
            with self.subTest(active=active):
                decision = decide(active, NAMES, {}, Config(order=("work",)), NOW)
                self.assertIsNone(decision.switch_to)
                self.assertFalse(decision.exhausted)

    def test_most_remaining_tie_keeps_priority_order(self):
        memory = {"work": usage(95, 10), "other": usage(20, 20), "personal": usage(20, 20)}
        decision = decide("work", NAMES, memory, Config(strategy="most_remaining", order=("personal",)), NOW)
        self.assertEqual(decision.switch_to, "personal")

    def test_stale_reading_of_live_account_switches_early(self):
        old = Usage(Window(82, None), Window(10, None), NOW - STALE_SECONDS - 1)
        decision = decide("work", NAMES, {"work": old}, Config(), NOW)
        self.assertEqual(decision.switch_to, "other")
        self.assertTrue(decision.stale)
        fresh = Usage(Window(82, None), Window(10, None), NOW)
        self.assertIsNone(decide("work", NAMES, {"work": fresh}, Config(), NOW).switch_to)

    def test_all_full_reports_earliest_time_one_frees_up(self):
        memory = {
            "work": usage(95, 10, five_reset=NOW + 300),
            "other": usage(95, 96, five_reset=NOW + 100, seven_reset=NOW + 900),
            "personal": usage(10, 99),
        }
        decision = decide("work", NAMES, memory, Config(), NOW)
        self.assertIsNone(decision.switch_to)
        self.assertTrue(decision.exhausted)
        self.assertEqual(decision.frees_at, NOW + 300)

    def test_switches_back_to_a_preferred_account_once_it_has_room_again(self):
        memory = {"work": usage(100, 10, five_reset=NOW - 1), "other": usage(20, 30)}
        decision = decide("other", NAMES, memory, Config(order=("work", "personal", "other")), NOW)
        self.assertEqual(decision.switch_to, "work")
        self.assertIn("earlier in the order", decision.reason)

    def test_an_earlier_account_never_seen_counts_as_having_room(self):
        decision = decide("other", NAMES, {"other": usage(20, 30)}, Config(order=("work", "other")), NOW)
        self.assertEqual(decision.switch_to, "work")

    def test_stale_numbers_cannot_make_it_switch_back_and_forth(self):
        stale_work = Usage(Window(82, None), Window(10, None), NOW - STALE_SECONDS - 1)
        config = Config(order=("work", "other"))
        memory = {"work": stale_work, "other": usage(20, 30)}
        self.assertEqual(decide("work", NAMES, memory, config, NOW).switch_to, "other")
        self.assertIsNone(decide("other", NAMES, memory, config, NOW).switch_to)

    def test_no_switching_back_from_an_account_chosen_by_hand(self):
        decision = decide("other", NAMES, {"other": usage(20, 30)}, Config(order=("work", "other")), NOW,
                          chosen_by_hand=True)
        self.assertIsNone(decision.switch_to)
        self.assertIn("chosen by hand", decision.reason)

    def test_an_account_chosen_by_hand_may_use_its_reserve(self):
        config = Config(order=("work", "other"), reserve=("other:30",))
        memory = {"other": usage(20, 75)}
        self.assertIsNone(decide("other", NAMES, memory, config, NOW, chosen_by_hand=True).switch_to)
        self.assertEqual(decide("other", NAMES, memory, config, NOW).switch_to, "work")

    def test_stays_while_the_preferred_accounts_are_still_full(self):
        memory = {"work": usage(100, 10, five_reset=NOW + 600), "personal": usage(10, 99), "other": usage(20, 30)}
        decision = decide("other", NAMES, memory, Config(order=("work", "personal", "other")), NOW)
        self.assertIsNone(decision.switch_to)

    def test_an_unlisted_live_account_gives_way_to_a_listed_one(self):
        decision = decide("other", NAMES, {"other": usage(20, 30)}, Config(order=("work",)), NOW)
        self.assertEqual(decision.switch_to, "work")

    def test_no_switching_back_without_an_order(self):
        memory = {"other": usage(20, 30)}
        for config in (Config(), Config(order=("work", "other"), strategy="most_remaining")):
            with self.subTest(config=config):
                self.assertIsNone(decide("other", NAMES, memory, config, NOW).switch_to)

    def test_an_excluded_preferred_account_is_not_switched_back_to(self):
        config = Config(order=("work", "other"), exclude=("work",))
        self.assertIsNone(decide("other", NAMES, {"other": usage(20, 30)}, config, NOW).switch_to)

    def test_a_reserve_lowers_the_weekly_threshold(self):
        memory = {"work": usage(95, 10), "personal": usage(10, 96), "other": usage(10, 72)}
        config = Config(order=("work", "personal", "other"), reserve=("other:30",))
        decision = decide("work", NAMES, memory, config, NOW)
        self.assertTrue(decision.exhausted)
        self.assertEqual(decide("work", NAMES, {**memory, "other": usage(10, 69)}, config, NOW).switch_to, "other")

    def test_the_live_account_is_left_once_its_reserve_is_reached(self):
        config = Config(reserve=("other:30",))
        self.assertEqual(decide("other", NAMES, {"other": usage(10, 70)}, config, NOW).switch_to, "personal")

    def test_excluded_accounts_are_never_switched_to(self):
        memory = {"work": usage(95, 10)}
        decision = decide("work", NAMES, memory, Config(order=("other",), exclude=("other",)), NOW)
        self.assertEqual(decision.switch_to, "personal")

    def test_an_excluded_live_account_is_still_left_before_its_limit(self):
        decision = decide("work", NAMES, {"work": usage(95, 10)}, Config(exclude=("work",)), NOW)
        self.assertEqual(decision.switch_to, "other")

    def test_only_excluded_accounts_left_means_exhausted_and_only_the_pool_frees_up(self):
        memory = {"work": usage(95, 10, five_reset=NOW + 300), "personal": usage(99, 10, five_reset=NOW + 100)}
        decision = decide("work", NAMES, memory, Config(exclude=("other", "personal")), NOW)
        self.assertTrue(decision.exhausted)
        self.assertEqual(decision.frees_at, NOW + 300)

    def test_all_full_without_known_resets_is_still_exhausted(self):
        memory = {name: usage(99, 99) for name in NAMES}
        decision = decide("work", NAMES, memory, Config(), NOW)
        self.assertTrue(decision.exhausted)
        self.assertIsNone(decision.frees_at)


if __name__ == "__main__":
    unittest.main()
