"""Offline regressions for the v0.14.0 review of the Codex maintenance branch.

Each test pins one review finding (historian + Astra, 2026-09-08). No agent
subprocesses are started; live adapter entry points are patched to fail.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-review-0140-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = TEST_HOME.name  # before imports with startup side effects

from claude_squared import __main__ as CLI  # noqa: E402
from claude_squared import async_tasks as A, codex_models as CM, registry as R, server as S  # noqa: E402
from claude_squared import runtime as RT  # noqa: E402
from claude_squared.adapters import codex as C  # noqa: E402
from claude_squared.adapters.claude import ClaudeAdapter  # noqa: E402
from claude_squared.errors import PairError  # noqa: E402
from claude_squared.models import AsyncTaskState, ContextStatus, PairSpec, SendResult  # noqa: E402
from claude_squared.runtime import PairRuntime, TurnLogScope  # noqa: E402


def tool(value):
    return getattr(value, "fn", value)


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=TEST_HOME.name))
        for context in (
            patch.dict(os.environ, {"CLAUDE_HOME": str(self.directory)}),
            patch.object(CM, "codex_home", return_value=self.directory / "codex"),
            patch.object(C, "codex_home", return_value=self.directory / "codex"),
            patch.object(ClaudeAdapter, "_run_print", side_effect=AssertionError("live Claude call")),
            patch.object(C.CodexAdapter, "_run", side_effect=AssertionError("live Codex call")),
        ):
            context.start()
            self.addCleanup(context.stop)

    def claude_runtime(self, name="pair"):
        spec = PairSpec(name=name, session_id="s", cwd=str(self.directory))
        R.add_pair(spec)
        rt = PairRuntime(spec, ClaudeAdapter())
        rt._drift_checked = True
        return rt

    @staticmethod
    def result(text="completed"):
        return SendResult(name="pair", session_id="s", model_used="opus", response=text, duration_ms=1)

    @staticmethod
    def fake_proc(writes):
        return SimpleNamespace(stdin=SimpleNamespace(write=writes.append, flush=lambda: None),
                               poll=lambda: None, pid=0)


class InterruptOnceTests(ReviewCase):
    """Astra P1: pair_stop's direct interrupt and the worker's should_stop poll
    both wrote an interrupt, and one landing after the result could cancel the
    next self-woken turn."""

    def test_claim_interrupt_states(self):
        rt = self.claude_runtime()
        self.assertIsNone(rt._claim_interrupt())  # nothing open
        scope = TurnLogScope(rt.main_log_path, 1)
        rt._current_scope = scope
        self.assertTrue(rt._claim_interrupt())  # first claim writes
        self.assertFalse(rt._claim_interrupt())  # second caller only waits
        scope.result_snapshot = {"end_line": 1}
        self.assertIsNone(rt._claim_interrupt())  # result attributed: nothing to interrupt

    def test_worker_and_pair_stop_write_one_interrupt(self):
        rt = self.claude_runtime()
        writes: list = []
        rt.proc = self.fake_proc(writes)
        rt._current_scope = TurnLogScope(rt.main_log_path, 1)
        rt._write_interrupt_nowait()  # the worker's should_stop path
        rt._write_interrupt_nowait()
        self.assertEqual(len(writes), 1)
        # pair_stop's path: nothing more is written; it just waits for the ack.
        self.assertFalse(rt.send_interrupt(wait_for_result_seconds=0.05))
        self.assertEqual(len(writes), 1)

    def test_no_interrupt_once_the_result_is_attributed(self):
        rt = self.claude_runtime()
        writes: list = []
        rt.proc = self.fake_proc(writes)
        scope = TurnLogScope(rt.main_log_path, 1)
        scope.result_snapshot = {"end_line": 1}
        rt._current_scope = scope
        rt._write_interrupt_nowait()
        # A finished turn counts as acknowledged so pair_stop does not escalate.
        self.assertTrue(rt.send_interrupt(wait_for_result_seconds=0.05))
        self.assertEqual(writes, [])


class ReaderLockTests(ReviewCase):
    """Historian H1: the reader thread must never wait on the cross-process
    control FileLock."""

    def test_implicit_open_ignores_a_held_control_lock(self):
        rt = self.claude_runtime()
        lock = A.control_lock("pair")
        lock.acquire(timeout=5)  # another thread/process holds it (e.g. pair_stop)
        self.addCleanup(lock.release)
        done = threading.Event()

        def open_turn():
            rt._open_implicit_turn()
            done.set()

        threading.Thread(target=open_turn, daemon=True).start()
        self.assertTrue(done.wait(2), "reader blocked on the control lock")
        self.assertIsNotNone(rt._implicit_scope)
        self.assertIsNotNone(rt._implicit_task)


class StopSelectionTests(ReviewCase):
    def test_force_stop_reaches_an_untracked_implicit_turn(self):
        # Astra P2: registration failed → open implicit scope, no task identity,
        # a queued send behind it → force must still tear the runtime down.
        rt = self.claude_runtime()
        rt.is_alive = lambda: True
        stopped = threading.Event()
        rt.stop = lambda: (stopped.set(), "killed")[1]
        rt._implicit_scope = TurnLogScope(rt.main_log_path, 1)
        rt._implicit_task = None
        RT.registry()._runtimes["pair"] = rt
        self.addCleanup(RT.registry()._runtimes.pop, "pair", None)
        release = threading.Event()
        self.addCleanup(release.set)
        A.start_task("pair", "queued send", lambda tid: release.wait(5) and self.result(), queued=True)
        out = tool(S.pair_stop)("pair", force=True)
        self.assertIn("tree-killed", out)
        self.assertTrue(stopped.is_set())

    def test_untagged_codex_process_is_never_killed(self):
        # Astra P1: an untagged in-flight process is a create/fork/clear probe
        # that may be the selected turn's successor.
        R.add_pair(PairSpec(name="pair", backend="codex", model="gpt-5.6-sol", session_id="s",
                            cwd=str(self.directory)))
        killed: list = []
        with patch.object(C, "_tree_kill", side_effect=lambda proc: killed.append(proc) or "killed"):
            with C._INFLIGHT_LOCK:
                C._INFLIGHT["pair"] = {"proc": "probe", "task_id": None, "started_at": datetime.utcnow()}
            self.addCleanup(lambda: C._INFLIGHT.pop("pair", None))
            release = threading.Event()
            self.addCleanup(release.set)
            task = A.start_task("pair", "turn", lambda tid: release.wait(5) and self.result(), queued=False)
            out = tool(S.pair_stop)("pair")
            self.assertIn("cancellation requested for executing task", out)
            self.assertEqual(killed, [])
            with C._INFLIGHT_LOCK:
                C._INFLIGHT["pair"]["task_id"] = task.task_id
            tool(S.pair_stop)("pair")
            self.assertEqual(killed, ["probe"])

    def _foreign(self, task_id, *, queued):
        now = datetime.utcnow()
        state = AsyncTaskState(task_id=task_id, pair_name="pair", message="turn", status="running",
                               started_at=now, owner_pid=os.getppid(), queued=queued,
                               execution_started_at=None if queued else now)
        A._save(state)
        return state

    def test_foreign_executing_task_gets_the_compatibility_marker(self):
        # Historian H2 / Astra P1: a pre-0.14 worker polls only the per-pair marker.
        R.add_pair(PairSpec(name="pair", session_id="s", cwd=str(self.directory)))
        self._foreign("foreign-exec", queued=False)
        out = tool(S.pair_stop)("pair")
        self.assertIn("stop marker written", out)
        marker = json.loads(S._stop_marker_path("pair").read_text(encoding="utf-8"))
        self.assertGreater(float(marker["requested_at"]), 0.0)
        self.assertTrue((A.async_dir() / "foreign-exec.cancel").exists())

    def test_foreign_queued_task_gets_no_marker(self):
        R.add_pair(PairSpec(name="pair", session_id="s", cwd=str(self.directory)))
        self._foreign("foreign-queued", queued=True)
        out = tool(S.pair_stop)("pair", drain_queue=True)
        self.assertIn("cancellation requested for queued task", out)
        self.assertNotIn("stop marker written", out)
        self.assertFalse(S._stop_marker_path("pair").exists())


class BookkeepingTests(ReviewCase):
    def test_backend_reply_survives_registry_bookkeeping_failure(self):
        # Astra P2: the registry stats update raised AFTER the backend replied
        # and the reply was lost.
        R.add_pair(PairSpec(name="pair", backend="codex", model="gpt-5.6-sol", session_id="s",
                            cwd=str(self.directory)))
        adapter = SimpleNamespace(send=lambda current, message, **kw: self.result("the reply"))
        with patch.object(S, "_adapter_for", return_value=adapter), \
                patch.object(S.reg_mod, "update_pair", side_effect=PairError("refusing to write")):
            runner = S._build_send_runner("pair", "hello", hard_timeout_seconds=5,
                                          override_model=None, override_effort=None,
                                          override_permission_mode=None)
            result = runner(None)
        self.assertEqual(result.response, "the reply")
        self.assertTrue(any("bookkeeping failed" in note for note in result.notes), result.notes)


class PersistenceRetryTests(ReviewCase):
    def test_terminal_write_failure_is_retried_in_background(self):
        # Historian M1 / Astra P2: with no local reader, the on-disk state stayed
        # "running" forever for every other process.
        original = A._save
        failures = {"n": 0}

        def flaky(state):
            if state.status != "running" and failures["n"] < 2:
                failures["n"] += 1
                raise PermissionError("terminal write blocked")
            original(state)

        with patch.object(A, "_save", side_effect=flaky), \
                patch.object(A, "_PERSIST_RETRY_DELAYS", (0.05, 0.05, 0.05, 0.05)):
            task = A.start_task("pair", "m", lambda tid: self.result())
            self.assertTrue(A._get_or_create_event(task.task_id).wait(5))
            deadline = time.monotonic() + 5
            data = {}
            while time.monotonic() < deadline:
                data = json.loads(A._task_path(task.task_id).read_text(encoding="utf-8"))
                if data.get("status") == "done":
                    break
                time.sleep(0.05)
        self.assertEqual(data.get("status"), "done")
        self.assertNotIn(task.task_id, A._unsaved_results)
        self.assertEqual(failures["n"], 2)


class RegistryReviewTests(ReviewCase):
    def test_corrupt_copy_is_shared_across_processes(self):
        # Historian L2: one copy per distinct bad content, not one per process.
        path = R.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        R.load()
        R._CORRUPT.discard(str(path))  # a second process has no memory of the first
        R.load()
        self.assertEqual(len(list(path.parent.glob("registry.corrupt-*.json"))), 1)


class WindowFingerprintTests(ReviewCase):
    def test_discarded_turn_window_is_not_reused_after_rewind(self):
        # Astra P2: a result from a turn rewound away is newer than the retained
        # usage but its token count no longer matches.
        spec = PairSpec(name="pair", session_id="s", cwd=str(self.directory))
        path = CLI._transcript_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "type": "assistant", "timestamp": "2026-09-08T10:00:00Z",
            "message": {"model": "claude-opus-5", "usage": {"input_tokens": 1000}},
        }) + "\n", encoding="utf-8")

        def captured(task_id, tokens_used):
            A._save(AsyncTaskState(
                task_id=task_id, pair_name="pair", message="m", status="done",
                started_at=datetime.utcnow(), finished_at=datetime.utcnow(), owner_pid=os.getpid(),
                result=SendResult(
                    name="pair", session_id="s", model_used="claude-opus-5", response="r", duration_ms=1,
                    context=ContextStatus(tokens_used=tokens_used, tokens_max=1_000_000, percent=0.1,
                                          window_source="reported"),
                ),
            ))

        captured("later-discarded", 5000)
        self.assertIsNone(CLI._observed_claude_window(spec, 1000))
        captured("retained", 1000)
        self.assertEqual(CLI._observed_claude_window(spec, 1000), 1_000_000)


if __name__ == "__main__":
    unittest.main()
