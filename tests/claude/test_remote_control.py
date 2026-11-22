"""Remote Control in `cswitch run`: turned on for allowed accounts, turned on again after
an account switch ends it, other failures left to Claude, refusals retried quietly, off on
accounts without it, and its events (and stale remote turns) hidden from the runner."""

import unittest
from unittest import mock

from cswitch.claude import remote_control
from cswitch.claude.headless import IDLE_EVENT
from cswitch.claude.remote_control import RemoteControl

IDLE = {"type": IDLE_EVENT}
WORK = {"type": "assistant", "message": {"content": []}}


def answer(request_id: str, url: str = "https://claude.ai/code/session_1", ok: bool = True) -> dict:
    body = {"subtype": "success", "request_id": request_id, "response": {"session_url": url}} if ok else \
        {"subtype": "error", "request_id": request_id, "error": "disabled by your organization"}
    return {"type": "control_response", "response": body}


def bridge(state: str, kind: str = "terminal") -> dict:
    return {"type": "system", "subtype": "bridge_state", "state": state, "failure_kind": kind,
            "detail": "account changed"}


def policy_disabled() -> dict:
    return {"type": "system", "subtype": "bridge_state", "state": "policy_disabled",
            "detail": "disabled by your organization"}


class FakeSession:
    def __init__(self, clock: list[float], events: list[dict]):
        self.clock, self.events, self.sent, self.messages = clock, list(events), [], []

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def send_user(self, text: str) -> None:
        self.messages.append(text)

    def next_event(self, silence_seconds: float) -> dict | None:
        while self.events and callable(self.events[0]):
            self.events.pop(0)()
        if not self.events:
            return None
        event = self.events.pop(0)
        if event is IDLE:
            self.clock[0] += silence_seconds
        return event


