"""The `cswitch` command line: output, exit codes, and the interactive --new flow."""

import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from cswitch import cli, config, log, schedule
from cswitch.claude import server
from cswitch.rotation import state
from tests.fakes import FakeHomeTestCase


class CliTest(FakeHomeTestCase):
    def setUp(self):
        super().setUp()
        self.task = {name: self.patch(schedule, name) for name in ("install", "uninstall", "installed")}
        self.task["installed"].return_value = False
        self.patch(schedule, "wake_timers_allowed", return_value=False)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        """Run `cswitch` fully isolated: fake homes, own CSWITCH_HOME, no network."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.ClaudeHome, "locate", return_value=self.home), \
             mock.patch.object(cli.AccountStore, "locate", return_value=self.store), \
             mock.patch.dict(os.environ, {"CSWITCH_HOME": str(self.root / ".cswitcher")}), \
             mock.patch.object(server, "organization_of", return_value=None), \
             mock.patch.object(server, "usage_of", return_value=None), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def config(self) -> config.Config:
        return config.load(self.root / ".cswitcher" / "config.toml")

    def test_status_shows_usage_and_decision(self):
        self.add_two_accounts()
        state.save(self.root / ".cswitcher" / state.STATE_FILE,
                   state.State(last_active="personal", last_switch_at=time.time() - 60))
        self.write_claude_cache("uuid-b", 92)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("* personal         5h 92%", out)
        self.assertIn("work             no usage seen yet", out)
        self.assertIn("Would switch to 'work'", out)

    def test_status_marks_excluded_accounts(self):
        self.add_two_accounts()
        (self.root / ".cswitcher").mkdir(exist_ok=True)
        (self.root / ".cswitcher" / "config.toml").write_text('exclude = ["work"]\n', encoding="utf-8")
        _, out, _ = self.run_cli("status")
        self.assertIn("work             no usage seen yet  (excluded)", out)

    def test_status_without_accounts_writes_nothing(self):
        _, out, _ = self.run_cli("status")
        self.assertIn("No saved accounts", out)
        self.assertFalse((self.root / ".cswitcher" / state.STATE_FILE).exists())

    def test_switch_records_when_it_happened(self):
        self.add_two_accounts()
        before = time.time()
        self.run_cli("switch", "work")
        memory = state.load(self.root / ".cswitcher" / state.STATE_FILE)
        self.assertGreaterEqual(memory.last_switch_at, before)
        self.assertEqual(memory.last_active, "work")

    def test_install_and_uninstall(self):
        self.write(self.home.settings_file, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}})
        code, out, _ = self.run_cli("install")
        self.assertEqual(code, 0)
        hooks = self.read(self.home.settings_file)["hooks"]
        self.assertEqual(sorted(hooks), ["PostToolUse", "Stop", "StopFailure"])
        self.assertIn("-m cswitch hook Stop", json.dumps(hooks["Stop"]))
        self.assertTrue((self.root / ".cswitcher" / "config.toml").exists())
        self.run_cli("uninstall")
        self.assertEqual(self.read(self.home.settings_file),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}})

    def test_hook_always_exits_zero_and_prints_nothing(self):
        for argv in (["hook", "Stop"], ["hook"], ["hook", "Nonsense", "--bad"]):
            with self.subTest(argv=argv), mock.patch.object(cli, "_read_stdin", return_value="{broken"):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out, err), (0, "", ""))

    def test_exclude_and_include_from_the_terminal(self):
        self.add_two_accounts()
        _, out, _ = self.run_cli("exclude", "Work")
        self.assertIn("'work' is excluded from automatic switching", out)
        self.assertEqual(self.config().exclude, ("work",))
        self.run_cli("include", "work")
        self.assertEqual(self.config().exclude, ())
        code, _, err = self.run_cli("exclude", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("No saved account", err)
        _, out, _ = self.run_cli("include", "ghost")
        self.assertIn("wasn't excluded", out)

    def test_status_marks_a_reserve(self):
        self.add_two_accounts()
        self.run_cli("set", "reserve", "work:20")
        _, out, _ = self.run_cli("status")
        self.assertIn("(keeps 20% weekly)", out)

    def test_set_reserve_notes_unknown_accounts(self):
        self.add_two_accounts()
        _, out, _ = self.run_cli("set", "reserve", "ghost:20")
        self.assertIn("ignored until added: ghost", out)

    def test_set_notes_names_that_are_not_saved_accounts(self):
        self.add_two_accounts()
        _, out, _ = self.run_cli("set", "order", "work, persnal")
        self.assertEqual(self.config().order, ("work", "persnal"))
        self.assertIn("not saved accounts, ignored until added: persnal", out)

    def test_set_unknown_setting_is_an_error(self):
        code, _, err = self.run_cli("set", "speed", "fast")
        self.assertEqual(code, 1)
        self.assertIn("Unknown setting 'speed'", err)

    def test_set_and_config_show_the_settings(self):
        _, out, _ = self.run_cli("set", "seven_day_threshold", "97")
        self.assertIn("seven_day_threshold = 97", out)
        _, out, _ = self.run_cli("config")
        self.assertIn("  seven_day_threshold = 97", out)
        self.assertIn("  exclude = []", out)

    def test_remove_keeps_the_tokens_in_backups(self):
        self.add_two_accounts()
        _, out, _ = self.run_cli("remove", "work")
        self.assertIn("Removed 'work'", out)
        self.assertEqual(self.store.names(), ["personal"])
        self.assertIn("rt-a1", self.backed_up_refresh_tokens())

    def test_removing_the_live_account_says_claude_stays_logged_in_and_cleans_the_config(self):
        self.add_two_accounts()
        self.run_cli("set", "order", "personal", "work")
        self.run_cli("exclude", "personal")
        self.run_cli("set", "reserve", "personal:20", "work:10")
        self.run_cli("set", "remote_control_accounts", "personal")
        _, out, _ = self.run_cli("remove", "personal")
        self.assertIn("stays logged in", out)
        self.assertEqual((self.config().order, self.config().exclude), (("work",), ()))
        self.assertEqual((self.config().reserve, self.config().remote_control_accounts), (("work:10",), ("personal",)))
        code, _, err = self.run_cli("remove", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("No saved account", err)

    def test_run_needs_a_task_or_a_session_to_resume(self):
        self.add_two_accounts()
        for argv in (["run"], ["run", "task", "--resume", "abc"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("either a task or --resume", err)

    def test_run_starts_claude_with_permission_mode_and_extra_options(self):
        self.add_two_accounts()
        with mock.patch.object(cli.runner, "run", return_value=0) as run, \
             mock.patch.object(cli.headless, "Session") as session:
            code, out, _ = self.run_cli("run", "fix it", "--", "--model", "opus")
            start = run.call_args.args[1]
            start(False)
        self.assertEqual(code, 0)
        _, resume, _, claude_args = session.call_args.args
        self.assertFalse(resume)
        self.assertEqual(claude_args[:2], ["--permission-mode", "acceptEdits"])
        self.assertEqual(claude_args[2:4], ["--append-system-prompt", cli.runner.UNATTENDED])
        self.assertEqual(claude_args[4:], ["--model", "opus"])
        self.assertIn("from this folder with: cswitch run --resume", out)
        self.assertEqual(run.call_args.args[6], cli.runner.BACKGROUND_SECONDS)

    def test_run_takes_how_long_to_wait_for_background_work(self):
        self.add_two_accounts()
        with mock.patch.object(cli.runner, "run", return_value=0) as run:
            self.run_cli("run", "simulate", "--background-hours", "1.5")
            self.assertEqual(run.call_args.args[6], 5400)
            for bad in ("0", "-1", "nan", "inf", "soon"):
                with self.subTest(bad=bad), self.assertRaises(SystemExit):
                    self.run_cli("run", "simulate", "--background-hours", bad)

    def test_run_with_remote_control_wraps_the_session(self):
        self.add_two_accounts()
        with (mock.patch.object(cli.runner, "run", return_value=0) as run,
              mock.patch.object(cli.headless, "Session")):
            self.run_cli("run", "fix it", "--remote-control")
            wrapped = run.call_args.args[1](False)
            self.run_cli("run", "fix it")
            plain = run.call_args.args[1](False)
        self.assertIsInstance(wrapped, cli.RemoteControl)
        self.assertNotIsInstance(plain, cli.RemoteControl)

    def test_remote_control_only_on_allowed_accounts(self):
        self.add_two_accounts()
        with mock.patch.dict(os.environ, {"CSWITCH_HOME": str(self.root / ".cswitcher")}):
            self.assertEqual(cli._remote_control_account(self.home, self.store), "personal")
            self.run_cli("set", "remote_control_accounts", "work")
            self.assertIsNone(cli._remote_control_account(self.home, self.store))
            self.run_cli("set", "remote_control_accounts", "work", "personal")
            self.assertEqual(cli._remote_control_account(self.home, self.store), "personal")

    def test_run_refuses_claude_options_it_sets_itself(self):
        self.add_two_accounts()
        code, _, err = self.run_cli("run", "fix it", "--", "--resume=abc")
        self.assertEqual(code, 1)
        self.assertIn("sets --resume=abc itself", err)

    def test_run_warns_when_hooks_are_not_installed(self):
        self.add_two_accounts()
        with mock.patch.object(cli.runner, "run", return_value=0):
            _, out, _ = self.run_cli("run", "fix it")
            self.run_cli("install")
            _, installed_out, _ = self.run_cli("run", "fix it")
        self.assertIn("hooks aren't installed", out)
        self.assertNotIn("hooks aren't installed", installed_out)

    def test_options_after_double_dash_are_only_for_run(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["status", "--", "--model", "opus"])

    def test_prime_passes_the_named_accounts_and_checks_them(self):
        self.add_two_accounts()
        with mock.patch.object(cli.priming, "prime", return_value=[]) as prime:
            self.run_cli("prime")
            self.run_cli("prime", "work")
        self.assertEqual([call.args[-1] for call in prime.call_args_list], [None, ["work"]])
        code, _, err = self.run_cli("prime", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("No saved account named ghost", err)

    def test_prime_fails_if_any_account_failed(self):
        self.add_two_accounts()
        results = [cli.priming.PrimeResult("work", cli.priming.Outcome.SKIPPED, "window running until 11:40"),
                   cli.priming.PrimeResult("personal", cli.priming.Outcome.FAILED, "claude exited with 1")]
        with mock.patch.object(cli.priming, "prime", return_value=iter(results)):
            code, out, _ = self.run_cli("prime")
        self.assertEqual(code, 1)
        self.assertIn("personal         failed: claude exited with 1", out)

    def test_prime_says_when_there_is_nothing_to_prime(self):
        with mock.patch.object(cli.priming, "prime", return_value=iter([])):
            code, out, _ = self.run_cli("prime")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to prime", out)

    def test_schedule_install_needs_a_prime_mode_and_installs_for_the_accounts(self):
        self.add_two_accounts()
        code, _, err = self.run_cli("schedule", "install")
        self.assertEqual(code, 1)
        self.assertIn("prime_mode", err)
        _, hint, _ = self.run_cli("set", "prime_mode", "spread")
        self.assertIn("cswitch schedule install", hint)
        _, out, _ = self.run_cli("schedule", "install")
        self.assertEqual(self.task["install"].call_args.args[1], 2)
        self.assertIn("personal, work, one account at a time, about every 2 h 33 min, between 08:00 and 20:00", out)
        self.assertIn("wakes the PC at 08:00, 10:33", out)
        self.assertIn("wake timers are off", out)

    def test_an_installed_schedule_follows_setting_and_account_changes(self):
        self.add_two_accounts()
        self.run_cli("set", "prime_mode", "together")
        self.task["installed"].return_value = True
        _, out, _ = self.run_cli("set", "prime_from", "06:00")
        self.assertEqual(self.task["install"].call_args.args[0].prime_from, "06:00")
        self.assertIn("Scheduled priming: personal, work, all together", out)
        self.assertNotIn("wake timers are off", out)
        self.run_cli("exclude", "work")
        self.assertEqual(self.task["install"].call_args.args[1], 1)
        self.run_cli("set", "notifications", "false")
        self.assertEqual(self.task["install"].call_count, 2)
        _, out, _ = self.run_cli("set", "prime_mode", "off")
        self.task["uninstall"].assert_called_once()
        self.assertIn("removed: prime_mode is off", out)

    def test_a_failed_schedule_update_says_the_setting_was_saved(self):
        self.add_two_accounts()
        self.run_cli("set", "prime_mode", "together")
        self.task["installed"].return_value = True
        self.task["install"].side_effect = cli.CswitchError("schtasks failed: access denied")
        code, out, _ = self.run_cli("set", "prime_from", "06:00")
        self.assertEqual((code, self.config().prime_from), (0, "06:00"))
        self.assertIn("Done, but updating scheduled priming failed", out)

    def test_scheduled_prime_logs_problems_instead_of_printing(self):
        with mock.patch.object(cli.priming, "run_scheduled", side_effect=ValueError("bad number")):
            code, out, err = self.run_cli("prime", "--scheduled")
        self.assertEqual((code, out, err), (1, "", ""))
        self.assertIn("scheduled run failed: ValueError: bad number", "\n".join(log.tail(self.root / ".cswitcher", 5)))

    def test_setting_changes_and_switches_by_hand_are_logged(self):
        self.add_two_accounts()
        self.run_cli("set", "five_hour_threshold", "85")
        self.run_cli("switch", "work")
        logged = "\n".join(log.tail(self.root / ".cswitcher", 5))
        self.assertIn("set five_hour_threshold = 85", logged)
        self.assertIn("switched to 'work' by hand", logged)

    def test_log_shows_recent_activity(self):
        _, empty, _ = self.run_cli("log")
        self.assertIn("Nothing logged yet", empty)
        log.write(self.root / ".cswitcher", "switched 'a' -> 'b'")
        _, out, _ = self.run_cli("log")
        self.assertIn("switched 'a' -> 'b'", out)

    def test_config_creates_template_once(self):
        _, first, _ = self.run_cli("config")
        _, second, _ = self.run_cli("config")
        self.assertIn("Created", first)
        self.assertIn("Config file", second)

    def test_errors_exit_1_with_message(self):
        code, _, err = self.run_cli("switch", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("cswitch: No saved account", err)

    def test_list_marks_active_account(self):
        self.add_two_accounts()
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("* personal", out)
        self.assertIn("  work", out)

    def test_names_are_case_insensitive(self):
        self.add_two_accounts()
        _, out, _ = self.run_cli("switch", "Personal")
        self.assertIn("already active", out)

    def test_add_new_checks_name_before_signing_out(self):
        self.add_two_accounts()
        code, _, err = self.run_cli("add", "work", "--new")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertIsNotNone(self.home.read_login())

    def test_add_new_saves_the_freshly_logged_in_account(self):
        self.log_in("uuid-a", "a1")
        self.run_cli("add", "work")
        with mock.patch("builtins.input", side_effect=lambda _: self.log_in("uuid-b", "b1")):
            code, out, _ = self.run_cli("add", "personal", "--new")
        self.assertEqual(code, 0)
        self.assertIn("Saved uuid-b@example.com as 'personal'", out)

    def test_add_new_waits_when_enter_is_pressed_before_login(self):
        self.log_in("uuid-a", "a1")
        self.run_cli("add", "work")
        def press_enter(prompt: str) -> None:
            if "Not logged in yet" in prompt:
                self.log_in("uuid-b", "b1", fresh=True)

        with mock.patch("builtins.input", side_effect=press_enter) as prompts:
            code, out, _ = self.run_cli("add", "personal", "--new")
        self.assertEqual(code, 0)
        self.assertEqual(prompts.call_count, 2)
        self.assertIn("as 'personal'", out)

    def test_add_new_cancelled_puts_the_previous_login_back(self):
        self.log_in("uuid-a", "a1")
        self.run_cli("add", "work")
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            code, _, err = self.run_cli("add", "personal", "--new")
        self.assertEqual(code, 1)
        self.assertIn("logged in as 'work' again", err)
        self.assertEqual(self.live_refresh_token(), "rt-a1")

    def test_setup_takes_a_config_file_and_reports_a_stop(self):
        self.add_two_accounts()
        copied = self.root / "copied.toml"
        copied.write_text('order = ["work", "personal"]\n', encoding="utf-8")
        with mock.patch("builtins.input", side_effect=[""]):
            code, out, _ = self.run_cli("setup", "--config", str(copied))
        self.assertEqual((code, self.config().order), (0, ("work", "personal")))
        self.assertIn("Done.", out)
        with mock.patch("builtins.input", side_effect=EOFError):
            code, _, err = self.run_cli("setup")
        self.assertEqual(code, 1)
        self.assertIn("Setup stopped", err)


if __name__ == "__main__":
    unittest.main()
