"""Codex activity/context regressions. Fake execs and temporary homes only."""
from __future__ import annotations

import ast
import io
from contextlib import ExitStack
from datetime import datetime, timedelta
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
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-codex-activity-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(TEST_HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(TEST_HOME.name) / "codex")

from claude_squared import activity as V, async_tasks as A, registry as R, server as S, codex_turns as T
from claude_squared import _wait_script as W
from claude_squared.adapters import codex as C
from claude_squared.errors import CLIError, CommandTimeout, SessionMissing, TaskStopped
from claude_squared.models import AsyncTaskState, PairSpec


EVENTS = [
    {"type": "item.completed", "item": {"id": "reply", "type": "agent_message", "text": "finished"}},
    {"type": "turn.completed", "usage": {"input_tokens": 90_000, "output_tokens": 50}},
]


def tool(fn):
    return getattr(fn, "fn", fn)


class CodexActivityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.stack.enter_context(patch.dict(os.environ, {
            "CLAUDE_HOME": str(self.root / "claude"), "CODEX_HOME": str(self.root / "codex"),
        }))
        self.addCleanup(self.retire_writers)
        self.spec = PairSpec(name="context", session_id="thread", backend="codex", model="gpt-6-luna",
                             permission_mode="read-only", effort="low", cwd=str(self.root))
        R.add_pair(self.spec)
        self.rollout = self.root / "rollout.jsonl"
        self.tokens(60_000)
        self.stack.enter_context(patch.object(C, "rollout_path_for", return_value=self.rollout))
        self.stack.enter_context(patch.object(C, "resolve_codex_model", return_value=(self.spec.model, None)))
        self.stack.enter_context(patch.object(C, "context_windows", return_value=(200_000, 800_000)))
        self.adapter = C.CodexAdapter()

    def retire_writers(self):
        home = str(self.root / "claude" / "pairs")
        with V._WRITERS_LOCK:
            writers = [V._WRITERS.pop(key) for key in list(V._WRITERS) if key[0] == home]
        for writer in writers:
            with writer._io_lock, writer._lock:
                writer._forgotten = True

    def tokens(self, used, window=200_000, cumulative=9_999_999):
        event = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": used, "total_tokens": used + 50},
            "total_token_usage": {"input_tokens": cumulative, "total_tokens": cumulative + 50},
            "model_context_window": window,
        }}}
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    def snapshot(self):
        return V.writer(self.spec.name, "codex").snapshot()

    def test_backend_compaction_rules_and_config_precedence(self):
        self.assertEqual(V.compaction_point(200_000), 170_000)
        self.assertEqual(V.compaction_point(200_000, "codex"), 180_000)
        self.assertEqual(V.compaction_point(200_000, "codex", 150_000), 150_000)
        self.assertEqual(V.context_alert_threshold(200_000, 40_000, "codex", 150_000), 110_000)
        self.assertEqual(V.compaction_point(200_000, "codex", 900_000), 200_000)
        args = ["codex", "-c", "model_auto_compact_token_limit=150000",
                "--config=model_auto_compact_token_limit=130000"]
        self.assertEqual(C._configured_compact_limit(args), 130_000)
        self.assertEqual(C._configured_compact_limit(args + ["-cmodel_auto_compact_token_limit=110000"]), 110_000)
        self.assertIsNone(C._configured_compact_limit(["codex"]))
        self.assertIsNone(C._configured_compact_limit(["codex", "-c", "model_auto_compact_token_limit=false"]))

    def test_sample_uses_last_input_and_reported_window_at_ten_second_intervals(self):
        turn = C._CodexActivity(self.spec, "sampling")
        turn.limit = 150_000
        with patch.object(C, "read_last_token_count", wraps=C.read_last_token_count) as read:
            turn.poll(force=True)
            self.tokens(90_000)
            for _ in range(10):
                turn.poll()
            self.assertEqual(read.call_count, 1)
            self.assertEqual(self.snapshot()["context"]["used"], 60_000)
            turn._next_sample = time.monotonic() - 1
            turn.poll()
            self.assertEqual(read.call_count, 2)
        context = self.snapshot()["context"]
        self.assertEqual((context["used"], context["window"], context["window_source"]),
                         (90_000, 200_000, "rollout"))
        self.assertEqual(context["auto_compact_token_limit"], 150_000)
        turn.writer.flush()
        self.assertEqual(V.reported_window(self.spec.name)["auto_compact_token_limit"], 150_000)
        turn.limit = 120_000
        turn.poll(force=True)
        turn.writer.flush()
        self.assertEqual(V.reported_window(self.spec.name)["auto_compact_token_limit"], 120_000)
        turn.close()

    def test_no_cumulative_fallback_when_last_usage_is_missing(self):
        turn = C._CodexActivity(self.spec, "missing-last")
        with patch.object(C, "read_last_token_count", return_value={"info": {
                "total_token_usage": {"input_tokens": 900_000}, "model_context_window": 200_000}}):
            turn.poll(force=True)
        self.assertIsNone(self.snapshot()["context"])
        turn.close()

    def test_token_lookup_reads_only_tail_of_large_history(self):
        payload = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 123}}}
        latest = (json.dumps({"type": "event_msg", "payload": payload}) + "\n").encode()
        class CountingFile(io.BytesIO):
            bytes_read = 0
            def read(self, size=-1):
                data = super().read(size)
                self.bytes_read += len(data)
                return data
        stream = CountingFile((b'{"type":"old"}\n' * 200_000) + latest)
        with patch("builtins.open", return_value=stream):
            self.assertEqual(C.read_last_token_count(self.rollout), payload)
        self.assertLessEqual(stream.bytes_read, 64 * 1024)

    def test_reverse_reader_handles_boundaries_partial_utf8_and_rewind(self):
        records = [b'first', 'split euro: €'.encode(), b'third' * 20]
        for chunk in (1, 3, 17, 64):
            for tail in (b'', b'partial' * 30):
                with self.subTest(chunk=chunk, tail=bool(tail)):
                    stream = io.BytesIO(b'\n'.join(records) + b'\n' + tail)
                    self.assertEqual(list(C._reverse_complete_lines(stream, chunk)), list(reversed(records)))
        self.tokens(90_000)
        self.assertEqual(C.read_last_token_count(self.rollout)["info"]["last_token_usage"]["input_tokens"], 90_000)
        self.rollout.write_bytes(b'')  # rewind truncates; no stale payload/cursor may survive
        self.tokens(100)
        with self.rollout.open("ab") as stream:
            stream.write(b'{"type":"event_msg","payload":{"type":"token_count"}}')
        self.assertEqual(C.read_last_token_count(self.rollout)["info"]["last_token_usage"]["input_tokens"], 100)

    def test_compactions_without_ids_use_file_positions_for_replay_dedup(self):
        turn = C._CodexActivity(self.spec, "anonymous")
        rows = [(10, {"type": "item.started", "item": {"type": "contextCompaction", "pre_tokens": 100}}),
                (20, {"type": "item.completed", "item": {"type": "contextCompaction", "post_tokens": 50}}),
                (30, {"type": "item.started", "item": {"type": "contextCompaction", "pre_tokens": 200}}),
                (40, {"type": "item.completed", "item": {"type": "contextCompaction", "post_tokens": 75}})]
        for _ in range(2):  # live reader followed by the final file reread
            for position, event in rows:
                turn.observe(event, position=position)
            turn.poll()
        self.assertEqual([(c["pre"], c["post"]) for c in self.snapshot()["compactions"]], [(100, 50), (200, 75)])
        turn.close()

    def test_broken_activity_writer_does_not_prevent_send(self):
        with patch.object(V, "writer", side_effect=OSError("activity unavailable")), \
                patch.object(self.adapter, "_exec_args", return_value=["fake"]), \
                patch.object(self.adapter, "_run", return_value=(EVENTS, "", 0, 1)) as run:
            result = self.adapter.send(self.spec, "go")
        self.assertEqual(result.response, "finished")
        self.assertIsInstance(run.call_args.kwargs["activity_turn"], C._NoCodexActivity)

    def test_recovery_supervision_status_matches_watcher_exit_codes(self):
        g = {"__name__": "offline_wait"}
        exec(W.WAIT_SCRIPT_SOURCE, g)
        for prefix, status, code in [(A.ORPHAN_ERROR_PREFIX, "owner_died", 4),
                                     (A.CRASHED_ERROR_PREFIX, "crashed", 6)]:
            with self.subTest(status=status):
                state = AsyncTaskState(task_id="lost", pair_name=self.spec.name, message="lost", status="failed",
                                       started_at=datetime.utcnow(), events_path=str(self.root / "missing.events.jsonl"),
                                       error=prefix + "owner/process exited")
                C.record_recovered_activity(state)
                self.assertEqual(self.snapshot()["window_turns"][-1]["status"], status)
                self.assertEqual(g["_turn_exit"](status), code)

    def test_identity_query_failure_cannot_skip_finalization_cleanup(self):
        state = AsyncTaskState(task_id="finalize", pair_name=self.spec.name, message="done", status="running",
                               owner_pid=os.getpid(), started_at=datetime.utcnow(), events_path="unused")
        A._save(state)
        A.request_task_stop(state.task_id)
        with A._states_guard:
            A._local_states[state.task_id] = state
        state.status = "done"
        event = threading.Event()
        with patch.object(A, "owner_alive", side_effect=RuntimeError("process query failed")):
            A._finish_task(state, event)
        self.assertTrue(event.is_set())
        self.assertNotIn(state.task_id, A.local_running_tasks(self.spec.name))
        with A._states_guard:
            self.assertNotIn(state.task_id, A._local_states)
        self.assertFalse(A.was_stopped(state.task_id))
        self.assertEqual(A.load_task(state.task_id).status, "done")

    def test_short_lived_recovery_publisher_marks_owner_file_exited(self):
        tid = str(uuid.uuid4())
        directory = R.logs_dir() / self.spec.name / "turns"
        directory.mkdir(parents=True)
        events_path = directory / f"{tid}.events.jsonl"
        events_path.write_text("".join(json.dumps(e) + "\n" for e in EVENTS))
        os.utime(events_path, (time.time() - 10,) * 2)
        A._save(AsyncTaskState(task_id=tid, pair_name=self.spec.name, message="recover", status="running",
                               started_at=datetime.utcnow(), owner_pid=999999999, child_pid=999999998,
                               events_path=str(events_path)))
        T.save_receipt(events_path, {"run_id": tid, "spec": self.spec.model_dump(mode="json"),
                                     "model_used": self.spec.model, "permission_level": "read-only",
                                     "started_epoch": time.time()-30, "log_path": str(directory.parent / "main.log"),
                                     "logged_offset": 0})
        code = ("import threading\nfrom claude_squared import async_tasks as A\n"
                "for thread in threading.enumerate():\n"
                " if thread.name == 'codex-orphan-recovery': thread.join(10)\n"
                f"assert A.load_task({tid!r}).status == 'done'\n")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = subprocess.Popen([sys.executable, "-B", "-c", code], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", **flags)
        try:
            out, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, out + err)
            owner_file = R.pairs_dir() / "activity" / self.spec.name / f"{proc.pid}.json"
            self.assertEqual(json.loads(owner_file.read_text())["state"], "exited")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()

    def test_compaction_is_queued_off_reader_and_deduplicated(self):
        turn = C._CodexActivity(self.spec, "compaction")
        turn.poll(force=True)
        started = {"type": "item.started", "item": {"id": "compact", "type": "contextCompaction"}}
        completed = {"type": "item.completed", "item": {"id": "compact", "type": "contextCompaction"}}
        with patch.object(C, "read_last_token_count", wraps=C.read_last_token_count) as read:
            reader = threading.Thread(target=lambda: (turn.observe(started), turn.observe(completed)))
            reader.start()
            reader.join(5)
            read.assert_not_called()
            self.tokens(5_000)
            turn.poll()
        turn.observe(completed)  # final complete-file reread sees it again
        turn.poll(force=True)
        compactions = self.snapshot()["compactions"]
        self.assertEqual(len(compactions), 1)
        self.assertEqual((compactions[0]["trigger"], compactions[0]["pre"], compactions[0]["post"]),
                         ("auto", 60_000, 5_000))
        turn.close()

    def test_send_closes_activity_on_success_native_failure_and_exceptions(self):
        cases = [(None, EVENTS, 0, "done"),
                 (None, [{"type": "turn.failed", "error": {"message": "failed"}}], 1, "failed"),
                 (CLIError("spawn failed"), None, None, "failed"),
                 (CommandTimeout(self.spec.name, 1), None, None, "timeout"),
                 (TaskStopped("cancelled"), None, None, "stopped")]
        for error, events, code, expected in cases:
            with self.subTest(status=expected):
                def run(*args, **kwargs):
                    self.assertEqual(self.snapshot()["turn"]["kind"], "send")
                    if error:
                        raise error
                    return events, "", code, 1
                with patch.object(self.adapter, "_exec_args", return_value=["fake"]), \
                        patch.object(self.adapter, "_run", side_effect=run):
                    if error:
                        with self.assertRaises(type(error)):
                            self.adapter.send(self.spec, "go")
                    else:
                        self.adapter.send(self.spec, "go")
                self.assertIsNone(self.snapshot()["turn"])
                self.assertEqual(self.snapshot()["window_turns"][-1]["status"], expected)
        with patch.object(self.adapter, "session_exists", return_value=False), self.assertRaises(SessionMissing):
            self.adapter.send(self.spec, "missing")
        self.assertEqual(self.snapshot()["window_turns"][-1]["status"], "failed")

    def test_fake_exec_publishes_context_while_running_and_status_renders_it(self):
        script = self.root / "fake.py"
        script.write_text("import json, time\n"
                          "print(json.dumps({'type':'turn.started'}), flush=True)\n"
                          "time.sleep(.3)\n"
                          f"with open({str(self.rollout)!r}, 'a') as f:\n"
                          " f.write(json.dumps({'type':'event_msg','payload':{'type':'token_count','info':"
                          "{'last_token_usage':{'input_tokens':90000},'total_token_usage':{'input_tokens':9999999},"
                          "'model_context_window':200000}}})+'\\n')\n"
                          "time.sleep(.6)\n"
                          f"for event in {EVENTS!r}: print(json.dumps(event), flush=True)\n", encoding="utf-8")
        seen = []
        threads = []
        read = C.read_last_token_count
        def sample(path):
            threads.append(threading.current_thread().name)
            return read(path)
        def event(ev):
            if ev["type"] == "item.completed":
                seen.append(tool(S.pair_status)(self.spec.name))
        with patch.object(self.adapter, "_exec_args", return_value=[sys.executable, "-u", str(script)]), \
                patch.object(C, "CONTEXT_POLL_SECONDS", .05), patch.object(C, "read_last_token_count", side_effect=sample):
            result = self.adapter.send(self.spec, "go", on_event=event)
        self.assertEqual(result.response, "finished")
        self.assertIn("90,000 / 200,000 (45%) live", " ".join(seen))
        self.assertTrue(all(name == threading.current_thread().name for name in threads))
        self.assertEqual(self.snapshot()["window_turns"][-1]["status"], "done")

    def test_compact_runner_opens_only_one_compact_turn(self):
        with patch.object(self.adapter.__class__, "_appserver_compact", return_value=None):
            S._build_compact_runner(self.spec.name, None, compact_timeout_seconds=5)()
        turns = self.snapshot()["window_turns"]
        self.assertEqual([t["kind"] for t in turns], ["compact"])

    def usage_event(self, last_input, last_total, cumulative, window=475_000):
        event = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": last_input, "output_tokens": last_total - last_input,
                                 "total_tokens": last_total},
            "total_token_usage": {"input_tokens": cumulative - 276_417, "output_tokens": 276_417,
                                  "total_tokens": cumulative},
            "model_context_window": window,
        }}}
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    def test_compact_uses_post_compaction_estimate_not_cumulative_usage(self):
        # 2026-10-15 regression: the last pre-compaction call (318,367 input)
        # and Codex's post-compaction token_count (input 0, total_tokens = its
        # estimate of the new history) — the cumulative 56,802,796 was
        # reported as the compacted size and tripped the context alert.
        self.usage_event(318_367, 318_487, 56_802_796)
        turn = C._CodexActivity(self.spec, "before-compact")
        turn.poll(force=True)
        turn.close()
        self.assertEqual(self.snapshot()["context"]["used"], 318_367)
        compacted = lambda *a, **k: self.usage_event(0, 27_527, 56_802_796)
        with patch.object(self.adapter.__class__, "_appserver_compact", side_effect=compacted):
            result = S._build_compact_runner(self.spec.name, None, compact_timeout_seconds=5)()
        self.assertEqual((result.pre_tokens, result.post_tokens), (318_367, 27_527))
        self.assertIn("Codex's estimate", result.summary_preview)
        context = self.snapshot()["context"]
        self.assertEqual((context["used"], context["source"]), (27_527, "compaction"))
        status = tool(S.pair_status)(self.spec.name)
        self.assertIn("27,527 / 475,000", status)
        self.assertNotIn("past the context alert", status)
        log = (R.logs_dir() / self.spec.name / "main.log").read_text(encoding="utf-8")
        self.assertIn("COMPACTED 318,367 -> 27,527", log)
        # The next sample before any model call keeps the estimate, not 0.
        turn = C._CodexActivity(self.spec, "after-compact")
        turn.poll(force=True)
        turn.close()
        self.assertEqual(self.snapshot()["context"]["used"], 27_527)
        self.assertEqual(self.adapter.context(self.spec).tokens_used, 27_527)

    def test_compact_without_a_new_token_count_never_reports_the_old_one(self):
        self.usage_event(318_367, 318_487, 56_802_796)
        with patch.object(self.adapter.__class__, "_appserver_compact", return_value=None):
            result = self.adapter.compact(self.spec, timeout_seconds=5)
        self.assertEqual(result.pre_tokens, 318_367)
        self.assertNotIn(result.post_tokens, (318_367, 318_487, 56_802_796))

    def test_live_context_tokens_ignores_cumulative_usage(self):
        self.assertEqual(C.live_context_tokens({"last_token_usage": {"input_tokens": 90_000, "total_tokens": 90_050},
                                                "total_token_usage": {"total_tokens": 9_999_999}}), 90_000)
        self.assertEqual(C.live_context_tokens({"last_token_usage": {"input_tokens": 0, "total_tokens": 27_527},
                                                "total_token_usage": {"total_tokens": 56_802_796}}), 27_527)
        self.assertIsNone(C.live_context_tokens({"total_token_usage": {"total_tokens": 56_802_796}}))
        self.assertIsNone(C.live_context_tokens(None))

    def test_context_larger_than_window_is_unknown_and_never_alerts(self):
        # A context file already holding a cumulative figure (written by an
        # older build) reads as unknown: no status alert, no wait.py exit 8.
        writer = V.writer(self.spec.name, "codex")
        writer.note_context(318_367, 475_000, window_source="rollout", source="rollout")
        writer.note_compaction("manual (app-server)", 318_367, 56_802_796)
        self.assertIsNone(self.snapshot()["context"]["used"])
        writer.note_context(56_802_796, 475_000, window_source="rollout", source="compaction")
        writer.flush()
        context = V.view(self.spec.name, grace_seconds=0)["context"]
        self.assertIsNone(context["used"])
        self.assertEqual(context["implausible_used"], 56_802_796)
        self.assertNotIn("past the context alert", tool(S.pair_status)(self.spec.name))
        g = {"__name__": "offline_wait"}
        exec(W.WAIT_SCRIPT_SOURCE, g)
        g["_headroom_read"] = lambda: (True, 40_000)
        alert = g["Alert"]("default", None)
        self.assertIsNone(alert.check(dict(context, used=318_367), None))
        self.assertIsNone(alert.check(context, None))

    def test_graceful_owner_shutdown_does_not_publish_a_surviving_send_as_aborted(self):
        turn = C._CodexActivity(self.spec, "detaching")
        turn.writer._shutdown("owner exited")
        snap = self.snapshot()
        self.assertEqual(snap["state"], "exited")
        self.assertEqual(snap["turn"]["task_id"], "detaching")
        self.assertEqual(snap["window_turns"], [])

    def test_poll_and_verbose_status_use_live_codex_compaction_boundary(self):
        tid = str(uuid.uuid4())
        A._save(AsyncTaskState(task_id=tid, pair_name=self.spec.name, message="poll", status="running",
                               started_at=datetime.utcnow(), owner_pid=os.getpid()))
        turn = C._CodexActivity(self.spec, tid)
        turn.limit = 150_000
        self.tokens(120_000)
        turn.poll(force=True)
        text = tool(S.pair_poll)(tid)
        self.assertIn("120,000 / 200,000 (60%) live", text)
        self.assertIn("estimated Codex auto-compaction point 150,000", text)
        self.assertNotIn("steering prompt", text)
        data = json.loads(tool(S.pair_status)(self.spec.name, verbose=True))
        self.assertEqual(data["activity"]["context"]["backend"], "codex")
        turn.close()

    def test_real_fake_exec_stop_and_timeout_close_activity(self):
        script = self.root / "sleep.py"
        script.write_text("import time\ntime.sleep(30)\n")
        with patch.object(self.adapter, "_exec_args", return_value=[sys.executable, str(script)]):
            with self.assertRaises(CLIError):
                self.adapter.send(self.spec, "stop", should_stop=lambda: True)
            self.assertEqual(self.snapshot()["window_turns"][-1]["status"], "stopped")
            with self.assertRaises(CommandTimeout):
                self.adapter.send(self.spec, "timeout", timeout_seconds=.1)
            self.assertEqual(self.snapshot()["window_turns"][-1]["status"], "timeout")

    def test_compaction_notice_survives_a_rollout_read_failure(self):
        turn = C._CodexActivity(self.spec, "unreadable")
        turn.observe({"type": "item.completed", "item": {"id": "c", "type": "contextCompaction",
                                                            "pre_tokens": 100, "post_tokens": 0}})
        with patch.object(C, "read_last_token_count", side_effect=OSError("rollout unavailable")):
            turn.poll()
        event = self.snapshot()["compactions"][0]
        self.assertEqual((event["pre"], event["post"]), (100, 0))
        turn.close()

    def test_recovery_closes_in_new_owner_and_merged_view_uses_terminal_task(self):
        tid = str(uuid.uuid4())
        directory = R.logs_dir() / self.spec.name / "turns"
        directory.mkdir(parents=True)
        events_path = directory / f"{tid}.events.jsonl"
        events_path.write_text("".join(json.dumps(e) + "\n" for e in EVENTS))
        os.utime(events_path, (time.time() - 10,) * 2)
        state = AsyncTaskState(task_id=tid, pair_name=self.spec.name, message="recover", status="running",
                               started_at=datetime.utcnow() - timedelta(minutes=1), owner_pid=999999999,
                               child_pid=999999998, events_path=str(events_path))
        A._save(state)
        T.save_receipt(events_path, {"run_id": tid, "spec": self.spec.model_dump(mode="json"),
                                     "model_used": self.spec.model, "permission_level": "read-only",
                                     "started_epoch": time.time()-30, "log_path": str(directory.parent / "main.log"),
                                     "logged_offset": 0, "auto_compact_token_limit": 150_000})
        old_file = R.pairs_dir() / "activity" / self.spec.name / "999999999.json"
        old_file.parent.mkdir(parents=True)
        old = {"version": 1, "pair": self.spec.name, "backend": "codex", "owner_pid": 999999999,
               "state": "live", "window_started_at": state.started_at.isoformat(),
               "turn": {"task_id": tid, "kind": "send", "started_at": state.started_at.isoformat()}}
        old_file.write_text(json.dumps(old))
        result = A.reap_orphan(tid)
        self.assertEqual(result.status, "done")
        view = V.view(self.spec.name, grace_seconds=0)
        self.assertEqual(view["verdict"], "idle")
        self.assertEqual([(t["task_id"], t["status"]) for t in view["window_turns"]], [(tid, "done")])
        self.assertEqual(view["context"]["auto_compact_token_limit"], 150_000)
        self.assertEqual(json.loads(old_file.read_text()), old)
        A.reap_orphan(tid)
        self.assertEqual(len(self.snapshot()["window_turns"]), 1)

    def test_wait_alert_uses_codex_limit_and_embeds_identical_stdlib_block(self):
        g = {"__name__": "offline_wait"}
        exec(W.WAIT_SCRIPT_SOURCE, g)
        g["_headroom_read"] = lambda: (True, 40_000)
        alert = g["Alert"]("default", None)
        ctx = {"backend": "codex", "used": 100_000, "window": 200_000, "auto_compact_token_limit": 150_000}
        self.assertIsNone(alert.check(ctx, None))
        hit = alert.check(dict(ctx, used=120_000), None)
        self.assertEqual(hit, (120_000, 200_000, 110_000))
        message = g["_alert_message"](self.spec.name, *hit, [self.spec.name], alert.backend, alert.auto_compact_token_limit)
        self.assertIn("150,000", message)
        self.assertNotIn("steering_prompt", message)
        self.assertIsNone(g["Alert"]("default", None).check(dict(ctx, used=130_000), None))
        source = Path(V.__file__).read_text(encoding="utf-8")
        self.assertIn(source[source.index(W._BEGIN):source.index(W._END)+len(W._END)], W.WAIT_SCRIPT_SOURCE)
        ast.parse(W.WAIT_SCRIPT_SOURCE, feature_version=(3, 7))

    def test_old_task_recovery_does_not_overwrite_replacement_session_activity(self):
        writer = V.writer(self.spec.name, "codex")
        writer.turn_opened("new-task", "send")
        path = self.root / "old.events.jsonl"
        T.save_receipt(path, {"spec": self.spec.model_copy(update={"session_id": "old-thread"}).model_dump(mode="json")})
        state = AsyncTaskState(task_id="old-task", pair_name=self.spec.name, message="old", status="done",
                               started_at=datetime.utcnow(), events_path=str(path), recovered_at=datetime.utcnow())
        before = writer.snapshot()
        C.record_recovered_activity(state)
        self.assertEqual(writer.snapshot(), before)
        R.remove_pair(self.spec.name)
        C.record_recovered_activity(state)
        self.assertEqual(writer.snapshot(), before)

    def test_standalone_waiter_exits_eight_on_live_codex_crossing(self):
        tid = str(uuid.uuid4())
        A._save(AsyncTaskState(task_id=tid, pair_name=self.spec.name, message="watch", status="running",
                               started_at=datetime.utcnow(), owner_pid=os.getpid()))
        turn = C._CodexActivity(self.spec, tid)
        turn.poll(force=True)
        turn.writer.flush()
        script = self.root / "watch.py"
        # Instrument only readiness. The assembled watcher itself stays stdlib
        # and reads the real activity files; no sleep-based baseline race.
        script.write_text("g={'__name__':'offline_wait'}\n" + f"exec({W.WAIT_SCRIPT_SOURCE!r}, g)\n"
                          "read=g['pair_activity_view']\nready=False\n"
                          "def view(*a,**k):\n global ready\n value=read(*a,**k)\n"
                          " if not ready:\n  ready=True\n  print('READY',flush=True)\n return value\n"
                          "g['pair_activity_view']=view\n"
                          f"raise SystemExit(g['main']([{self.spec.name!r},'--context-alert','40','--poll','.5','--timeout','10']))\n",
                          encoding="utf-8")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = subprocess.Popen([sys.executable, "-B", "-u", str(script)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **kwargs)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "READY")
            self.tokens(90_000)
            turn.poll(force=True)
            turn.writer.flush()
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 8, out + err)
            self.assertIn("CONTEXT ALERT", out)
            self.assertIn("180,000", out)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
            turn.close()


if __name__ == "__main__":
    unittest.main()
