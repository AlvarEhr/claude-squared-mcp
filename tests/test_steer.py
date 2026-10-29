"""Offline steering races: no model calls, all package state in a fresh home."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".test-homes"
SCRATCH.mkdir(exist_ok=True)
HOME = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="steer-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
sys.path.insert(0, str(ROOT / "src"))
from claude_squared import activity as A, async_tasks as AT, registry as R, runtime as RT, server as S
from claude_squared import _auto_watch as H
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.errors import CLIError, PairError
from claude_squared.models import PairSpec


def tool(value):
    return getattr(value, "fn", value)


class SteerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.spec = PairSpec(name="steer-" + uuid.uuid4().hex[:8], session_id=str(uuid.uuid4()), model="haiku")
        R.add_pair(self.spec)
        self.rt = RT.PairRuntime(self.spec, ClaudeAdapter())
        self.rt.proc = SimpleNamespace(stdin=io.BytesIO(), poll=lambda: None)
        self.stack.enter_context(patch.object(RT.subprocess, "Popen", side_effect=AssertionError("No live CLI")))
        self.stack.enter_context(patch.object(S.runtime_mod, "registry", return_value=SimpleNamespace(
            get_or_none=lambda name: self.rt)))
        self.scope = RT.TurnLogScope(self.rt.main_log_path, 1)
        self.scope.task_id = "existing-task"
        self.scope.input_sent = True
        self.rt._current_scope = self.scope
        self.rt._steer_supported = True
        self.rt._turn_opened(self.scope.task_id, "send")

    def frame(self):
        return json.loads(self.rt.proc.stdin.getvalue().splitlines()[-1])

    def result(self, ids=(), cost=1.0, **kwargs):
        ev = dict(type="result", subtype="success", result="final", duration_ms=100,
                  total_cost_usd=cost, user_message_uuids=list(ids), **kwargs)
        raw = json.dumps(ev)
        if self.rt._on_event_for_log(raw) is not False:
            self.rt._stdout_q.put(raw)
        return ev

    def test_tool_reuses_task_and_activity_window(self):
        with patch.object(AT, "start_task", side_effect=AssertionError("Must not start a second task")):
            reply = tool(S.pair_send_async)(self.spec.name, "new direction", steer=True)
        self.assertIn("Async task: existing-task", reply)
        self.assertIn("continuing the existing task", reply)
        self.assertIn("pair_poll('existing-task'", reply)
        self.assertEqual(self.frame()["message"]["content"], "new direction")
        self.assertEqual(self.scope.pending_steers, {self.frame()["uuid"]})
        st = self.rt._activity._state
        self.assertEqual(st["turn"]["task_id"], "existing-task")
        self.assertEqual(st["turn"]["steer_count"], 1)
        self.assertEqual(st["window_task_id"], "existing-task")
        self.assertEqual(st["window_turns"], [])
        self.result([self.frame()["uuid"]])
        self.assertIsNone(st["turn"])
        self.assertEqual(st["window_turns"][0]["steer_count"], 1)

    def test_late_steer_holds_original_result_and_aggregates_cost(self):
        self.rt.steer("follow-up")
        mid = self.frame()["uuid"]
        self.result(["original"], permission_denials=[{"tool_name": "one"}])
        self.assertTrue(self.rt._stdout_q.empty())
        self.assertIsNone(self.scope.result_snapshot)
        self.assertIsNotNone(self.rt._activity._state["turn"])
        self.result([mid], cost=1.5, permission_denials=[{"tool_name": "two"}])
        self.assertEqual(self.rt._stdout_q.qsize(), 1)
        self.assertEqual(self.scope.result_snapshot["turn_cost_usd"], 1.5)
        self.assertEqual(self.scope.result_snapshot["duration_ms"], 200)
        self.assertEqual(len(self.scope.result_snapshot["permission_denials"]), 2)
        self.assertEqual(len(self.rt._activity._state["window_turns"]), 1)

    def test_steer_handle_arms_existing_auto_watch_protocol(self):
        self.scope.task_id = str(uuid.uuid4())
        reply = tool(S.pair_send_async)(self.spec.name, "follow-up", steer=True)
        payload = {"hook_event_name": "PostToolUse", "session_id": "manager",
                   "tool_name": "mcp__pair__pair_send_async",
                   "tool_input": {"name": self.spec.name, "message": "follow-up", "steer": True},
                   "tool_response": reply}
        with patch.dict(os.environ, {"CLAUDE_SQUARED_PAIR": ""}):
            self.assertEqual(H._target(payload, R.pairs_dir()), ("manager", self.spec.name))

    def test_multiple_steers_wait_for_all_consumed_ids(self):
        self.rt.steer("first")
        first = self.frame()["uuid"]
        self.rt.steer("second")
        second = self.frame()["uuid"]
        self.result([first])
        self.assertEqual(self.scope.pending_steers, {second})
        self.assertIsNone(self.scope.result_snapshot)
        self.result([second], cost=2.0)
        self.assertFalse(self.scope.pending_steers)
        self.assertIsNotNone(self.scope.result_snapshot)

    def test_result_before_stdin_flush_does_not_release_scope(self):
        original = self.rt.proc.stdin
        rt = self.rt
        class RacingPipe:
            def write(inner, data):
                self.result(["original"])
                self.assertIsNone(self.scope.result_snapshot)
                return original.write(data)
            def flush(inner):
                return original.flush()
        rt.proc.stdin = RacingPipe()
        task, _ = rt.steer("racing input")
        self.assertEqual(task, "existing-task")
        self.assertEqual(len(self.scope.pending_steers), 1)

    def test_finished_stopping_unstarted_and_capped_turns_do_not_write(self):
        for field, value in (("result_snapshot", {}), ("interrupt_sent", True),
                             ("input_sent", False), ("steer_count", 63)):
            before = getattr(self.scope, field)
            setattr(self.scope, field, value)
            with self.subTest(field=field):
                self.assertIsNone(self.rt.steer("don't inject")[0])
                self.assertEqual(self.rt.proc.stdin.getvalue(), b"")
            setattr(self.scope, field, before)

    def test_subagent_correlation_does_not_enable_support(self):
        self.rt._steer_supported = False
        ev = {"type": "assistant", "parent_tool_use_id": "child", "user_message_uuids": ["id"]}
        self.rt._on_event_for_log(json.dumps(ev))
        self.assertFalse(self.rt._steer_supported)
        ev["parent_tool_use_id"] = None
        self.rt._on_event_for_log(json.dumps(ev))
        self.assertTrue(self.rt._steer_supported)

    def test_fallbacks_are_explicit_and_start_one_queued_task(self):
        for mode in ("codex", "foreign", "old-cli", "idle", "self-woken"):
            with self.subTest(mode=mode), ExitStack() as stack:
                if mode == "codex":
                    stack.enter_context(patch.object(S.reg_mod, "get_pair", return_value=SimpleNamespace(backend="codex")))
                elif mode == "foreign":
                    stack.enter_context(patch.object(S.runtime_mod, "registry", return_value=SimpleNamespace(get_or_none=lambda name: None)))
                elif mode == "old-cli":
                    stack.enter_context(patch.object(self.rt, "_steer_supported", False))
                else:
                    stack.enter_context(patch.object(self.rt, "_current_scope", None))
                    if mode == "self-woken":
                        stack.enter_context(patch.object(self.rt, "_implicit_scope", self.scope))
                stack.enter_context(patch.object(S, "_build_send_runner", return_value=Mock()))
                start = stack.enter_context(patch.object(AT, "start_task", return_value=SimpleNamespace(task_id="queued-task")))
                reply = tool(S.pair_send_async)(self.spec.name, "follow-up", steer=True)
                self.assertIn("queued as a normal send", reply)
                self.assertIn("Async task: queued-task", reply)
                self.assertTrue(start.call_args.kwargs["queued"])
                self.assertEqual(self.rt.proc.stdin.getvalue(), b"")

    def test_override_and_timeout_rejected_without_delivery(self):
        for name, value in (("override_model", "haiku"), ("override_effort", "low"),
                            ("override_permission_mode", "auto"), ("hard_timeout_seconds", 30)):
            with self.subTest(name=name), self.assertRaises(PairError):
                tool(S.pair_send_async)(self.spec.name, "follow-up", steer=True, **{name: value})
        self.assertEqual(self.rt.proc.stdin.getvalue(), b"")

    def test_empty_steer_is_rejected(self):
        with self.assertRaises(PairError):
            tool(S.pair_send_async)(self.spec.name, " \n", steer=True)
        self.assertFalse(self.scope.pending_steers)

    def test_write_failure_never_requeues_and_is_visible_to_worker(self):
        self.rt.proc.stdin = Mock(write=Mock(side_effect=BrokenPipeError("closed")))
        with patch.object(AT, "start_task", side_effect=AssertionError("No retry")), self.assertRaises(CLIError):
            tool(S.pair_send_async)(self.spec.name, "follow-up", steer=True)
        self.assertEqual(self.scope.steer_error, "closed")

    def test_ended_turn_between_reservation_and_write_is_not_delivered(self):
        pipe = self.rt.proc.stdin
        def retire(line):
            self.rt._reader_gen += 1
            self.rt.proc = None
        with patch.object(self.rt, "_append_main_log_line", side_effect=retire), \
                patch.object(AT, "start_task", side_effect=AssertionError("No automatic resend")), \
                self.assertRaises(CLIError) as caught:
            tool(S.pair_send_async)(self.spec.name, "follow-up", steer=True)
        self.assertIn("Turn ended before the follow-up was delivered; send it normally", str(caught.exception))
        self.assertNotIn("pair_stop", str(caught.exception))
        self.assertEqual(pipe.getvalue(), b"")
        self.assertFalse(self.scope.pending_steers)

    def test_retired_runtime_write_failure_reports_undelivered(self):
        def fail(data):
            self.rt._reader_gen += 1
            raise BrokenPipeError("retired")
        self.rt.proc.stdin = Mock(write=Mock(side_effect=fail), closed=False)
        with self.assertRaises(CLIError) as caught:
            self.rt.steer("follow-up")
        self.assertIn("send it normally", str(caught.exception))
        self.assertFalse(self.scope.pending_steers)

    def test_retirement_after_write_is_uncertain_not_success(self):
        def retire():
            self.rt._reader_gen += 1
        self.rt.proc.stdin = Mock(flush=Mock(side_effect=retire), closed=False)
        with self.assertRaises(CLIError) as caught:
            self.rt.steer("follow-up")
        self.assertIn("delivery uncertain", str(caught.exception))
        self.assertNotIn("send it normally", str(caught.exception))

    def _stop_task(self):
        task = AT.register_external_task(self.spec.name, "ordinary send")
        self.scope.task_id = task.task_id
        self.addCleanup(AT.finalize_external_task, task, status="stopped")
        return task

    def _fake_hard_stop(self, **kwargs):
        self.rt.proc = None
        self.rt.last_lost_background = [{"task_id": "background", "kind": "bash"}]
        return "tree-killed"

    def test_pair_stop_reports_worker_hard_stop_and_background_loss(self):
        self._stop_task()
        self.rt.steer("pending")
        def interrupt(data):
            self.assertEqual(json.loads(data)["request"]["subtype"], "interrupt")
            self.result(["original"], is_error=True)
            self.rt._stop_for_steer(self.scope, "unconsumed input")
        self.rt.proc.stdin = Mock(write=Mock(side_effect=interrupt))
        with patch.object(self.rt, "stop", side_effect=self._fake_hard_stop) as stop:
            reply = tool(S.pair_stop)(self.spec.name)
        self.assertIn("hard stop: tree-killed", reply)
        self.assertIn("1 background task(s) lost", reply)
        self.assertNotIn("pair stays alive", reply)
        self.assertNotIn("did not ack", reply)
        stop.assert_called_once()

    def test_pair_stop_reports_discard_completed_before_runtime_inspection(self):
        self._stop_task()
        self.rt.steer("pending")
        def cancel(task_id):
            self.scope.interrupt_sent = True
            self.result(["original"], is_error=True)
            self.rt._stop_for_steer(self.scope, "unconsumed input")
            return True
        with patch.object(self.rt, "stop", side_effect=self._fake_hard_stop), \
                patch.object(AT, "request_task_stop", side_effect=cancel):
            reply = tool(S.pair_stop)(self.spec.name)
        self.assertIn("hard stop: tree-killed", reply)
        self.assertIn("1 background task(s) lost", reply)
        self.assertNotIn("runtime left alive", reply)

    def test_interrupt_reports_pending_hard_stop_without_claiming_completion(self):
        self.rt.steer("pending")
        self.scope.interrupt_sent = True
        self.result(["original"], is_error=True)
        report = self.rt.send_interrupt(wait_for_result_seconds=.01)
        self.assertIn("hard stop in progress", report)
        self.assertNotIn("tree-killed", report)
        self.assertIsNone(self.rt.steer_stop_report({"other-task"}))

    def test_interrupt_with_consumed_steer_preserves_soft_stop(self):
        self.rt.steer("pending")
        mid = self.frame()["uuid"]
        self.rt.proc.stdin = Mock(write=Mock(side_effect=lambda data: self.result([mid], is_error=True)))
        with patch.object(self.rt, "stop", side_effect=AssertionError("No hard stop")):
            self.assertIs(self.rt.send_interrupt(wait_for_result_seconds=.1), True)
        self.assertIsNone(self.rt.steer_stop_report({self.scope.task_id}))

    def test_interrupt_with_unconsumed_steer_requires_discarding_input(self):
        self.rt.steer("pending")
        self.scope.interrupt_sent = True
        self.result(["original"])
        self.assertTrue(self.scope.result_snapshot["steer_abort"])
        self.assertEqual(self.rt._stdout_q.qsize(), 1)

    def test_interrupt_waiting_for_stdin_never_targets_successor(self):
        waiting = threading.Event()
        release = threading.Event()
        class DelayedLock:
            def __enter__(inner):
                waiting.set()
                self.assertTrue(release.wait(3))
            def __exit__(inner, *args):
                pass
        self.rt._input_lock = DelayedLock()
        for interrupt in (self.rt.send_interrupt, self.rt._write_interrupt_nowait):
            waiting.clear()
            release.clear()
            self.rt._current_scope = self.scope
            thread = threading.Thread(target=interrupt)
            thread.start()
            self.assertTrue(waiting.wait(3))
            successor = RT.TurnLogScope(self.rt.main_log_path, 2)
            successor.task_id = "successor"
            self.rt._current_scope = successor
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(successor.interrupt_sent)
        self.assertEqual(self.rt.proc.stdin.getvalue(), b"")

    def test_real_send_loop_waits_for_steer_result(self):
        self.rt._current_scope = None
        self.rt._activity.turn_closed("existing-task")
        finished = queue.Queue()
        def run():
            try:
                finished.put(self.rt.send("original", timeout_seconds=5, task_id=None))
            except Exception as exc:
                finished.put(exc)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 3
        while not (self.rt._current_scope and self.rt._current_scope.input_sent) and time.monotonic() < deadline:
            time.sleep(.01)
        self.scope = self.rt._current_scope
        self.assertIsNotNone(self.scope)
        self.scope.task_id = "existing-task"
        self.rt.steer("new plan")
        mid = self.frame()["uuid"]
        self.result(["original"])
        self.assertTrue(finished.empty())
        self.result([mid], cost=1.25)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        out = finished.get_nowait()
        self.assertIsInstance(out, dict)
        self.assertEqual(out["_turn_cost_usd"], 1.25)
        self.assertEqual(out["duration_ms"], 200)
        self.assertIsNone(self.rt._current_scope)

    def test_send_loop_discards_pending_input_on_interrupt_or_error(self):
        for interrupted in (True, False):
            with self.subTest(interrupted=interrupted), patch.object(self.rt, "stop") as stop:
                self.rt._current_scope = None
                finished = queue.Queue()
                def run():
                    try:
                        finished.put(self.rt.send("original", timeout_seconds=5))
                    except Exception as exc:
                        finished.put(exc)
                thread = threading.Thread(target=run)
                thread.start()
                deadline = time.monotonic() + 3
                while not (self.rt._current_scope and self.rt._current_scope.input_sent) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.scope = self.rt._current_scope
                self.assertIsNotNone(self.scope)
                self.scope.task_id = "existing-task"
                self.rt.steer("pending")
                self.scope.interrupt_sent = interrupted
                self.result(["original"], is_error=not interrupted)
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(finished.get_nowait(), CLIError)
                stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
