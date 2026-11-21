"""cSwitcher's entries in Claude Code's settings.json: added and removed without touching the user's own."""

import sys
from unittest import mock

from cswitch.claude import settings
from cswitch.errors import CswitchError
from tests.fakes import FakeHomeTestCase

USER_HOOK = {"type": "command", "command": "echo mine"}
ARGS = ("hook", "Stop")


class SettingsTest(FakeHomeTestCase):
    def setUp(self):
        super().setUp()
        self.file = self.home.settings_file
        self.original = {
            "effortLevel": "h\u00f6her",
            "hooks": {
                "Stop": [{"hooks": [USER_HOOK]}],
                "PreToolUse": [{"matcher": "Bash"}],
                "Notification": [],
            },
        }
        self.write(self.file, self.original)

    def our_commands(self, event: str) -> list[str]:
        groups = self.read(self.file).get("hooks", {}).get(event, [])
        return [h["command"] for g in groups for h in g.get("hooks", []) if "-m cswitch" in h["command"]]

    def test_install_adds_ours_next_to_users_entries(self):
        settings.install(self.file, {"Stop": ARGS, "PostToolUse": ARGS})
        data = self.read(self.file)
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"], [USER_HOOK])
        self.assertEqual(len(self.our_commands("Stop")), 1)
        self.assertEqual(len(self.our_commands("PostToolUse")), 1)

    def test_install_replaces_all_previous_cswitch_entries(self):
        settings.install(self.file, {"Stop": ARGS})
        settings.install(self.file, {"PostToolUse": ARGS})
        self.assertEqual(self.our_commands("Stop"), [])
        self.assertEqual(len(self.our_commands("PostToolUse")), 1)

    def test_uninstall_restores_original_exactly(self):
        settings.install(self.file, {"Stop": ARGS, "PostToolUse": ARGS, "PreToolUse": ARGS})
        settings.uninstall(self.file)
        self.assertEqual(self.read(self.file), self.original)

    def test_uninstall_keeps_users_hook_in_a_mixed_group(self):
        ours = {"type": "command", "command": "py -m cswitch hook Stop"}
        self.write(self.file, {"hooks": {"Stop": [{"matcher": "", "hooks": [USER_HOOK, ours]}]}})
        settings.uninstall(self.file)
        self.assertEqual(self.read(self.file), {"hooks": {"Stop": [{"matcher": "", "hooks": [USER_HOOK]}]}})

    def test_uninstall_without_our_entries_does_not_rewrite(self):
        self.file.write_text('{"keep":   "formatting"}', encoding="utf-8")
        settings.uninstall(self.file)
        self.assertEqual(self.file.read_text(encoding="utf-8"), '{"keep":   "formatting"}')

    def test_status_line_from_early_versions_is_removed_but_users_own_is_kept(self):
        self.write(self.file, {"statusLine": {"type": "command", "command": "py -m cswitch probe status"}})
        settings.uninstall(self.file)
        self.assertEqual(self.read(self.file), {})
        self.write(self.file, {"statusLine": {"type": "command", "command": "my-line"}})
        settings.install(self.file, {"Stop": ARGS})
        self.assertEqual(self.read(self.file)["statusLine"]["command"], "my-line")

    def test_unexpected_shapes_are_errors_not_crashes(self):
        for hooks in (None, ["x"], {"Stop": {"not": "a list"}}):
            with self.subTest(hooks=hooks):
                self.write(self.file, {"hooks": hooks})
                with self.assertRaises(CswitchError):
                    settings.install(self.file, {"Stop": ARGS})
                settings.uninstall(self.file)
                self.assertEqual(self.read(self.file), {"hooks": hooks})

    def test_works_without_existing_settings_file(self):
        self.file.unlink()
        settings.install(self.file, {"Stop": ARGS})
        settings.uninstall(self.file)
        self.assertEqual(self.read(self.file), {})


class CommandTest(FakeHomeTestCase):
    def test_command_is_unquoted_so_bash_and_powershell_both_run_it(self):
        command = settings._command(ARGS)
        self.assertNotIn('"', command)
        self.assertTrue(command.endswith(" -m cswitch hook Stop"))

    def test_path_with_spaces_is_rejected_when_no_short_name_exists(self):
        with mock.patch.object(sys, "executable", "/opt/my tools/python"), \
             mock.patch.object(settings, "_short_windows_path", side_effect=lambda p: p):
            with self.assertRaisesRegex(CswitchError, "spaces"):
                settings._command(ARGS)

    def test_path_with_spaces_uses_windows_short_name(self):
        with mock.patch.object(sys, "executable", "C:/My Tools/python.exe"), \
             mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(settings, "_short_windows_path", return_value="C:\\MYTOOL~1\\python.exe"):
            self.assertEqual(settings._command(ARGS), "C:/MYTOOL~1/python.exe -m cswitch hook Stop")
