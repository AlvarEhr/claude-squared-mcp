"""Offline owner-death recovery: fake executables and isolated stores only."""

from __future__ import annotations

from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-detach-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(TEST_HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(TEST_HOME.name) / "codex")

from claude_squared import async_tasks as A, codex_turns as T, registry as R, server as S
from claude_squared.adapters import codex as C
from claude_squared import process_identity as P
from claude_squared.errors import CLIError, CommandTimeout, TaskStopped, PairError
from claude_squared.models import AsyncTaskState, PairSpec


def tool(value):
    return getattr(value, "fn", value)


EVENTS = [
    {"type": "thread.started", "thread_id": "test-thread"},
    {"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "fake"}},
    {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
                                          "command": "fake", "aggregated_output": "ok", "exit_code": 0,
                                          "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "recovered reply"}},
    {"type": "turn.completed", "usage": {"input_tokens": 40, "output_tokens": 10}},
]


class DetachTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.stack.enter_context(patch.dict(os.environ, {
            "CLAUDE_HOME": str(self.directory / "claude"), "CODEX_HOME": str(self.directory / "codex"),
        }))
        self.addCleanup(self.retire_activity)
        self.spec = PairSpec(name="detach", backend="codex", model="gpt-6-astra", effort="high",
                             permission_mode="read-only", session_id="test-thread", cwd=str(self.directory))
        R.add_pair(self.spec)
        self.adapter = C.CodexAdapter()
        self.stack.enter_context(patch.object(C, "rollout_path_for", return_value=None))
        self.stack.enter_context(patch.object(C, "resolve_codex_model", return_value=("gpt-6-astra", None)))
        self.stack.enter_context(patch.object(C, "context_windows", return_value=(1000, 2000)))
        self.stack.enter_context(patch.object(C, "guardian_verdicts_since", return_value=[]))

    def retire_activity(self):
        from claude_squared import activity
        home = str(self.directory / "claude" / "pairs")
        with activity._WRITERS_LOCK:
            writers = [activity._WRITERS.pop(key) for key in list(activity._WRITERS) if key[0] == home]
        for writer in writers:
            with writer._io_lock, writer._lock:
                writer._forgotten = True

    def task(self, events=None, *, child_pid=999999998):
        tid = str(uuid.uuid4())
        directory = R.logs_dir() / self.spec.name / "turns"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{tid}.events.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in (EVENTS if events is None else events)), encoding="utf-8")
        os.utime(path, (time.time() - 10, time.time() - 10))
        state = AsyncTaskState(task_id=tid, pair_name=self.spec.name, message="test", status="running",
                               started_at=datetime.utcnow(), owner_pid=999999999,
                               child_pid=child_pid, child_started_at=P.started_at(child_pid) or 100,
                               events_path=str(path))
        A._save(state)
        T.save_receipt(path, {
            "run_id": tid, "task_id": tid, "child_pid": child_pid, "child_started_at": state.child_started_at,
            "spec": self.spec.model_dump(mode="json"), "model_used": "gpt-6-astra",
            "permission_level": "read-only", "started_epoch": time.time() - 30,
            "log_path": str(directory.parent / "main.log"), "logged_offset": 0, "log": True,
        })
        return state, path

    def test_complete_lines_wait_for_newline_and_split_utf8(self):
        stream = io.BytesIO(b'{"text":"\xe2\x82')
        self.assertEqual(list(T.complete_lines(stream)), [])
        self.assertEqual(stream.tell(), 0)
        stream.seek(0, 2)
        stream.write(b'\xac"}\n{"bad":"\xff"}\npartial')
        stream.seek(0)
        lines = list(T.complete_lines(stream))
        self.assertIn("€", lines[0][1])
        self.assertIn("\ufffd", lines[1][1])
        self.assertEqual(stream.read(), b"partial")

    def test_recovery_replays_once(self):
        state, path = self.task()
        threads = [threading.Thread(target=A.reap_orphan, args=(state.task_id,)) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        result = A.reap_orphan(state.task_id)
        self.assertEqual(result.status, "done")
        self.assertEqual(result.result.response, "recovered reply")
        self.assertIn("recovered after owner death", result.result.notes)
        self.assertIsNotNone(result.recovered_at)
        self.assertEqual(result.recovered_by_pid, os.getpid())
        self.assertIsNone(result.child_pid)
        self.assertEqual(result.events_path, str(path))
        self.assertFalse(A.was_stopped(state.task_id))
        self.assertEqual(R.get_pair(self.spec.name).turn_count, 1)
        log = Path(result.result.log_path).read_text(encoding="utf-8")
        self.assertEqual(log.count("[text] recovered reply"), 1)
        self.assertEqual(result.result.log_line_start, 1)
        self.assertEqual(result.result.log_line_end, len(log.splitlines()))

    def test_busy_pair_skips_recovery_without_waiting(self):
        state, _ = self.task()
        with S._PairLock(self.spec.name):
            started = time.monotonic()
            self.assertEqual(A.reap_orphan(state.task_id).status, "running")
            self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(A.reap_orphan(state.task_id).status, "done")

    def test_startup_sweep_and_atexit_preserve_child(self):
        state, _ = self.task(child_pid=os.getpid())
        A._sweep_dead_predecessor_orphans()
        self.assertEqual(A.load_task(state.task_id).status, "running")
        self.assertIn("DETACHED", tool(S.pair_poll)(state.task_id))
        self.assertIn("DETACHED", tool(S.pair_status)(self.spec.name))
        state.owner_pid = os.getpid()
        A._save(state)
        A._cleanup_orphaned_running_tasks()
        self.assertEqual(A.load_task(state.task_id).status, "running")
        state.owner_pid, state.child_pid = 999999999, 999999998
        A._save(state)
        A._sweep_dead_predecessor_orphans()
        self.assertEqual(A.load_task(state.task_id).status, "done")

    def test_mtime_advance_is_liveness_evidence(self):
        state, path = self.task(events=[])
        self.assertFalse(A.codex_child_alive(state))
        path.write_text('{"type":"turn.started"}\n', encoding="utf-8")
        self.assertTrue(A.codex_child_alive(state))
        self.assertEqual(A.reap_orphan(state.task_id).status, "running")

    def test_partial_completion_is_not_success_and_orphan_text_is_backend_aware(self):
        state, path = self.task(events=[])
        path.write_bytes(json.dumps(EVENTS[-1]).encode())
        os.utime(path, (time.time() - 10, time.time() - 10))
        result = A.reap_orphan(state.task_id)
        self.assertEqual(result.status, "failed")
        self.assertIn("No completed Codex result", result.error)
        self.assertNotIn("often runs to completion", tool(S.pair_poll)(state.task_id))

    def test_replay_skips_logged_prefix_and_allocates_fresh_tags(self):
        state, path = self.task()
        receipt = T.read_receipt(path)
        # The old owner had logged a command's start; another tool consumed T-2.
        counter = C.ToolCounter(index_path=path.parent.parent / "main.idx.json")
        counter.next_id_for("item_0")
        counter.next_id_for("other")
        offset = sum(len((json.dumps(e) + "\n").encode()) for e in EVENTS[:2])
        T.append_log(path, receipt, ["old thread", "[T-1] [tool_use] fake"], offset)
        result = A.reap_orphan(state.task_id).result
        log = Path(result.log_path).read_text(encoding="utf-8")
        self.assertIn("[T-3] [tool_result]", log)
        self.assertNotIn("=== THREAD", log)
        self.assertEqual(result.log_line_start, 3)

    def test_retry_after_registry_save_does_not_double_count(self):
        state, _ = self.task()
        with patch.object(A, "_retry_persist_in_background"), patch.object(A, "_save", side_effect=OSError("disk")):
            A.reap_orphan(state.task_id)
        with A._states_guard:
            A._unsaved_results.pop(state.task_id, None)  # simulate recovering owner's death
        self.assertEqual(R.get_pair(self.spec.name).turn_count, 1)
        recovered = A.reap_orphan(state.task_id)
        self.assertEqual(recovered.status, "done")
        self.assertEqual(R.get_pair(self.spec.name).turn_count, 1)
        self.assertEqual(Path(recovered.result.log_path).read_text().count("[text] recovered reply"), 1)

    def test_pending_log_append_is_completed_after_crash(self):
        state, path = self.task()
        receipt = T.read_receipt(path)
        receipt["pending_log"] = {"offset": 0, "text": "one line\n", "events_offset": 0}
        T.save_receipt(path, receipt)
        Path(receipt["log_path"]).write_bytes(b"one ")
        T.finish_pending_log(path, receipt)
        self.assertEqual(Path(receipt["log_path"]).read_bytes(), b"one line\n")

    def test_recovery_failures_are_bounded_and_do_not_wedge_successor(self):
        state, path = self.task()
        receipt = T.read_receipt(path)
        receipt["pending_log"] = {"offset": 100, "text": "lost log\n", "events_offset": 0}
        T.save_receipt(path, receipt)  # deterministic deleted/truncated log failure
        for attempt in (1, 2, 3):
            current = A.reap_orphan(state.task_id)
            self.assertEqual(current.recovery_attempts, attempt)
            self.assertEqual(T.read_receipt(path)["recovery_attempts"], attempt)
        self.assertEqual(current.status, "failed")
        self.assertIn("recovery failed:", current.error)
        self.assertIn(str(path), current.error)
        self.assertNotIn("ORPHANED:", current.error)
        with S._PairLock(self.spec.name):
            A.wait_for_detached(self.spec.name)

    def test_unexpected_recovery_exception_and_unreadable_receipt_are_bounded(self):
        for error in (AttributeError("bad event"), TypeError("bad replay"), ValueError("old spec")):
            state, path = self.task()
            with self.subTest(error=error), patch.object(C, "recover_turn", side_effect=error), \
                    patch.object(T, "read_receipt", side_effect=OSError("receipt unreadable")):
                for _ in range(3):
                    current = A.reap_orphan(state.task_id)
                self.assertEqual(current.status, "failed")
                self.assertEqual(current.recovery_attempts, 3)

    def test_cancel_precedes_recovery_even_when_recovery_is_broken(self):
        state, path = self.task()
        A.request_task_stop(state.task_id)
        with patch.object(C, "recover_turn", side_effect=AssertionError("must not recover")) as recover:
            result = A.reap_orphan(state.task_id)
            recover.assert_not_called()
        self.assertEqual(result.status, "stopped")
        self.assertFalse(A.was_stopped(state.task_id))

    def test_missing_pair_skips_recovery_bookkeeping(self):
        state, _ = self.task()
        R.remove_pair(self.spec.name)
        result = A.reap_orphan(state.task_id)
        self.assertEqual(result.status, "done")
        self.assertIn("pair missing or quarantined", " ".join(result.result.notes))

    def test_reused_child_pid_neither_blocks_recovery_nor_gets_killed(self):
        state, path = self.task(child_pid=os.getpid())
        state.child_started_at -= 100  # same PID, different process lifetime
        A._save(state)
        self.assertFalse(A.codex_child_alive(state))
        with patch.object(C.subprocess, "run") as kill, patch("os.killpg", create=True) as killpg:
            self.assertIn("identity-unverified", C.stop_detached(state))
            kill.assert_not_called()
            killpg.assert_not_called()
        self.assertEqual(A.reap_orphan(state.task_id).status, "done")

    def test_reused_owner_pid_is_dead_for_poll_and_startup_sweep(self):
        if P.started_at(os.getpid()) is None:
            self.skipTest("no process creation times on this platform")
        state, _ = self.task()
        state.owner_pid = os.getpid()
        state.started_at = datetime.utcfromtimestamp(P.started_at(os.getpid()) - 10)
        A._save(state)
        self.assertTrue(A.is_detached(state))
        A._sweep_dead_predecessor_orphans()
        self.assertEqual(A.load_task(state.task_id).status, "done")

    def test_process_creation_time_matches_and_rejects_a_different_lifetime(self):
        created = P.started_at(os.getpid())
        # Unknown identity falls back to liveness (v0.16.1 — macOS CI read
        # every live child as gone when "unknown" meant "no match").
        self.assertTrue(P.matches(os.getpid(), None))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertFalse(P.matches(dead.pid, None))
        if created is None:
            self.skipTest("no process creation times on this platform")
        self.assertTrue(P.matches(os.getpid(), created))
        self.assertFalse(P.matches(os.getpid(), created - 10))

    def test_mutating_tools_refuse_live_detached_turn_before_side_effects(self):
        state, _ = self.task(child_pid=os.getpid())
        before = R.registry_path().read_bytes()
        operations = [
            (S.pair_clear, {"name": self.spec.name}),
            (S.pair_forget, {"name": self.spec.name}),
            (S.pair_fork, {"name": self.spec.name, "new_name": "fork"}),
            (S.pair_rewind, {"name": self.spec.name, "to_point": 1}),
            (S.pair_update, {"name": self.spec.name, "purpose": "new"}),
        ]
        for operation, kwargs in operations:
            with self.subTest(tool=operation), self.assertRaisesRegex(PairError, "detached Codex turn is in flight"):
                tool(operation)(**kwargs)
            self.assertEqual(R.registry_path().read_bytes(), before)
            self.assertEqual(A.load_task(state.task_id).status, "running")

    def test_replay_formatting_failure_is_best_effort_and_batched(self):
        state, path = self.task(events=[{"type": "item.completed", "item": "bad shape"}] + EVENTS)
        with patch.object(T, "append_log", wraps=T.append_log) as append:
            result = A.reap_orphan(state.task_id)
        self.assertEqual(result.status, "done")
        self.assertEqual(result.result.response, "recovered reply")
        self.assertEqual(append.call_count, 1)
        self.assertIn("event formatting skipped", Path(result.result.log_path).read_text())

    def run_fake_events(self, events, callback=None, *, gap=0):
        script = self.directory / "events.py"
        script.write_text("import json, time\n" + f"events = {events!r}\n"
                          "for event in events:\n print(json.dumps(event), flush=True)\n"
                          f" time.sleep({gap!r})\n", encoding="utf-8")
        return self.adapter._run([sys.executable, "-u", str(script)], self.spec,
                                  timeout_seconds=20, task_label="send", log=True, on_event=callback)

    def test_live_formatting_failure_does_not_kill_turn(self):
        events, stderr, code, duration = self.run_fake_events(
            [{"type": "item.completed", "item": "bad shape"}] + EVENTS)
        self.assertEqual(code, 0)
        result = self.adapter._build_send_result(self.spec, events, stderr, code, duration,
                                                 time.time(), model_used=self.spec.model, permission_level="read-only")
        self.assertEqual(result.response, "recovered reply")
        self.assertIsNone(result.safety_signal)

    def test_final_file_reread_includes_events_after_a_blocked_callback(self):
        release = threading.Event()
        entered = threading.Event()
        readers = []
        def callback(event):
            readers.append(threading.current_thread())
            entered.set()
            release.wait(12)
        try:
            events, stderr, code, duration = self.run_fake_events(EVENTS, callback, gap=.2)
            self.assertTrue(entered.is_set())
            self.assertEqual(code, 0)
            self.assertEqual(events[-2]["type"], "turn.completed")
            result = self.adapter._build_send_result(self.spec, events, stderr, code, duration,
                                                     time.time(), model_used=self.spec.model, permission_level="read-only")
            self.assertEqual(result.response, "recovered reply")
        finally:
            release.set()
            for reader in set(readers):
                reader.join(5)
                self.assertFalse(reader.is_alive(), "event reader did not close its files")

    def test_transient_logging_failure_replays_tail_at_exit(self):
        append = T.append_log
        failed = False
        def flaky(events, receipt, lines, offset):
            nonlocal failed
            if offset and not failed:
                failed = True
                raise OSError("temporary log failure")
            return append(events, receipt, lines, offset)
        with patch.object(T, "append_log", side_effect=flaky):
            events, _, code, _ = self.run_fake_events(EVENTS)
        self.assertTrue(failed)
        self.assertEqual(code, 0)
        log = Path(events[-1]["log_path"]).read_text()
        self.assertEqual(log.count("[text] recovered reply"), 1)
        self.assertIn("TURN COMPLETED", log)

    def test_terminal_error_takes_priority_over_reconnect_notices(self):
        events = [{"type": "error", "message": "rate limit reconnect " * 100},
                  {"type": "turn.failed", "error": {"message": "thread-store conflict"}}]
        result = self.adapter._build_send_result(self.spec, events, "", 1, 10, time.time(),
                                                 model_used=self.spec.model, permission_level="read-only")
        self.assertEqual(result.safety_kind, "error")
        self.assertIn("thread-store conflict", result.safety_signal)

    def test_wait_loop_recovers_detached_completion_early(self):
        state, _ = self.task()
        started = time.monotonic()
        self.assertEqual(A.wait_for_task(state.task_id, 10).status, "done")
        self.assertLess(time.monotonic() - started, 3)

    def test_detached_wait_scans_directory_once_and_polls_known_task(self):
        state, _ = self.task(child_pid=os.getpid())
        finished = state.model_copy(update={"status": "done"})
        with patch.object(A, "list_running_task_ids_for_pair", wraps=A.list_running_task_ids_for_pair) as scan, \
                patch.object(A, "reap_orphan", side_effect=[state, state, finished]) as reap, \
                patch.object(A.time, "sleep") as sleep, S._PairLock(self.spec.name):
            A.wait_for_detached(self.spec.name)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(reap.call_count, 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (1,)])

    def test_new_runs_do_not_require_reading_receipts_for_retention(self):
        _, path = self.task()
        with patch.object(T, "read_receipt", side_effect=AssertionError("too new to prune")):
            T.prune_turns(path.parent)

    def test_live_batch_writes_no_receipts_for_no_output_events(self):
        with patch.object(T, "append_log", wraps=T.append_log) as append:
            self.run_fake_events([{"type": "turn.started"}] * 20 + [EVENTS[-1]])
        # Initial turn marker plus the terminal event, regardless of how many
        # batches the no-output events were read in.
        self.assertEqual(append.call_count, 2)

    def test_linux_creation_time_handles_spaces_and_parentheses_in_comm(self):
        # Pure synthetic /proc records; no live Linux process required.
        fields = ["S"] + ["0"] * 18 + ["12345"]
        def read(path, *args, **kwargs):
            return ("100 (worker (test) name) " + " ".join(fields)
                    if path.as_posix() != "/proc/stat" else "btime 1000\n")
        with patch.object(P.sys, "platform", "linux"), patch.object(Path, "read_text", read), \
                patch.object(P.os, "sysconf", return_value=100, create=True):
            self.assertEqual(P.started_at(100), 1123.45)

    def test_startup_sweeps_unvalidated_legacy_task_and_compact_text_is_specific(self):
        path = A._task_path("legacy")
        path.write_text(json.dumps({"task_id": "legacy", "pair_name": self.spec.name,
                                    "owner_pid": 999999999, "status": "running"}))
        A._sweep_dead_predecessor_orphans()
        self.assertEqual(json.loads(path.read_text())["status"], "failed")
        state, _ = self.task(events=[])
        state.message = "/compact"
        state.events_path = None
        A._save(state)
        self.assertIn("app-server RPC", A.reap_orphan(state.task_id).error)

    def test_wait_behind_detached_can_be_cancelled(self):
        state, _ = self.task(child_pid=os.getpid())
        with S._PairLock(self.spec.name), self.assertRaises(TaskStopped):
            A.wait_for_detached(self.spec.name, should_stop=lambda: True)
        self.assertEqual(A.load_task(state.task_id).status, "running")

    def test_successor_waits_recovers_then_sends(self):
        predecessor, _ = self.task(child_pid=54321)
        release = threading.Event()
        started = threading.Event()

        def alive(pid):
            return pid == os.getpid() or (pid == 54321 and not release.is_set())

        def send(*args, **kwargs):
            self.assertEqual(A.load_task(predecessor.task_id).status, "done")
            self.assertEqual(R.get_pair(self.spec.name).turn_count, 1)
            started.set()
            return C.SendResult(name=self.spec.name, session_id=self.spec.session_id,
                                response="next", model_used=self.spec.model, backend="codex", duration_ms=1)

        with patch.object(A, "_is_pid_alive", side_effect=alive), \
                patch.object(P, "matches", return_value=True), \
                patch.object(C.CodexAdapter, "send", side_effect=send):
            runner = S._build_send_runner(self.spec.name, "next", hard_timeout_seconds=5,
                                          override_model=None, override_effort=None, override_permission_mode=None)
            successor = A.start_task(self.spec.name, "next", runner, queued=True)
            time.sleep(.3)
            self.assertFalse(started.is_set())
            self.assertTrue(A.load_task(successor.task_id).queued)
            release.set()
            done = A.wait_for_task(successor.task_id, 5)
            self.assertEqual(done.status, "done", done.error)
            self.assertTrue(started.is_set())
            self.assertEqual(R.get_pair(self.spec.name).turn_count, 2)

    def test_thread_store_conflict_is_a_real_error(self):
        result = self.adapter._build_send_result(self.spec, [], "thread-store conflict", 1, 20,
                                                 time.time(), model_used=self.spec.model,
                                                 permission_level="read-only")
        self.assertEqual(result.safety_kind, "error")
        self.assertIn("thread-store conflict", result.safety_signal)
        state, _ = self.task(events=[{"type": "error", "message": "thread-store conflict"}])
        recovered = A.reap_orphan(state.task_id)
        self.assertEqual(recovered.result.safety_kind, "error")
        self.assertIn("thread-store conflict", recovered.result.safety_signal)

    def test_live_stop_and_timeout_kill_only_spawned_fake_child(self):
        script = self.directory / "sleeper.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        for timeout, checker, error in [(5, lambda: True, CLIError), (.1, None, CommandTimeout)]:
            with self.subTest(error=error), self.assertRaises(error):
                self.adapter._run([sys.executable, str(script)], self.spec,
                                  timeout_seconds=timeout, should_stop=checker,
                                  task_label="send", log=True)
            self.assertIsNone(C.inflight_info(self.spec.name))

    def test_recovery_keeps_per_call_model_and_permission(self):
        state, path = self.task()
        receipt = T.read_receipt(path)
        receipt["model_used"] = "gpt-6-luna"
        receipt["permission_level"] = "unrestricted"
        T.save_receipt(path, receipt)
        with patch.object(C.CodexAdapter, "_build_send_result", wraps=self.adapter._build_send_result) as builder:
            result = A.reap_orphan(state.task_id)
        self.assertEqual(result.result.model_used, "gpt-6-luna")
        self.assertEqual(builder.call_args.kwargs["permission_level"], "unrestricted")

    def test_stop_targets_selected_detached_pid_only(self):
        state, _ = self.task(child_pid=54321)
        with patch.object(A, "_is_pid_alive", side_effect=lambda pid: pid == 54321), \
                patch.object(P, "matching_process", return_value=nullcontext(True)), \
                patch.object(C.subprocess, "run") as run, patch("os.killpg", create=True) as killpg:
            run.return_value.returncode = 0
            output = tool(S.pair_stop)(self.spec.name)
            self.assertIn("tree-killed detached codex child PID 54321", output)
            if os.name == "nt":
                self.assertEqual(run.call_args.args[0], ["taskkill", "/F", "/T", "/PID", "54321"])
            else:
                self.assertEqual(killpg.call_args.args[0], 54321)
        self.assertTrue(A.was_stopped(state.task_id))

    def test_high_effort_silence_is_not_reported_as_hung(self):
        state, _ = self.task(child_pid=os.getpid())
        state.owner_pid = os.getpid()
        A._save(state)
        old = datetime.utcnow() - timedelta(seconds=250)
        with patch.object(C, "inflight_info", return_value={"started_at": old, "last_activity": old}):
            self.assertNotIn("likely-hung", tool(S.pair_status)(self.spec.name))
            self.assertNotIn("may be hung", tool(S.pair_poll)(state.task_id))
            self.assertIn("3–6 minutes", tool(S.pair_poll)(state.task_id))

    def test_retention_preserves_unresolved_turns(self):
        state, path = self.task()
        os.utime(path, (time.time() - 40 * 86400,) * 2)
        T.prune_turns(path.parent)
        self.assertTrue(path.exists())
        state.status = "done"
        A._save(state)
        T.prune_turns(path.parent)
        self.assertFalse(path.exists())

    def test_fake_exec_reads_prompt_file_and_emits_live_events(self):
        script = self.directory / "fake.py"
        script.write_text("import sys, json, time\n"
                          "assert len(sys.stdin.read()) == 100000\n"
                          f"events = {EVENTS!r}\n"
                          "for event in events:\n"
                          " print(json.dumps(event), flush=True)\n"
                          " time.sleep(.03)\n", encoding="utf-8")
        observed = []
        state = A.start_task(self.spec.name, "fake", lambda tid: self._fake_send(script, tid, observed))
        done = A.wait_for_task(state.task_id, 10)
        self.assertEqual(done.status, "done", done.error)
        self.assertEqual(done.result.response, "recovered reply")
        self.assertEqual(observed, EVENTS)
        self.assertIsNone(done.child_pid)
        self.assertTrue(Path(done.events_path).exists())

    def _fake_send(self, script, tid, observed):
        with S._PairLock(self.spec.name):
            events, stderr, code, duration = self.adapter._run(
                [sys.executable, "-u", str(script)], self.spec, timeout_seconds=5,
                task_label="send", log=True, task_id=tid, stdin_text="x" * 100000,
                on_event=observed.append,
            )
            return self.adapter._build_send_result(self.spec, events, stderr, code, duration,
                                                    time.time(), model_used="gpt-6-astra",
                                                    permission_level="read-only")

    def test_real_parent_death_fake_child_survives_and_startup_recovers(self):
        release = self.directory / "finish-child"
        child_pid_file = self.directory / "child-pid"
        child = self.directory / "child.py"
        child.write_text("import json, os, time\n"
                         "from pathlib import Path\n"
                         f"Path({str(child_pid_file)!r}).write_text(str(os.getpid()))\n"
                         f"events = {EVENTS!r}\n"
                         "print(json.dumps(events[0]), flush=True)\n"
                         "deadline = time.monotonic() + 30\n"
                         f"while not Path({str(release)!r}).exists():\n"
                         " if time.monotonic() >= deadline: raise SystemExit('release timed out')\n"
                         " time.sleep(.05)\n"
                         "for event in events[1:]: print(json.dumps(event), flush=True)\n",
                         encoding="utf-8")
        parent = self.directory / "parent.py"
        parent.write_text(
            "import sys, time\n"
            "from claude_squared import async_tasks as A, registry as R\n"
            "from claude_squared.adapters.codex import CodexAdapter\n"
            "from claude_squared.server import _PairLock\n"
            "def run(tid):\n"
            " with _PairLock('detach'):\n"
            "  CodexAdapter()._run([sys.executable, '-u', sys.argv[1]], R.get_pair('detach'),\n"
            "    timeout_seconds=15, task_label='send', log=True, task_id=tid, stdin_text='fake')\n"
            "A.start_task('detach', 'fake child', run)\n"
            "time.sleep(20)\n", encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen([sys.executable, "-u", str(parent), str(child)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        state = None
        child_pid = None
        child_handle = None

        def capture_child():
            nonlocal child_pid, child_handle
            if child_pid is None and child_pid_file.exists():
                value = child_pid_file.read_text().strip()
                if value:
                    child_pid = int(value)
            if child_pid is None:
                tid = A.latest_task_id_for_pair("detach")
                published = A.load_task(tid) if tid else None
                if published is not None:
                    child_pid = published.child_pid
            if child_pid is not None and os.name == "nt" and child_handle is None:
                import _winapi
                try:
                    # Retain the exact process object, not a reusable PID, for
                    # cleanup after its parent dies. SYNCHRONIZE | TERMINATE.
                    child_handle = _winapi.OpenProcess(0x00100001, False, child_pid)
                except OSError:
                    if A._is_pid_alive(child_pid):
                        raise

        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                tid = A.latest_task_id_for_pair("detach")
                state = A.load_task(tid) if tid else None
                capture_child()
                if (state and state.child_pid and state.events_path and child_pid is not None
                        and Path(state.events_path).stat().st_size):
                    break
                time.sleep(.05)
            else:
                self.fail("fake child did not publish its PID/events before the deadline")
            self.assertIsNotNone(state)
            self.assertEqual(state.child_pid, child_pid)
            proc.kill()  # ONLY the parent we spawned; deliberately not its tree
            proc.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = A.load_task(state.task_id)
                if not A._is_pid_alive(proc.pid) and state.child_pid == child_pid:
                    break
                time.sleep(.05)
            else:
                self.fail("owner still reported alive or child PID missing after kill/wait")
            self.assertEqual(A.reap_orphan(state.task_id).status, "running")
            self.assertIn("DETACHED", tool(S.pair_poll)(state.task_id))
            release.touch()  # completion starts only AFTER detached rendering
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                A._sweep_dead_predecessor_orphans()
                result = A.load_task(state.task_id)
                if result.status != "running":
                    break
                time.sleep(.1)
            self.assertEqual(result.status, "done", result.error)
            self.assertEqual(result.result.response, "recovered reply")
            self.assertEqual(R.get_pair("detach").turn_count, 1)
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                # Discover even a child whose task-state publication failed.
                # Its own PID file is written before it opens the test gate.
                capture_child()
                if child_handle is not None:
                    import _winapi
                    try:
                        if _winapi.WaitForSingleObject(child_handle, 0) == _winapi.WAIT_TIMEOUT:
                            _winapi.TerminateProcess(child_handle, 1)
                        self.assertEqual(_winapi.WaitForSingleObject(child_handle, 5000),
                                         _winapi.WAIT_OBJECT_0, "fake child did not exit during cleanup")
                    finally:
                        _winapi.CloseHandle(child_handle)
                elif child_pid is not None and os.name != "nt" and A._is_pid_alive(child_pid):
                    import signal
                    os.kill(child_pid, signal.SIGKILL)
            finally:
                if proc.stderr:
                    proc.stderr.close()

    @unittest.skipUnless(os.name == "nt", "Windows retains exited process objects while handles remain open")
    def test_exited_windows_process_is_dead_while_popen_retains_handle(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(A._is_pid_alive(proc.pid))
            proc.kill()
            proc.wait(timeout=5)
            self.assertFalse(A._is_pid_alive(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
