"""The scheduled tasks: when they run, how often, whether they wake the computer, and what they start."""

import os
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from unittest import mock

from cswitch import schedule
from cswitch.config import Config
from cswitch.errors import CswitchError

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
TODAY = date(2026, 11, 23)


def text(task: ET.Element, path: str) -> str | None:
    return task.findtext(path, namespaces=NS)


class TaskXmlTest(unittest.TestCase):
    def test_the_check_task_runs_every_few_minutes_on_the_chosen_days_without_waking(self):
        settings = Config(prime_mode="spread", prime_from="04:40", prime_until="20:00", prime_days=("mon", "sat"))
        task = ET.fromstring(schedule.check_task_xml(settings, TODAY))
        self.assertEqual(text(task, ".//t:StartBoundary"), "2026-11-23T04:40:00")
        self.assertEqual(text(task, ".//t:Repetition/t:Interval"), f"PT{schedule.CHECK_MINUTES}M")
        self.assertEqual(text(task, ".//t:Repetition/t:Duration"), "PT921M")
        self.assertEqual([day.tag.split("}")[1] for day in task.find(".//t:DaysOfWeek", NS)], ["Monday", "Saturday"])
        self.assertEqual((text(task, ".//t:WakeToRun"), text(task, ".//t:StartWhenAvailable")), ("false", "true"))
        self.assertEqual(text(task, ".//t:Arguments"), "-I -m cswitch prime --scheduled")

    def test_the_wake_task_wakes_for_the_first_prime_of_each_account_when_spread(self):
        settings = Config(prime_mode="spread", prime_from="04:40", prime_until="20:00")
        task = ET.fromstring(schedule.wake_task_xml(settings, 3, TODAY))
        starts = [boundary.text for boundary in task.iterfind(".//t:StartBoundary", NS)]
        self.assertEqual(starts, ["2026-11-23T04:40:00", "2026-11-23T06:22:00", "2026-11-23T08:04:00"])
        self.assertEqual((text(task, ".//t:WakeToRun"), text(task, ".//t:StartWhenAvailable")), ("true", "false"))
        self.assertIsNone(task.find(".//t:Repetition", NS))

    def test_wake_times(self):
        self.assertEqual(schedule.wake_times(Config(prime_mode="together", prime_from="08:00"), 3), ["08:00"])
        short = Config(prime_mode="spread", prime_from="08:00", prime_until="09:00")
        self.assertEqual(schedule.wake_times(short, 3), ["08:00"])

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_a_folder_override_set_only_in_this_shell_is_refused(self):
        with mock.patch.object(schedule, "_shell_only", side_effect=lambda name: name == "CSWITCH_HOME"), \
                mock.patch.object(schedule, "_register") as register:
            with self.assertRaisesRegex(CswitchError, "CSWITCH_HOME is set only in this shell"):
                schedule.install(Config(prime_mode="together"), 2)
        register.assert_not_called()


if __name__ == "__main__":
    unittest.main()
