"""`cswitch run`'s loop: when a task counts as finished, how usage limits are recognised
and handled (switch, a hook's switch, waiting), retries with growing pauses, giving up
without progress, restarting a crashed or silent Claude, and failing fast."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cswitch import log, notify, runner
from cswitch.claude.headless import IDLE_EVENT
from cswitch.config import Config
from cswitch.errors import CswitchError
from cswitch.rotation import engine, policy, state

START = 1_000_000.0


def said(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def used(name: str, **tool_input: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]}}


def api_error(error: str) -> dict:
    return {"type": "assistant", "error": error, "message": {"content": [{"type": "text", "text": "API Error"}]}}


def finished(text: str = "done", **extra: object) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, **extra}


def failed(text: str, status: int | None = None) -> dict:
    return {"type": "result", "subtype": "success", "is_error": True, "result": text, "api_error_status": status}


def rate_limit(status: str, window: str = "five_hour") -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status, "rateLimitType": window}}


def background(*statuses: str) -> dict:
    return {"type": "system", "subtype": "background_tasks_changed",
            "tasks": [{"description": "task", "status": s} for s in statuses]}


IDLE = {"type": IDLE_EVENT}


class FakeSession:
    def __init__(self, events: list[dict]):
        self.stream, self.sent, self.closed, self.waits = iter(events), [], False, []

    def send_user(self, text: str) -> None:
        self.sent.append(text)

    def next_event(self, silence_seconds: float) -> dict | None:
        self.waits.append(silence_seconds)
        return next(self.stream, None)

    def close(self) -> None:
        self.closed = True


class RunnerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.shown: list[str] = []
        self.now = START
        self.slept: list[float] = []
        self.patch(time, "time", side_effect=lambda: self.now)
        self.patch(time, "sleep", side_effect=self.sleep)
        self.engine_tick = self.patch(engine, "tick")
        self.look_around = self.patch(engine, "look_around")
        self.patch(engine, "reserve_reached", return_value=False)
        self.notify = self.patch(notify, "notify")

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds))
        self.now += seconds

    def tick(self, switched=False, exhausted=False, frees_at=None, hook_switched=False) -> engine.Tick:
        decision = policy.Decision("other" if switched else None, "test", exhausted=exhausted, frees_at=frees_at)
        check = engine.Check(Config(), state.State(), "live", decision)
        return engine.Tick(check, switched, False, recently_switched=hook_switched)

    def run_task(self, *sessions: FakeSession, task: str | None = "fix it") -> int:
        queue = list(sessions)
        self.starts: list[bool] = []

        def start(resume: bool) -> FakeSession:
            self.starts.append(resume)
            return queue.pop(0)

        return runner.run(task, start, None, None, self.root, self.shown.append)

    def notified(self) -> str:
        return self.notify.call_args.args[0]

    def test_finishes_when_a_turn_completes(self):
        session = FakeSession([said("Working on it"), used("Bash", command="pytest -q\nmore"), finished("all green")])
        self.assertEqual(self.run_task(session), 0)
        self.assertEqual(session.sent, ["fix it"])
        self.assertIn("  > Bash pytest -q", self.shown)
        self.assertTrue(session.closed)
        self.engine_tick.assert_not_called()
        self.assertIn("finished", self.notified())
        self.assertIn("run: finished: all green", log.tail(self.root, 5)[-1])

    def test_refused_actions_are_shown_as_they_happen(self):
        denied = {"type": "system", "subtype": "permission_denied", "tool_name": "Bash",
                  "message": "This command requires approval"}
        self.run_task(FakeSession([denied, finished()]))
        self.assertIn("  ! refused Bash: This command requires approval", self.shown)

    def test_refused_actions_are_reported_when_finishing(self):
        denials = [{"tool_name": "Bash"}, {"tool_name": "Bash"}]
        self.run_task(FakeSession([finished("could not run tests", permission_denials=denials)]))
        self.assertIn("2 actions were refused: Bash", self.notify.call_args.args[1])

    def test_finishes_once_background_work_is_done_and_nothing_new_starts(self):
        session = FakeSession([background("running"), finished("waiting"), background("completed"), IDLE])
        self.assertEqual(self.run_task(session), 0)
        self.assertEqual(session.sent, ["fix it"])
        self.assertEqual(session.waits[-1], runner.GRACE_SECONDS)

    def test_a_new_turn_after_background_work_is_followed_to_its_end(self):
        session = FakeSession([background("running"), finished("waiting"), background("completed"),
                               said("reading the output"), used("Read"), finished("all done")])
        self.assertEqual(self.run_task(session), 0)
        self.assertIn("all done", log.tail(self.root, 1)[0])

    def test_background_work_that_never_ends_still_finishes_after_silence(self):
        self.assertEqual(self.run_task(FakeSession([background("running"), finished("serving"), IDLE])), 0)
        self.assertIn("background work still running", log.tail(self.root, 1)[0])

    def test_usage_limit_switches_and_continues_at_once(self):
        self.engine_tick.return_value = self.tick(switched=True)
        session = FakeSession([api_error("rate_limit"), failed("limit"), used("Read"), finished()])
        self.assertEqual(self.run_task(session), 0)
        self.assertEqual(session.sent, ["fix it", "continue"])
        self.assertTrue(self.engine_tick.call_args.kwargs["limit_hit"])
        self.assertEqual(self.slept, [])

    def test_a_failure_looking_at_other_accounts_never_stops_the_switch(self):
        self.look_around.side_effect = OSError("lock busy")
        self.engine_tick.return_value = self.tick(switched=True)
        session = FakeSession([api_error("rate_limit"), failed("limit"), finished()])
        self.assertEqual(self.run_task(session), 0)
        self.assertEqual(session.sent, ["fix it", "continue"])
        self.look_around.assert_called()
        self.assertTrue(any("looking at the other accounts failed" in line for line in log.tail(self.root, 10)))

    def test_a_switch_a_hook_just_made_is_accepted(self):
        self.engine_tick.return_value = self.tick(hook_switched=True)
        session = FakeSession([rate_limit("rejected"), failed("stopped", status=429), finished()])
        self.assertEqual(self.run_task(session), 0)
        self.assertEqual((session.sent, self.slept), (["fix it", "continue"], []))

    def test_limit_recognised_from_the_message_text(self):
        self.engine_tick.return_value = self.tick(switched=True)
        self.run_task(FakeSession([failed("Claude AI usage limit reached|1795278000"), finished()]))
        self.assertTrue(self.engine_tick.call_args.kwargs["limit_hit"])

    def test_limit_recognised_from_a_used_up_window(self):
        self.engine_tick.return_value = self.tick(switched=True)
        info = {"status": "allowed_warning", "rateLimitType": "seven_day",
                "unifiedWindows": {"five_hour": {"utilization": 1.0, "status": "rejected"}}}
        self.run_task(FakeSession([{"type": "rate_limit_event", "rate_limit_info": info}, failed("stopped"), finished()]))
        self.assertTrue(self.engine_tick.call_args.kwargs["limit_hit"])

    def test_a_second_limit_without_progress_pauses_before_continuing(self):
        self.engine_tick.return_value = self.tick(switched=True)
        session = FakeSession([api_error("rate_limit"), failed("limit")] * 2 + [finished()])
        self.assertEqual(self.run_task(session), 0)
        self.assertEqual(self.slept, [60])

    def test_other_errors_are_not_usage_limits(self):
        for events in ([failed("overloaded", status=429)], [rate_limit("rejected", "overage"), failed("tool failed")],
                       [failed("Context limit reached · /compact or /clear to continue")],
                       [failed("Output token limit reached")]):
            with self.subTest(events=events):
                self.engine_tick.reset_mock()
                self.slept.clear()
                self.run_task(FakeSession([*events, finished()]))
                self.engine_tick.assert_not_called()
                self.assertEqual(self.slept, [30])

    def test_reaching_the_reserve_stops_claude_and_waits_for_room(self):
        self.patch(engine, "reserve_reached", return_value=True)
        self.engine_tick.side_effect = [self.tick(exhausted=True, frees_at=START + 600), self.tick()]
        working, resumed = FakeSession([used("Edit"), used("Edit")]), FakeSession([finished()])
        self.assertEqual(self.run_task(working, resumed), 0)
        self.assertTrue(working.closed)
        self.assertEqual(self.starts, [False, True])
        self.assertEqual(self.slept, [600 + runner.RESET_MARGIN_SECONDS])
        self.assertTrue(self.engine_tick.call_args_list[0].kwargs["force"])

    def test_at_the_reserve_a_fresh_check_that_finds_room_switches_at_once(self):
        self.patch(engine, "reserve_reached", return_value=True)
        self.engine_tick.return_value = self.tick(switched=True)
        working, resumed = FakeSession([used("Edit")]), FakeSession([finished()])
        self.assertEqual(self.run_task(working, resumed), 0)
        self.assertEqual((self.starts, self.slept), ([False, True], []))

    def test_at_the_reserve_a_failed_check_is_retried_soon(self):
        self.patch(engine, "reserve_reached", return_value=True)
        self.engine_tick.side_effect = [None, self.tick(switched=True)]
        working, resumed = FakeSession([used("Edit")]), FakeSession([finished()])
        self.assertEqual(self.run_task(working, resumed), 0)
        self.assertEqual(self.slept, [runner.RESET_MARGIN_SECONDS])

    def test_waits_until_the_first_account_frees_up(self):
        self.engine_tick.side_effect = [self.tick(exhausted=True, frees_at=START + 600), self.tick()]
        session = FakeSession([api_error("rate_limit"), failed("limit"), finished()])
        self.assertEqual(self.run_task(session), 0)
        self.assertEqual(self.slept, [600 + runner.RESET_MARGIN_SECONDS])
        self.assertEqual(session.sent, ["fix it", "continue"])
        self.assertTrue(any("every account is used up" in line for line in self.shown))

    def test_waiting_goes_on_when_a_check_has_no_answer_and_never_spins(self):
        used_up = self.tick(exhausted=True, frees_at=START - 100)
        self.engine_tick.side_effect = [used_up, None, used_up, self.tick()]
        self.run_task(FakeSession([api_error("rate_limit"), failed("limit"), finished()]))
        self.assertEqual(self.slept, [runner.RESET_MARGIN_SECONDS] * 3)

    def test_checks_that_keep_failing_are_retried_less_often_and_end_the_run_after_hours(self):
        used_up = self.tick(exhausted=True, frees_at=START - 100)

        def check(*args, **kwargs):
            if self.slept:
                raise OSError("state.json is locked")
            return used_up

        self.engine_tick.side_effect = check
        with self.assertRaisesRegex(CswitchError, "couldn't be checked for hours"):
            self.run_task(FakeSession([api_error("rate_limit"), failed("limit")]))
        margin = runner.RESET_MARGIN_SECONDS
        self.assertEqual(self.slept[:5], [margin, margin, margin * 2, margin * 4, margin * 8])
        self.assertEqual(max(self.slept), runner.MAX_WAIT_SECONDS)
        self.assertGreater(sum(self.slept[1:]), runner.GIVE_UP_SECONDS)
        self.assertIn("stopped by an error", self.notified())
        self.assertTrue(any("check failed: OSError" in line for line in log.tail(self.root, 5)))

    def test_one_successful_check_restarts_the_give_up_clock_and_the_backoff(self):
        used_up = self.tick(exhausted=True, frees_at=START - 100)
        five_hours_of_failures = [None] * 13
        self.engine_tick.side_effect = [used_up, *five_hours_of_failures, used_up, *five_hours_of_failures, self.tick()]
        self.assertEqual(self.run_task(FakeSession([api_error("rate_limit"), failed("limit"), finished()])), 0)
        margin = runner.RESET_MARGIN_SECONDS
        self.assertGreater(sum(self.slept[1:14]), 5 * 3600)
        self.assertEqual(self.slept[14:17], [margin, margin, margin * 2])

    def test_api_errors_are_not_progress_and_the_run_gives_up_after_hours(self):
        session = FakeSession([api_error("authentication_failed"), failed("401")] * 30)
        self.assertEqual(self.run_task(session), 1)
        self.assertEqual(self.slept[:8], [30, 60, 120, 240, 480, 960, 1800, 1800])
        self.assertGreater(sum(self.slept), runner.GIVE_UP_SECONDS - runner.MAX_PAUSE_SECONDS)
        self.assertIn("gave up after hours without progress", self.notified())

    def test_progress_resets_the_pauses(self):
        events = [failed("server error")] * 3 + [used("Edit"), failed("server error"), finished()]
        self.assertEqual(self.run_task(FakeSession(events)), 0)
        self.assertEqual(self.slept, [30, 60, 120, 30])

    def test_a_crashed_claude_is_restarted_by_resuming_the_session(self):
        crashed, restarted = FakeSession([used("Read"), said("half way")]), FakeSession([finished()])
        self.assertEqual(self.run_task(crashed, restarted), 0)
        self.assertEqual(self.starts, [False, True])
        self.assertTrue(crashed.closed)
        self.assertEqual(restarted.sent, ["continue"])

    def test_a_crash_after_the_task_arrived_resumes(self):
        crashed, restarted = FakeSession([{"type": "system", "subtype": "init"}]), FakeSession([finished()])
        self.run_task(crashed, restarted)
        self.assertEqual((self.starts, restarted.sent), ([False, True], ["continue"]))

    def test_a_later_restart_that_fails_to_start_is_retried(self):
        sessions = FakeSession([used("Read")]), FakeSession([]), FakeSession([finished()])
        self.assertEqual(self.run_task(*sessions), 0)
        self.assertEqual(self.starts, [False, True, True])

    def test_crashing_again_and_again_gives_up_even_with_progress(self):
        sessions = [FakeSession([used("Read")]) for _ in range(runner.MAX_RESTARTS + 2)]
        self.assertEqual(self.run_task(*sessions), 1)
        self.assertIn("restarts in a row", self.notified())

    def test_a_silent_claude_is_restarted(self):
        silent, restarted = FakeSession([used("Bash", command="make"), IDLE]), FakeSession([finished()])
        self.assertEqual(self.run_task(silent, restarted), 0)
        self.assertEqual(self.starts, [False, True])

    def test_claude_that_cannot_start_fails_fast(self):
        self.assertEqual(self.run_task(FakeSession([])), 1)
        self.assertEqual(self.slept, [])
        self.assertIn("could not start", self.notified())

    def test_resuming_starts_with_continue(self):
        session = FakeSession([finished()])
        self.run_task(session, task=None)
        self.assertEqual((self.starts, session.sent), ([True], ["continue"]))

    def test_a_failing_check_is_logged_and_the_run_carries_on(self):
        self.engine_tick.side_effect = OSError("disk busy")
        self.assertEqual(self.run_task(FakeSession([api_error("rate_limit"), failed("limit"), finished()])), 0)
        self.assertEqual(self.slept, [30])
        self.assertTrue(any("check failed: OSError" in line for line in log.tail(self.root, 10)))

    def test_an_unexpected_error_is_notified_and_raised(self):
        session = FakeSession([])
        session.next_event = mock.Mock(side_effect=RuntimeError("broken pipe"))
        with self.assertRaises(RuntimeError):
            self.run_task(session)
        self.assertIn("stopped by an error", self.notified())

    def test_ctrl_c_closes_the_session_without_notifying(self):
        session = FakeSession([])
        session.next_event = mock.Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_task(session)
        self.assertTrue(session.closed)
        self.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