class RemoteControlTest(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        patcher = mock.patch.object(remote_control.time, "monotonic", side_effect=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.live: str | None = "pro"
        self.said: list[str] = []

    def switch_to(self, account: str | None):
        return lambda: setattr(self, "live", account)

    def wrap(self, *events) -> tuple[RemoteControl, FakeSession]:
        session = FakeSession(self.clock, list(events))
        return RemoteControl(session, lambda: self.live, "cswitch: project", self.said.append), session

    def requests(self, session: FakeSession) -> list[str]:
        return [m["request_id"] for m in session.sent]

    def test_turned_on_for_an_allowed_account_and_its_events_hidden(self):
        wrapped, session = self.wrap(answer("cswitch-rc-1"), bridge("connected"), WORK)
        self.assertEqual(wrapped.next_event(60), WORK)
        self.assertEqual(self.requests(session), ["cswitch-rc-1"])
        self.assertEqual(session.sent[0]["request"]["name"], "cswitch: project")
        self.assertIn("Remote Control on for 'pro': https://claude.ai/code/session_1", self.said)

    def test_not_turned_on_for_an_account_without_it(self):
        self.live = None
        wrapped, session = self.wrap(WORK)
        wrapped.next_event(60)
        self.assertEqual(session.sent, [])

    def test_turned_on_again_after_a_switch_ends_it(self):
        wrapped, session = self.wrap(answer("cswitch-rc-1"), self.switch_to("other-pro"), bridge("failed"),
                                     IDLE, answer("cswitch-rc-2", "u2"), WORK)
        self.assertEqual(wrapped.next_event(3 * remote_control.CHECK_SECONDS), WORK)
        self.assertEqual(self.requests(session), ["cswitch-rc-1", "cswitch-rc-2"])
        self.assertIn("Remote Control on for 'other-pro': u2", self.said)

    def test_comes_back_after_a_stretch_on_an_account_without_it(self):
        wrapped, session = self.wrap(answer("cswitch-rc-1"), self.switch_to(None), bridge("failed"), IDLE,
                                     IDLE, self.switch_to("pro"), IDLE, WORK)
        self.assertEqual(wrapped.next_event(5 * remote_control.CHECK_SECONDS), WORK)
        self.assertEqual(self.requests(session), ["cswitch-rc-1", "cswitch-rc-2"])

    def test_an_account_change_failure_is_retried_by_the_next_check_not_at_once(self):
        wrapped, session = self.wrap(answer("cswitch-rc-1"), bridge("failed"), WORK)
        wrapped.next_event(60)
        self.assertEqual(self.requests(session), ["cswitch-rc-1"])

    def test_other_bridge_failures_are_left_to_claude(self):
        wrapped, session = self.wrap(answer("cswitch-rc-1"), bridge("failed", kind="network"), IDLE, IDLE, WORK)
        self.assertEqual(wrapped.next_event(5 * remote_control.CHECK_SECONDS), WORK)
        self.assertEqual(self.requests(session), ["cswitch-rc-1"])
        self.assertFalse(any("ended" in line for line in self.said))

    def test_disabled_by_policy_is_announced_once_and_not_retried(self):
        """What Claude Code sends for an organization that disables it: the bridge state, then a refusal."""
        wrapped, session = self.wrap(policy_disabled(), answer("cswitch-rc-1", ok=False),
                                     self.switch_to("other-pro"), IDLE, answer("cswitch-rc-2"),
                                     self.switch_to("pro"), IDLE, IDLE, WORK)
        self.assertEqual(wrapped.next_event(5 * remote_control.CHECK_SECONDS), WORK)
        self.assertEqual(self.requests(session), ["cswitch-rc-1", "cswitch-rc-2"])
        self.assertEqual([line for line in self.said if "'pro'" in line],
                         ["Remote Control is disabled for 'pro': disabled by your organization"])

    def test_a_policy_block_is_kept_across_restarts_and_either_event_order(self):
        blocked: set[str] = set()
        first = FakeSession(self.clock, [answer("cswitch-rc-1", ok=False), policy_disabled(), IDLE, WORK])
        RemoteControl(first, lambda: self.live, "cswitch: project", self.said.append, blocked).next_event(120)
        restarted = FakeSession(self.clock, [WORK])
        RemoteControl(restarted, lambda: self.live, "cswitch: project", self.said.append, blocked).next_event(60)
        self.assertEqual((len(first.sent), restarted.sent, blocked), (1, [], {"pro"}))
        self.assertEqual(sum("disabled for 'pro'" in line for line in self.said), 1)

    def test_the_reply_names_the_account_it_was_requested_for(self):
        wrapped, _ = self.wrap(self.switch_to("other-pro"), answer("cswitch-rc-1"), WORK)
        wrapped.next_event(60)
        self.assertIn("Remote Control on for 'pro': https://claude.ai/code/session_1", self.said)

    def test_a_remote_turn_is_let_finish_and_dropped_before_the_runner_speaks(self):
        runner_done = {"type": "result", "subtype": "success", "is_error": False, "result": "runner turn"}
        phone_done = {"type": "result", "subtype": "success", "is_error": False, "result": "phone turn"}
        wrapped, session = self.wrap(answer("cswitch-rc-1"), runner_done, WORK, IDLE, WORK, phone_done, IDLE, WORK)
        self.assertEqual(wrapped.next_event(0), runner_done)
        wrapped.send_user("continue")
        self.assertEqual(session.messages, ["continue"])
        self.assertEqual(wrapped.next_event(60), WORK)
        self.assertEqual(session.events, [])

    def test_the_latest_background_task_list_survives_a_drain(self):
        running = {"type": "system", "subtype": "background_tasks_changed", "tasks": [{"description": "sim"}]}
        ended = {"type": "system", "subtype": "background_tasks_changed", "tasks": []}
        woken_done = {"type": "result", "subtype": "success", "is_error": False, "result": "woken turn"}
        wrapped, session = self.wrap(answer("cswitch-rc-1"), running, ended, WORK, woken_done, IDLE, WORK)
        self.assertEqual(wrapped.next_event(0), running)
        wrapped.send_user("continue")
        self.assertEqual(wrapped.next_event(60), ended)
        self.assertEqual(wrapped.next_event(60), WORK)

    def test_nothing_is_drained_before_remote_control_was_ever_turned_on(self):
        self.live = None
        wrapped, session = self.wrap(WORK)
        wrapped.send_user("task")
        self.assertEqual((session.messages, session.events), (["task"], [WORK]))

    def test_a_refusal_is_retried_once_a_minute_and_announced_once(self):
        wrapped, session = self.wrap(answer("cswitch-rc-1", ok=False), IDLE, answer("cswitch-rc-2", ok=False), WORK)
        self.assertEqual(wrapped.next_event(3 * remote_control.CHECK_SECONDS), WORK)
        self.assertEqual(self.requests(session), ["cswitch-rc-1", "cswitch-rc-2"])
        self.assertEqual(sum("unavailable for 'pro'" in line for line in self.said), 1)

    def test_silence_is_passed_on_only_after_the_full_wait(self):
        wrapped, _ = self.wrap(IDLE, IDLE, IDLE)
        self.assertEqual(wrapped.next_event(2.5 * remote_control.CHECK_SECONDS), IDLE)
        self.assertGreaterEqual(self.clock[0], 1000 + 2.5 * remote_control.CHECK_SECONDS)

    def test_other_control_responses_and_events_pass_through(self):
        foreign = {"type": "control_response", "response": {"request_id": "other", "subtype": "success"}}
        wrapped, _ = self.wrap(foreign)
        self.assertEqual(wrapped.next_event(60), foreign)


if __name__ == "__main__":
    unittest.main()
