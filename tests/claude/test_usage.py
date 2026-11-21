"""Parsing usage from Claude Code's saved data and Anthropic's endpoint."""

import unittest
from datetime import datetime, timezone

from cswitch.claude.usage import Usage, Window, from_claude_cache, parse

RESET = "2026-11-24T14:59:59.912259+00:00"
RESET_TS = datetime(2026, 11, 24, 14, 59, 59, 912259, tzinfo=timezone.utc).timestamp()


class UsageTest(unittest.TestCase):
    def test_parses_both_windows(self):
        usage = parse({"five_hour": {"utilization": 38, "resets_at": RESET},
                       "seven_day": {"utilization": 7.5, "resets_at": None}}, 100.0)
        self.assertEqual(usage, Usage(Window(38.0, RESET_TS), Window(7.5, None), 100.0))

    def test_null_window_is_unused_and_odd_reset_is_unknown(self):
        usage = parse({"five_hour": None, "seven_day": {"utilization": 4, "resets_at": "not a date"}}, 1.0)
        self.assertEqual(usage, Usage(Window(0.0, None), Window(4.0, None), 1.0))

    def test_unreadable_window_makes_usage_unknown_not_zero(self):
        for bad in ({"utilization": None}, {"utilization": "12"}, {"utilization": True}, "x"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse({"five_hour": bad, "seven_day": {"utilization": 1}}, 1.0))

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            Window(None, None)
        with self.assertRaises(ValueError):
            Usage(Window(1, None), Window(1, None), "now")

    def test_no_windows_means_no_usage(self):
        for data in (None, [], {}, {"five_hour": None, "seven_day": None}):
            with self.subTest(data=data):
                self.assertIsNone(parse(data, 1.0))

    def test_window_is_empty_once_its_reset_passed(self):
        window = Window(80.0, 1000.0)
        self.assertEqual(window.used_at(999.0), 80.0)
        self.assertEqual(window.used_at(1000.0), 0.0)
        self.assertEqual(Window(80.0, None).used_at(10**12), 80.0)

    def test_claude_cache(self):
        cache = {"fetchedAtMs": 1795262284174, "accountUuid": "acc-1",
                 "utilization": {"five_hour": {"utilization": 38, "resets_at": RESET}, "seven_day": None}}
        uuid, usage = from_claude_cache(cache)
        self.assertEqual(uuid, "acc-1")
        self.assertEqual(usage.fetched_at, 1795262284.174)
        self.assertIsNone(from_claude_cache({"utilization": {}}))

    def test_round_trip_for_state_file(self):
        usage = Usage(Window(1.0, 2.0), Window(3.0, None), 4.0)
        self.assertEqual(Usage.from_dict(usage.to_dict()), usage)


if __name__ == "__main__":
    unittest.main()
