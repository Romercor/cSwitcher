"""Account operations: add, sign out, switch, and protection against token mix-ups."""

import unittest
from unittest import mock

from cswitch.accounts import switcher
from cswitch.accounts.store import BACKUPS_KEPT
from cswitch.errors import CswitchError
from tests.fakes import FakeHomeTestCase


class SwitchTest(FakeHomeTestCase):
    def test_round_trip(self):
        self.add_two_accounts()
        self.assertEqual(switcher.active_name(self.home, self.store), "personal")
        self.assertTrue(switcher.switch_to(self.home, self.store, "work"))
        self.assertEqual(switcher.active_name(self.home, self.store), "work")
        self.assertEqual(self.live_refresh_token(), "rt-a1")

    def test_keeps_tokens_claude_refreshed_while_active(self):
        self.add_two_accounts()
        self.log_in("uuid-b", "b2")
        switcher.switch_to(self.home, self.store, "work")
        switcher.switch_to(self.home, self.store, "personal")
        self.assertEqual(self.live_refresh_token(), "rt-b2")

    def test_switch_to_active_account_is_noop(self):
        self.add_two_accounts()
        self.assertFalse(switcher.switch_to(self.home, self.store, "personal"))

    def test_switch_while_logged_out(self):
        self.add_two_accounts()
        self.home.clear_login()
        self.assertTrue(switcher.switch_to(self.home, self.store, "work"))
        self.assertEqual(switcher.active_name(self.home, self.store), "work")

    def test_unknown_live_login_is_backed_up_not_lost(self):
        self.add_two_accounts()
        self.log_in("uuid-unknown", "x1")
        switcher.switch_to(self.home, self.store, "work")
        backups = [b.read_text() for b in self.store.backups_dir.glob("*.json")]
        self.assertTrue(any("rt-x1" in b for b in backups))

    def test_saved_live_login_is_not_duplicated_into_backups(self):
        self.add_two_accounts()
        switcher.switch_to(self.home, self.store, "work")
        self.assertEqual(list(self.store.backups_dir.glob("*.json")), [])

    def test_backups_are_rotated(self):
        self.add_two_accounts()
        for i in range(BACKUPS_KEPT + 3):
            self.log_in("uuid-unknown", f"x{i}")
            switcher.switch_to(self.home, self.store, "work")
        self.assertEqual(len(list(self.store.backups_dir.glob("*.json"))), BACKUPS_KEPT)

    def test_backups_skip_duplicates_and_drop_expired_logins(self):
        self.add_two_accounts()
        self.log_in("uuid-unknown", "x1")
        switcher.switch_to(self.home, self.store, "work")
        self.log_in("uuid-unknown", "x1")
        switcher.switch_to(self.home, self.store, "work")
        self.assertEqual(self.backed_up_refresh_tokens(), ["rt-x1"])
        expired = self.store.backups_dir / "1.json"
        self.write(expired, {"credentials": {"claudeAiOauth": {"refreshToken": "rt-old", "refreshTokenExpiresAt": 1}},
                             "oauthAccount": {}})
        self.log_in("uuid-unknown", "x2")
        switcher.switch_to(self.home, self.store, "work")
        self.assertEqual(sorted(self.backed_up_refresh_tokens()), ["rt-x1", "rt-x2"])

    def test_snapshot_keeps_previous_version(self):
        self.add_two_accounts()
        self.log_in("uuid-b", "b2")
        switcher.switch_to(self.home, self.store, "work")
        self.assertIn("rt-b1", (self.store.accounts_dir / "personal.json.bak").read_text())

    def test_unchanged_sync_keeps_previous_version(self):
        self.add_two_accounts()
        self.log_in("uuid-b", "b2")
        switcher.switch_to(self.home, self.store, "work")
        switcher.switch_to(self.home, self.store, "personal")
        switcher.switch_to(self.home, self.store, "work")
        self.assertIn("rt-b1", (self.store.accounts_dir / "personal.json.bak").read_text())

    def test_fresh_login_tokens_rotated_while_active_reach_their_owner(self):
        self.add_two_accounts(fresh=True)
        self.write_tokens("b2", None)
        switcher.switch_to(self.home, self.store, "work")
        self.assertEqual(self.store.load("personal").refresh_token, "rt-b2")

    def test_switch_drops_previous_accounts_token_organization(self):
        self.log_in("uuid-a", "a1")
        switcher.add_current(self.home, self.store, "team")
        switcher.sign_out_for_new_login(self.home, self.store)
        self.log_in("uuid-b", "b1", fresh=True)
        switcher.add_current(self.home, self.store, "pro")
        switcher.switch_to(self.home, self.store, "team")
        switcher.switch_to(self.home, self.store, "pro")
        self.assertNotIn("organizationUuid", self.read(self.home.credentials_file))
        self.assertEqual(switcher.active_name(self.home, self.store), "pro")


