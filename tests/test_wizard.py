"""`cswitch setup`: saves the accounts (the live one first, then others after a /login,
retrying a name that doesn't work and putting the previous login back when cancelled),
asks for the settings until they are valid or takes them from a file, installs the
hooks and makes the scheduled tasks match the settings."""

from unittest import mock

from cswitch import config, log, schedule, wizard
from cswitch.accounts.adding import Cancelled
from cswitch.claude import settings
from cswitch.errors import CswitchError
from tests.fakes import FakeHomeTestCase


class WizardTest(FakeHomeTestCase):
    def setUp(self):
        super().setUp()
        self.cswitch_root = self.root / ".cswitcher"
        self.config_path = self.cswitch_root / "config.toml"
        self.cswitch_root.mkdir()
        self.shown: list[str] = []
        self.task = {}
        for name, value in (("install", None), ("uninstall", True), ("installed", False), ("wake_timers_allowed", False)):
            patcher = mock.patch.object(schedule, name, return_value=value)
            self.task[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, *answers, config_file=None) -> None:
        """Answer the questions in turn; a callable answer is called (e.g. a /login) and counts as Enter."""
        pending = list(answers)

        def ask(question: str) -> str:
            self.shown.append(question)
            answer = pending.pop(0)
            return answer() if callable(answer) else answer

        wizard.run(self.home, self.store, self.config_path, config_file, ask, self.shown.append)
        self.assertEqual(pending, [], "not every answer was asked for")

    def log_in_first(self) -> str:
        self.log_in("uuid-a", "a1")
        return ""

    def log_in_personal(self) -> str:
        self.log_in("uuid-b", "b1")
        return ""

    def said(self, text: str) -> bool:
        return any(text in line for line in self.shown)

    def test_a_new_computer_is_set_up_in_one_pass(self):
        self.run_with(self.log_in_first, "work", "personal", self.log_in_personal, "",
                      "personal work", "", "personal:20", "spread", "04:40", "", "")
        self.assertEqual(self.store.names(), ["personal", "work"])
        loaded = config.load(self.config_path)
        self.assertEqual((loaded.order, loaded.reserve, loaded.prime_mode, loaded.prime_from),
                         (("personal", "work"), ("personal:20",), "spread", "04:40"))
        self.assertTrue(settings.installed(self.home.settings_file))
        self.assertEqual(self.task["install"].call_args.args[1], 2)
        self.assertTrue(self.said("Scheduled priming: personal, work, one account at a time"))
        self.assertIn(schedule.WAKE_TIMERS_OFF, self.shown)
        self.assertTrue(self.said("Claude Code is logged in as 'personal'"))
        self.assertIn("set reserve = [\"personal:20\"] (setup)", "\n".join(log.tail(self.cswitch_root, 10)))

    def test_current_values_are_shown_as_you_would_type_them(self):
        self.add_two_accounts()
        self.run_with("", "", "", "", "")
        self.assertTrue(self.said("(personal work) [none]: "))
        self.assertTrue(self.said("[off]: "))

    def test_the_live_login_is_asked_for_again_until_it_is_saved(self):
        self.log_in("uuid-a", "a1")
        self.run_with("", "My Work", "work", "", "")
        self.assertEqual(self.store.names(), ["work"])
        self.assertEqual(sum("A short name for it" in line for line in self.shown), 3)

    def test_a_wrong_answer_is_explained_and_asked_again(self):
        self.add_two_accounts()
        self.run_with("", "", "", "", "spread", "4:40", "04:40", "", "")
        self.assertTrue(self.said("24-hour"))
        self.assertEqual(config.load(self.config_path).prime_from, "04:40")

    def test_cancelling_a_login_puts_the_previous_login_back_and_ends_setup(self):
        self.log_in("uuid-a", "a1")
        with self.assertRaisesRegex(Cancelled, "logged in as 'work' again"):
            self.run_with("work", "personal", self._interrupt)
        self.assertEqual(self.live_refresh_token(), "rt-a1")

    @staticmethod
    def _interrupt() -> str:
        raise KeyboardInterrupt

    def test_setup_needs_at_least_one_account(self):
        with self.assertRaisesRegex(CswitchError, "No account is saved"):
            self.run_with("")

    def test_priming_turned_off_removes_installed_tasks(self):
        self.add_two_accounts()
        self.config_path.write_text('prime_mode = "spread"\n', encoding="utf-8")
        self.task["installed"].return_value = True
        self.run_with("", "", "", "", "off")
        self.task["uninstall"].assert_called_once()
        self.task["install"].assert_not_called()

    def test_settings_can_come_from_another_computers_config(self):
        self.add_two_accounts()
        self.config_path.write_text("notifications = false\n", encoding="utf-8")
        copied = self.root / "copied.toml"
        copied.write_text('order = ["work", "personal", "team"]\nprime_mode = "together"\n', encoding="utf-8")
        self.run_with("", config_file=copied)
        self.assertEqual(config.load(self.config_path).order, ("work", "personal", "team"))
        self.assertIn("notifications = false", (self.cswitch_root / "config.toml.bak").read_text(encoding="utf-8"))
        self.assertTrue(self.said("aren't saved here, ignored until added: team"))
        self.assertEqual(self.task["install"].call_args.args[1], 2)

    def test_a_config_file_that_isnt_utf8_is_refused_and_nothing_changes(self):
        self.add_two_accounts()
        copied = self.root / "copied.toml"
        copied.write_text('order = ["work"]\n', encoding="utf-16")
        with self.assertRaisesRegex(CswitchError, "isn't UTF-8"):
            self.run_with(config_file=copied)
        self.assertFalse(self.config_path.exists())

    def test_running_it_again_with_empty_answers_changes_nothing(self):
        self.add_two_accounts()
        self.config_path.write_text('reserve = ["work:30"]\n', encoding="utf-8")
        before = self.config_path.read_text(encoding="utf-8")
        self.run_with("", "", "", "", "")
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.names(), ["personal", "work"])
        self.task["install"].assert_not_called()

    def test_with_one_account_only_priming_is_asked(self):
        self.log_in("uuid-a", "a1")
        self.run_with("work", "", "")
        self.assertTrue(self.said("on a schedule"))
        self.assertFalse(self.said("order of preference"))
