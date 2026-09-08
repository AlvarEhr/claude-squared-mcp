"""Deterministic offline cancellation races; no agent subprocesses are started."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-async-review-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = TEST_HOME.name  # before imports with startup side effects

from claude_squared import async_tasks as A, codex_models as CM, registry as R, server as S
from claude_squared.adapters import codex as C
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.errors import TaskStopped
from claude_squared.models import PairSpec, SendResult
from claude_squared.runtime import PairRuntime, RuntimeRegistry, TurnLogScope


def tool(value):
    return getattr(value, "fn", value)


class AsyncReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=TEST_HOME.name))
        self.threads = []
        self.errors = []
        for context in (
            patch.dict(os.environ, {"CLAUDE_HOME": str(self.directory)}),
            patch.object(CM, "codex_home", return_value=self.directory / "codex"),
            patch.object(C, "codex_home", return_value=self.directory / "codex"),
            patch.object(ClaudeAdapter, "_run_print", side_effect=AssertionError("live Claude call")),
            patch.object(C.CodexAdapter, "_run", side_effect=AssertionError("live Codex call")),
        ):
            context.start()
            self.addCleanup(context.stop)

    def thread(self, fn):
        def run():
            try:
                fn()
            except BaseException as exc:
                self.errors.append(exc)
        worker = threading.Thread(target=run, daemon=True)
        self.threads.append(worker)
        worker.start()
        return worker

    def join(self):
        for worker in self.threads:
            worker.join(5)
            self.assertFalse(worker.is_alive(), "control operation deadlocked")
        if self.errors:
            raise self.errors[0]

    def result(self, text="completed"):
        return SendResult(name="pair", session_id="s", model_used="opus", response=text, duration_ms=1)

    def send_runner(self, message):
        return S._build_send_runner("pair", message, hard_timeout_seconds=5,
            override_model=None, override_effort=None, override_permission_mode=None)

    def test_control_lock_is_cached_and_reentrant(self):
        lock = A.control_lock("pair")
        self.assertIs(lock, A.control_lock("pair"))
        with lock:
            with A.control_lock("pair"):
                pass

    def test_terminal_marker_does_not_cancel_a_waiter_after_promotion(self):
        from claude_squared.models import AsyncTaskState
        state = AsyncTaskState(task_id="waiting", pair_name="pair", message="next", status="running",
                               queued=True, owner_pid=os.getpid(), started_at=datetime.utcnow())
        A._save(state)
        checker = S._make_stop_checker("pair", state.task_id)
        marker_time = datetime.now(timezone.utc)
        marker = S._stop_marker_path("pair")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"requested_at": marker_time.timestamp()}))
        self.assertFalse(checker())
        self.assertTrue(marker.exists())
        state.queued = False
        state.execution_started_at = (marker_time + timedelta(seconds=1)).replace(tzinfo=None)
        A._save(state)
        self.assertFalse(checker())
        self.assertFalse(A.was_stopped(state.task_id))

    def test_task_cancellation_checker_signals_once(self):
        checker = S._make_stop_checker("pair", "specific-task")
        A.mark_task_stopped("specific-task")
        try:
            self.assertTrue(checker())
            self.assertFalse(checker())
        finally:
            A._clear_stopped("specific-task")

    def test_force_eviction_never_stops_a_replacement_runtime(self):
        registry = RuntimeRegistry()
        old_stop, new_stop = threading.Event(), threading.Event()
        old = SimpleNamespace(stop=old_stop.set)
        replacement = SimpleNamespace(stop=new_stop.set)
        registry._runtimes["pair"] = replacement
        self.assertFalse(registry.evict_if_current("pair", old))
        self.assertFalse(new_stop.is_set())
        self.assertTrue(registry.evict_if_current("pair", replacement))
        self.assertTrue(new_stop.is_set())
        self.assertFalse(old_stop.is_set())

    def test_stop_cannot_interrupt_a_promoted_successor(self):
        R.add_pair(PairSpec(name="pair", backend="codex", model="gpt-5.6-sol", session_id="s"))
        entered = threading.Event()
        cancel_seen = threading.Event()
        successor_attempt = threading.Event()
        successor_started = threading.Event()
        allow_completion = threading.Event()
        self.addCleanup(allow_completion.set)
        calls = []
        first_proc = object()
        original_promote = A.mark_task_executing

        class Adapter:
            def send(inner, current, message, **kw):
                calls.append(message)
                if message == "first":
                    entered.set()
                    if not allow_completion.wait(5):
                        raise AssertionError("first task was never released")
                    if kw["should_stop"]():
                        cancel_seen.set()
                        raise TaskStopped("first cancelled")
                successor_started.set()
                return self.result(message)

        def promote(tid):
            if tid == second.task_id:
                successor_attempt.set()
            original_promote(tid)

        runtime = SimpleNamespace(get_or_none=lambda name: None)
        with patch.object(S, "_adapter_for", return_value=Adapter()), \
             patch.object(S.runtime_mod, "registry", return_value=runtime):
            first = A.start_task("pair", "first", self.send_runner("first"), queued=True)
            self.assertTrue(entered.wait(5))
            second = A.start_task("pair", "second", self.send_runner("second"), queued=True)
            first_finished = A._get_or_create_event(first.task_id)

            def stop_process(proc):
                self.assertIs(proc, first_proc)
                allow_completion.set()
                self.assertTrue(cancel_seen.wait(5))
                self.assertTrue(first_finished.wait(5))
                self.assertTrue(successor_attempt.wait(5))
                self.assertFalse(successor_started.is_set())
                return "already-exited"

            with patch.object(A, "mark_task_executing", side_effect=promote), \
                 patch.object(C, "inflight_info", return_value={"task_id": first.task_id, "proc": first_proc}), \
                 patch.object(C, "_tree_kill", side_effect=stop_process):
                tool(S.pair_stop)("pair")
            done = A.wait_for_task(second.task_id, 5)
        self.assertEqual(done.status, "done")
        self.assertEqual(calls, ["first", "second"])

    def test_cancel_publication_serializes_with_promotion(self):
        ready = threading.Event()
        promote_now = threading.Event()
        publishing = threading.Event()
        finish_publish = threading.Event()
        promotion_attempt = threading.Event()
        backend_called = threading.Event()
        for event in (promote_now, finish_publish):
            self.addCleanup(event.set)

        def runner(tid):
            ready.set()
            if not promote_now.wait(5):
                raise AssertionError("promotion not released")
            promotion_attempt.set()
            A.mark_task_executing(tid)
            backend_called.set()
            return self.result()

        task = A.start_task("pair", "queued", runner, queued=True)
        self.assertTrue(ready.wait(5))
        # Simulate the remote requester, which has no local in-memory stop flag.
        task.owner_pid = os.getppid()
        A._save(task)
        original_replace = Path.replace

        def replace(path, target):
            if str(target).endswith(".cancel"):
                publishing.set()
                if not finish_publish.wait(5):
                    raise AssertionError("cancel publication not released")
            return original_replace(path, target)

        with patch.object(Path, "replace", replace):
            self.thread(lambda: A.request_task_stop(task.task_id))
            self.assertTrue(publishing.wait(5))
            promote_now.set()
            self.assertTrue(promotion_attempt.wait(5))
            self.assertTrue(A.load_task(task.task_id).queued)
            self.assertFalse(backend_called.is_set())
            finish_publish.set()
            self.join()
            done = A.wait_for_task(task.task_id, 5)
        self.assertEqual(done.status, "stopped")
        self.assertIsNone(done.execution_started_at)
        self.assertFalse(backend_called.is_set())

    def test_local_force_stop_survives_cancel_file_failure(self):
        R.add_pair(PairSpec(name="pair", session_id="s"))
        entered, killed = threading.Event(), threading.Event()
        self.addCleanup(killed.set)

        def runner(tid):
            entered.set()
            if not killed.wait(5):
                raise AssertionError("local process was not killed")
            raise TaskStopped("killed")

        task = A.start_task("pair", "running", runner)
        self.assertTrue(entered.wait(5))
        runtime = SimpleNamespace(is_alive=lambda: True, active_task_ids=lambda: {task.task_id})
        registry = SimpleNamespace(get_or_none=lambda name: runtime,
            evict_if_current=lambda name, expected: killed.set() or True)
        original_write = Path.write_text

        def write(path, *args, **kwargs):
            if ".cancel." in path.name:
                raise PermissionError("injected cancel write failure")
            return original_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", write), patch.object(S.runtime_mod, "registry", return_value=registry):
            response = tool(S.pair_stop)("pair", force=True)
            done = A.wait_for_task(task.task_id, 5)
        self.assertIn("could not persist cancellation", response)
        self.assertIn("tree-killed", response)
        self.assertEqual(done.status, "stopped")

    def test_cancellation_read_failure_does_not_strand_worker(self):
        with patch.object(A, "cancellation_requested", side_effect=PermissionError("unreadable")):
            task = A.start_task("pair", "read failure", lambda tid: self.result())
            done = A.wait_for_task(task.task_id, 5)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.result.response, "completed")

    def test_finalization_needs_no_metadata_read(self):
        promoted, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def runner(tid):
            A.mark_task_executing(tid)
            promoted.set()
            if not finish.wait(5):
                raise AssertionError("completion not released")
            return self.result()

        task = A.start_task("pair", "metadata read failure", runner, queued=True)
        self.assertTrue(promoted.wait(5))
        event = A._get_or_create_event(task.task_id)
        with patch.object(A, "load_task", side_effect=PermissionError("metadata unreadable")):
            finish.set()
            self.assertTrue(event.wait(5))
        done = A.load_task(task.task_id)
        self.assertEqual(done.status, "done")
        self.assertFalse(done.queued)
        self.assertIsNotNone(done.execution_started_at)
        self.assertEqual(done.result.response, "completed")

    def test_final_write_failure_keeps_local_result_and_recovers(self):
        original_save = A._save

        def save(state):
            if state.status != "running":
                raise PermissionError("terminal write blocked")
            original_save(state)

        with patch.object(A, "_save", side_effect=save):
            task = A.start_task("pair", "write failure", lambda tid: self.result())
            self.assertTrue(A._get_or_create_event(task.task_id).wait(5))
            done = A.wait_for_task(task.task_id, 5)
            self.assertEqual(done.status, "done")
            self.assertEqual(done.result.response, "completed")
        recovered = A.load_task(task.task_id)
        self.assertEqual(recovered.status, "done")
        self.assertNotIn(task.task_id, A._unsaved_results)
        self.assertEqual(json.loads(A._task_path(task.task_id).read_text())["status"], "done")

    def test_foreign_implicit_handoff_leaves_waiting_send_intact(self):
        R.add_pair(PairSpec(name="pair", session_id="s"))
        implicit = A.register_external_task("pair", A.SELF_WOKEN_MESSAGE)
        implicit.owner_pid = os.getppid()
        A._save(implicit)
        waiting = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        def foreign_ids(name):
            waiting.set()
            return [] if release.is_set() else [implicit.task_id]

        runner = self.send_runner("next")
        adapter = SimpleNamespace(send=lambda *args, **kwargs: self.result())
        registry = SimpleNamespace(get_or_none=lambda name: None)
        with patch.object(S, "_foreign_self_woken_task_ids", side_effect=foreign_ids), \
             patch.object(S, "_adapter_for", return_value=adapter), \
             patch.object(S.runtime_mod, "registry", return_value=registry), \
             patch.object(S, "_write_stop_marker", side_effect=AssertionError("pair-wide stop marker")):
            task = A.start_task("pair", "waiting", runner, queued=True)
            self.assertTrue(waiting.wait(5))
            response = tool(S.pair_stop)("pair", hard=True)
            self.assertIn("owner handoff required", response)
            self.assertIn("hard reset skipped", response)
            self.assertFalse(A.was_stopped(task.task_id))
            self.assertFalse(A.was_stopped(implicit.task_id))
            self.assertTrue(A.load_task(task.task_id).queued)
            release.set()
            done = A.wait_for_task(task.task_id, 5)
        A.finalize_external_task(implicit, status="done")
        self.assertEqual(done.status, "done")

    def test_local_implicit_wait_stays_queued_and_survives_default_stop(self):
        spec = PairSpec(name="pair", session_id="s", cwd=str(self.directory))
        R.add_pair(spec)
        adapter = ClaudeAdapter()
        runtime = PairRuntime(spec, adapter)
        runtime._drift_checked = True
        runtime._open_implicit_turn()
        implicit = runtime._implicit_task
        waiting = threading.Event()
        user_written = threading.Event()
        original_wait = runtime.wait_for_implicit_idle

        def wait(*args):
            waiting.set()
            return original_wait(*args)

        def write(payload):
            self.assertEqual(json.loads(payload)["type"], "user")
            user_written.set()
            event = {"type": "result", "subtype": "success", "result": "completed"}
            runtime._on_event_for_log(json.dumps(event))
            runtime._stdout_q.put(json.dumps(event))

        runtime.proc = SimpleNamespace(stdin=SimpleNamespace(write=write, flush=lambda: None))
        registry = SimpleNamespace(get_or_none=lambda name: runtime, get_or_start=lambda *args: runtime)

        def interrupt(**kwargs):
            self.assertFalse(user_written.is_set())
            runtime._after_result({"type": "result", "subtype": "error_during_execution", "result": "interrupted"})
            return True

        with patch.object(S.runtime_mod, "registry", return_value=registry), \
             patch.object(adapter, "session_exists", return_value=True), \
             patch.object(S, "_adapter_for", return_value=adapter), \
             patch.object(runtime, "is_alive", return_value=True), \
             patch.object(runtime, "wait_for_implicit_idle", side_effect=wait), \
             patch.object(runtime, "send_interrupt", side_effect=interrupt):
            task = A.start_task("pair", "next", self.send_runner("next"), queued=True)
            self.assertTrue(waiting.wait(5))
            self.assertTrue(A.load_task(task.task_id).queued)
            self.assertIn("no turn log", tool(S.pair_poll)(task.task_id, with_turn_log=True))
            tool(S.pair_stop)("pair")
            done = A.wait_for_task(task.task_id, 5)
        self.assertEqual(A.load_task(implicit.task_id).status, "stopped")
        self.assertEqual(done.status, "done")
        self.assertTrue(user_written.is_set())

    def test_draining_waiter_does_not_interrupt_implicit_turn(self):
        spec = PairSpec(name="pair", session_id="s", cwd=str(self.directory))
        runtime = PairRuntime(spec, ClaudeAdapter())
        runtime._implicit_scope = TurnLogScope(runtime.main_log_path, 1)
        with patch.object(runtime, "is_alive", return_value=True), \
             patch.object(runtime, "_write_interrupt_nowait") as interrupt:
            with self.assertRaises(TaskStopped):
                runtime.wait_for_implicit_idle(5, lambda: True)
        interrupt.assert_not_called()
        self.assertIsNotNone(runtime._implicit_scope)

    def test_implicit_finalization_survives_cancellation_read_failure(self):
        spec = PairSpec(name="pair", session_id="s", cwd=str(self.directory))
        R.add_pair(spec)
        runtime = PairRuntime(spec, ClaudeAdapter())
        runtime._open_implicit_turn()
        implicit = runtime._implicit_task
        with patch.object(A, "cancellation_requested", side_effect=PermissionError("unreadable")):
            runtime._after_result({"type": "result", "subtype": "success", "result": "completed"})
        self.assertTrue(runtime._implicit_done.is_set())
        self.assertEqual(A.load_task(implicit.task_id).status, "done")


if __name__ == "__main__":
    unittest.main()