class TokenMixUpTest(FakeHomeTestCase):
    def test_interrupted_switch_restores_previous_login(self):
        self.add_two_accounts()
        real_set_account = type(self.home)._set_account
        calls = []

        def fail_first_time(home, account):
            calls.append(account)
            if len(calls) == 1:
                raise OSError("disk full")
            real_set_account(home, account)

        with mock.patch.object(type(self.home), "_set_account", fail_first_time):
            with self.assertRaises(OSError):
                switcher.switch_to(self.home, self.store, "work")
        self.assertEqual(self.live_refresh_token(), "rt-b1")
        self.assertEqual(self.home.read_login().account_uuid, "uuid-b")

    def test_torn_state_is_credited_by_refresh_token(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.set_identity("uuid-b", "org-uuid-b")
        self.assertEqual(switcher.active_name(self.home, self.store), "work")
        switcher.switch_to(self.home, self.store, "personal")
        work = self.store.load("work")
        self.assertEqual((work.refresh_token, work.account_uuid), ("rt-a1", "uuid-a"))
        self.assertEqual(self.live_refresh_token(), "rt-b1")

    def test_weak_match_backs_up_the_snapshot_before_overwriting(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("b2", None)
        switcher.switch_to(self.home, self.store, "personal")
        self.assertIn("rt-a1", self.backed_up_refresh_tokens())

    def test_weak_match_with_other_plan_is_treated_as_unknown(self):
        self.add_two_accounts(fresh=True, plans=("max", "pro"))
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("b2", None, plan="pro")
        switcher.switch_to(self.home, self.store, "personal")
        self.assertEqual(self.store.load("work").refresh_token, "rt-a1")
        self.assertIn("rt-b2", self.backed_up_refresh_tokens())

    def test_weak_match_is_never_reported_as_already_active(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("b2", None)
        self.assertTrue(switcher.switch_to(self.home, self.store, "work"))

    def test_server_confirming_the_identity_makes_it_certain(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("a2", None)
        switcher.switch_to(self.home, self.store, "personal", identify=lambda login: "org-uuid-a")
        self.assertEqual(self.store.load("work").refresh_token, "rt-a2")
        self.assertEqual(self.backed_up_refresh_tokens(), [])

    def test_server_naming_another_account_moves_tokens_with_safeguards(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("b2", None)
        switcher.switch_to(self.home, self.store, "personal", identify=lambda login: "org-uuid-b")
        self.assertEqual(self.store.load("work").refresh_token, "rt-a1")
        self.assertEqual(self.live_refresh_token(), "rt-b2")
        self.assertIn("rt-b1", self.backed_up_refresh_tokens())

    def test_wrong_server_answer_cannot_lose_tokens(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("a2", None)
        switcher.switch_to(self.home, self.store, "work", identify=lambda login: "org-uuid-b")
        self.assertIn("rt-b1", self.backed_up_refresh_tokens())

    def test_server_naming_an_unsaved_organization_falls_back_to_local_rules(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("x1", None)
        switcher.switch_to(self.home, self.store, "personal", identify=lambda login: "org-elsewhere")
        self.assertIn("rt-a1", self.backed_up_refresh_tokens())

    def test_add_asks_the_server_at_most_once(self):
        self.add_two_accounts(fresh=True)
        self.write_tokens("b2", None)
        asked = mock.Mock(return_value="org-uuid-b")
        switcher.add_current(self.home, self.store, "personal", identify=asked)
        self.assertEqual(asked.call_count, 1)

    def test_no_server_answer_falls_back_to_local_rules(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("b2", None)
        switcher.switch_to(self.home, self.store, "personal", identify=lambda login: None)
        self.assertIn("rt-a1", self.backed_up_refresh_tokens())

    def test_server_is_not_asked_when_files_are_conclusive(self):
        self.add_two_accounts()
        asked = mock.Mock(return_value=None)
        self.log_in("uuid-b", "b2")
        switcher.switch_to(self.home, self.store, "work", identify=asked)
        switcher.switch_to(self.home, self.store, "personal", identify=asked)
        asked.assert_not_called()

    def test_same_person_in_two_organizations(self):
        self.log_in("uuid-x", "t1", org="org-team")
        switcher.add_current(self.home, self.store, "team")
        switcher.sign_out_for_new_login(self.home, self.store)
        self.log_in("uuid-x", "p1", org="org-pro", fresh=True)
        switcher.add_current(self.home, self.store, "pro")
        switcher.switch_to(self.home, self.store, "team")
        self.write_tokens("t2", "org-team")
        self.assertTrue(switcher.switch_to(self.home, self.store, "pro"))
        self.assertEqual(self.store.load("team").refresh_token, "rt-t2")
        self.assertEqual(self.store.load("pro").refresh_token, "rt-p1")
        self.assertEqual(self.live_refresh_token(), "rt-p1")

    def test_tokens_refreshed_by_a_stale_process_reach_their_owner(self):
        self.add_two_accounts()
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("b2", "org-uuid-b")
        switcher.switch_to(self.home, self.store, "work")
        personal = self.store.load("personal")
        self.assertEqual((personal.refresh_token, personal.account_uuid), ("rt-b2", "uuid-b"))
        self.assertEqual(self.live_refresh_token(), "rt-a1")

    def test_switch_repairs_identity_when_tokens_already_belong_to_target(self):
        self.add_two_accounts()
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("b2", "org-uuid-b")
        self.assertTrue(switcher.switch_to(self.home, self.store, "personal"))
        self.assertTrue(self.home.read_login().is_consistent)
        self.assertEqual(self.live_refresh_token(), "rt-b2")

    def test_reverted_switch_is_reported(self):
        self.add_two_accounts()
        real_write = type(self.home).write_login

        def write_then_revert(home, login):
            real_write(home, login)
            self.write_tokens("b9", "org-uuid-b")

        with mock.patch.object(type(self.home), "write_login", write_then_revert):
            with self.assertRaisesRegex(CswitchError, "overwrote the switch"):
                switcher.switch_to(self.home, self.store, "work")


class AddTest(FakeHomeTestCase):
    def test_same_account_cannot_be_saved_twice(self):
        self.add_two_accounts()
        with self.assertRaisesRegex(CswitchError, "already saved as 'personal'"):
            switcher.add_current(self.home, self.store, "other")

    def test_fresh_login_can_be_saved(self):
        self.log_in("uuid-a", "a1", fresh=True)
        self.assertEqual(switcher.add_current(self.home, self.store, "pro").account_uuid, "uuid-a")

    def test_existing_name_in_torn_state_is_refused(self):
        self.add_two_accounts(fresh=True)
        switcher.switch_to(self.home, self.store, "work")
        self.set_identity("uuid-b", "org-uuid-b")
        with self.assertRaisesRegex(CswitchError, "already saved as 'work'"):
            switcher.add_current(self.home, self.store, "personal")
        self.assertEqual(self.store.load("personal").refresh_token, "rt-b1")

    def test_re_adding_on_weak_evidence_backs_up_or_refuses(self):
        self.add_two_accounts(fresh=True, plans=("max", "pro"))
        switcher.switch_to(self.home, self.store, "work")
        self.write_tokens("b2", None, plan="pro")
        with self.assertRaisesRegex(CswitchError, "Can't tell which account"):
            switcher.add_current(self.home, self.store, "work")
        self.assertEqual(self.store.load("work").refresh_token, "rt-a1")
        self.write_tokens("a2", None, plan="max")
        switcher.add_current(self.home, self.store, "work")
        self.assertEqual(self.store.load("work").refresh_token, "rt-a2")
        self.assertIn("rt-a1", self.backed_up_refresh_tokens())

    def test_second_account_of_same_organization_is_rejected(self):
        self.add_two_accounts()
        self.log_in("uuid-c", "c1", org="org-uuid-a")
        with self.assertRaisesRegex(CswitchError, "already saved as 'work'"):
            switcher.add_current(self.home, self.store, "other")

    def test_name_holding_another_account_is_rejected(self):
        self.add_two_accounts()
        self.log_in("uuid-c", "c1")
        with self.assertRaisesRegex(CswitchError, "already holds another account"):
            switcher.add_current(self.home, self.store, "work")

    def test_inconsistent_login_is_rejected(self):
        self.log_in("uuid-a", "a1")
        credentials = self.read(self.home.credentials_file)
        credentials["organizationUuid"] = "org-other"
        self.write(self.home.credentials_file, credentials)
        with self.assertRaisesRegex(CswitchError, "disagree"):
            switcher.add_current(self.home, self.store, "work")

    def test_sign_out_refuses_unsaved_login(self):
        self.log_in("uuid-a", "a1")
        with self.assertRaisesRegex(CswitchError, "not saved"):
            switcher.sign_out_for_new_login(self.home, self.store)
        self.assertIsNotNone(self.home.read_login())

    def test_sign_out_clears_login(self):
        self.log_in("uuid-a", "a1")
        switcher.add_current(self.home, self.store, "work")
        self.assertEqual(switcher.sign_out_for_new_login(self.home, self.store), "work")
        self.assertIsNone(self.home.read_login())
        self.assertNotIn("oauthAccount", self.read(self.home.global_config))


class StoreTest(FakeHomeTestCase):
    def test_unknown_account(self):
        with self.assertRaisesRegex(CswitchError, "No saved account"):
            switcher.switch_to(self.home, self.store, "ghost")

    def test_invalid_name(self):
        with self.assertRaisesRegex(CswitchError, "lowercase letters"):
            self.store.exists("../evil")

    def test_corrupt_snapshot(self):
        self.store.accounts_dir.mkdir(parents=True)
        self.write(self.store.accounts_dir / "work.json", {"oops": 1})
        with self.assertRaisesRegex(CswitchError, "not a valid cSwitcher account file"):
            self.store.load("work")

    def test_json_that_is_not_an_object(self):
        self.store.accounts_dir.mkdir(parents=True)
        (self.store.accounts_dir / "work.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(CswitchError, "not contain a JSON object"):
            self.store.load("work")

    def test_stray_files_are_ignored(self):
        self.add_two_accounts()
        self.write(self.store.accounts_dir / "Old Name.json", {})
        self.assertEqual(self.store.names(), ["personal", "work"])


if __name__ == "__main__":
    unittest.main()
