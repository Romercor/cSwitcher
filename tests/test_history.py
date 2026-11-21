"""The optional usage history: a CSV line whenever an account's numbers change, only when
turned on, with the account switched away from still marked live, and never failing."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from cswitch import history
from cswitch.claude.usage import Usage, Window
from cswitch.rotation import engine


def at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


class HistoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.file = self.root / history.HISTORY_FILE

    def turn_on(self) -> None:
        (self.root / "config.toml").write_text("usage_history = true\n", encoding="utf-8")

    def rows(self) -> list[list[str]]:
        return [line.split(",") for line in self.file.read_text(encoding="utf-8").splitlines()]

    def test_changed_numbers_are_written_under_a_header_and_unchanged_ones_are_not(self):
        self.turn_on()
        team = Usage(Window(40, 1_800_000_000), Window(25, None), 1_799_990_000)
        other = Usage(Window(5, None), Window(80, None), 1_799_000_000)
        history.note(self.root, {"other": other}, {"team": team, "other": other}, {"team"})
        self.assertEqual(self.rows(), [history.HEADER.split(","),
                                       [at(1_799_990_000), "team", "1", "40", at(1_800_000_000), "25", ""]])

    def test_a_reading_that_differs_only_within_a_second_is_not_written_again(self):
        self.turn_on()
        first = Usage(Window(40, 1_800_000_000.2), Window(25, None), 1)
        again = Usage(Window(40, 1_800_000_000.7), Window(25, None), 61)
        history.note(self.root, {"team": first}, {"team": again}, {"team"})
        self.assertFalse(self.file.exists())

    def test_nothing_is_written_unless_turned_on(self):
        history.note(self.root, {}, {"team": Usage(Window(40, None), Window(25, None), 1)}, {"team"})
        self.assertFalse(self.file.exists())

    def test_a_reading_that_cant_be_written_is_skipped_without_failing(self):
        self.turn_on()
        history.note(self.root, {}, {"team": Usage(Window(40, 1e20), Window(25, None), 1)}, {"team"})
        self.assertFalse(self.file.exists())

    def test_the_account_switched_away_from_is_still_marked_live(self):
        self.turn_on()
        with engine.locked_state(self.root) as memory:
            memory.last_active = "team"
        with engine.locked_state(self.root) as memory:
            memory.usage["team"] = Usage(Window(92, None), Window(25, None), 1)
            memory.last_active = "pro"
        self.assertEqual([(row[1], row[2]) for row in self.rows()[1:]], [("team", "1")])


if __name__ == "__main__":
    unittest.main()
