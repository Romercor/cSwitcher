"""Lock directories compatible with Claude Code's proper-lockfile locks."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from cswitch.errors import CswitchError
from cswitch.files import locks


class LocksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lock = self.root / "a.lock"

    def test_held_inside_released_after(self):
        with locks.hold([(self.lock, 10)]):
            self.assertTrue(self.lock.is_dir())
        self.assertFalse(self.lock.exists())

    def test_times_out_on_a_fresh_lock(self):
        self.lock.mkdir()
        with self.assertRaisesRegex(locks.Busy, "Timed out"):
            with locks.hold([(self.lock, 10)], timeout=0.3):
                pass
        self.assertTrue(self.lock.is_dir())

    def test_breaks_a_stale_lock(self):
        self.lock.mkdir()
        old = time.time() - 60
        os.utime(self.lock, (old, old))
        with locks.hold([(self.lock, 10)]):
            pass
        self.assertEqual(list(self.root.iterdir()), [])

    def test_releases_earlier_locks_when_a_later_one_times_out(self):
        second = self.root / "b.lock"
        second.mkdir()
        with self.assertRaises(CswitchError):
            with locks.hold([(self.lock, 10), (second, 10)], timeout=0.3):
                pass
        self.assertFalse(self.lock.exists())

    def test_missing_folder_is_a_clear_error(self):
        with self.assertRaisesRegex(CswitchError, "Folder not found"):
            with locks.hold([(self.root / "nope" / "x.lock", 10)]):
                pass


if __name__ == "__main__":
    unittest.main()
