"""User settings: defaults, the commented template, and clear errors for mistakes."""

import tempfile
import unittest
from pathlib import Path

from cswitch import config
from cswitch.errors import CswitchError


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"

    def load(self, text: str) -> config.Config:
        self.path.write_text(text, encoding="utf-8")
        return config.load(self.path)

    def test_missing_file_means_defaults(self):
        self.assertEqual(config.load(self.path), config.Config())

    def test_template_is_valid_and_matches_defaults(self):
        self.assertTrue(config.write_default(self.path))
        self.assertEqual(config.load(self.path), config.Config())
        self.assertFalse(config.write_default(self.path))

    def test_values_are_read(self):
        loaded = self.load('order = ["Team", "pro"]\nexclude = ["Pro"]\nfive_hour_threshold = 80\n'
                           'strategy = "most_remaining"\nask_anthropic = false\n')
        self.assertEqual(loaded.order, ("team", "pro"))
        self.assertEqual(loaded.exclude, ("pro",))
        self.assertEqual(loaded.five_hour_threshold, 80)
        self.assertEqual(loaded.strategy, "most_remaining")
        self.assertFalse(loaded.ask_anthropic)

    def test_set_value_changes_one_line_and_keeps_the_comments(self):
        config.write_default(self.path)
        config.set_value(self.path, "five_hour_threshold", ["85.5"])
        config.set_value(self.path, "order", ["Team,pro", "x"])
        config.set_value(self.path, "notifications", ["off"])
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# Switch away from an account", text)
        self.assertIn("five_hour_threshold = 85.5\n", text)
        loaded = config.load(self.path)
        self.assertEqual((loaded.order, loaded.notifications), (("team", "pro", "x"), False))

    def test_set_value_adds_a_missing_line(self):
        self.path.write_text("# mine\n", encoding="utf-8")
        config.set_value(self.path, "exclude", ["pro"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), '# mine\nexclude = ["pro"]\n')

    def test_set_value_rejects_bad_values_and_leaves_the_file_alone(self):
        config.write_default(self.path)
        before = self.path.read_text(encoding="utf-8")
        for name, words in (("five_hour_threshold", ["150"]), ("five_hour_threshold", ["many"]),
                            ("strategy", ["random"]), ("notifications", ["maybe"]), ("nope", ["1"]),
                            ("seven_day_threshold", ["90", "95"])):
            with self.subTest(name=name, words=words), self.assertRaises(CswitchError):
                config.set_value(self.path, name, words)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_set_value_refuses_a_setting_written_over_several_lines(self):
        self.path.write_text('order = [\n  "a",\n]\n', encoding="utf-8")
        with self.assertRaisesRegex(CswitchError, "several lines"):
            config.set_value(self.path, "order", ["b"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), 'order = [\n  "a",\n]\n')

    def test_set_value_repairs_an_invalid_value(self):
        self.path.write_text("five_hour_threshold = 150\n", encoding="utf-8")
        self.assertEqual(config.set_value(self.path, "five_hour_threshold", ["85"]).five_hour_threshold, 85)

    def test_set_value_into_an_empty_file(self):
        self.path.write_text("", encoding="utf-8")
        config.set_value(self.path, "strategy", ["Most_Remaining"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), 'strategy = "most_remaining"\n')

    def test_account_lists_are_trimmed_and_deduplicated(self):
        config.set_value(self.path, "order", ["team, pro", "TEAM", ","])
        self.assertEqual(config.load(self.path).order, ("team", "pro"))

    def test_file_saved_with_a_byte_order_mark_is_read(self):
        self.path.write_text("five_hour_threshold = 80\n", encoding="utf-8-sig")
        self.assertEqual(config.load(self.path).five_hour_threshold, 80)

    def test_reserve_entries(self):
        loaded = self.load('reserve = ["Other:30", "work:2.5"]\nseven_day_threshold = 95\n')
        self.assertEqual((loaded.weekly_threshold("other"), loaded.weekly_threshold("work")), (70, 95))
        self.assertEqual(loaded.weekly_threshold("unreserved"), 95)
        for bad in ('["other"]', '["other:0"]', '["other:100"]', '["other:lots"]', '[":30"]',
                    '["other :30"]', '["other:30", "other:10"]'):
            with self.subTest(bad=bad), self.assertRaisesRegex(CswitchError, "reserve entr"):
                self.load(f"reserve = {bad}\n")

    def test_prime_schedule(self):
        loaded = self.load('prime_mode = "spread"\nprime_from = "04:40"\nprime_days = ["Sat", "sun"]\n')
        self.assertEqual((loaded.prime_mode, loaded.prime_from, loaded.prime_days), ("spread", "04:40", ("sat", "sun")))
        self.assertEqual(config.clock_minutes("04:40"), 280)

    def test_mistakes_are_reported(self):
        cases = {
            "five_hour_treshold = 80": "unknown setting",
            'five_hour_threshold = "80"': "wrong type",
            "ask_anthropic = 1": "wrong type",
            "order = [1, 2]": "wrong type",
            "five_hour_threshold = 0": "between 1 and 100",
            'strategy = "random"': "strategy must be one of",
            "check_interval_seconds = -1": "0 or more",
            "check_interval_seconds = inf": "0 or more",
            "order = [": "config.toml",
            'prime_mode = "sometimes"': "prime_mode must be one of",
            'prime_from = "4:40"': "24-hour",
            'prime_until = "24:00"': "24-hour",
            'prime_from = "20:00"': "earlier than prime_until",
            'prime_days = ["monday"]': "prime_days are some of",
            "prime_days = []": "prime_days are some of",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(CswitchError, message):
                    self.load(text)


if __name__ == "__main__":
    unittest.main()
