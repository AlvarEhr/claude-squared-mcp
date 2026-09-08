"""Offline regressions for the Codex maintenance branch; no live model calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-maintenance-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = TEST_HOME.name

from claude_squared import async_tasks as A, registry as R, server as S
from claude_squared import __main__ as CLI
from claude_squared import codex_models as CM
from claude_squared.adapters import codex as C
from claude_squared.errors import PairError, TaskStopped
from claude_squared.models import AsyncTaskState, PairSpec, SendResult
from claude_squared.runtime import ToolCounter
from claude_squared.tool_details import codex_tool_detail


def tool(value):
    return getattr(value, "fn", value)


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=TEST_HOME.name))
        self.environment = patch.dict(os.environ, {"CLAUDE_HOME": str(self.directory)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        codex_home = self.directory / "codex-fixture"
        codex_home.mkdir()
        for module in (CM, C):
            home_patch = patch.object(module, "codex_home", return_value=codex_home)
            home_patch.start()
            self.addCleanup(home_patch.stop)

    def spec(self, name="pair"):
        return PairSpec(name=name, session_id="session-1", cwd=str(self.directory))


class RegistryTests(IsolatedCase):
    def write_registry(self, data):
        path = R.registry_path()
        path.write_text(json.dumps(data), encoding="utf-8")
        return path, path.read_bytes()

    def assert_preserved(self, data):
        path, original = self.write_registry(data)
        R.load()
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaisesRegex(PairError, "refusing to write"):
            R.add_pair(self.spec("new"))
        self.assertEqual(path.read_bytes(), original)
        backups = list(path.parent.glob("registry.corrupt-*.json"))
        self.assertTrue(backups)
        self.assertTrue(any(p.read_bytes() == original for p in backups))

    def test_unreadable_entries_are_quarantined_verbatim_and_writes_continue(self):
        # v0.14.0 review decision: an entry that fails validation is kept on
        # disk untouched and hidden from the tools; the REST of the registry
        # keeps working (the maintenance branch froze every write instead).
        for version in (2, 3):
            with self.subTest(version=version):
                bad = {"model": "opus", "future_field": [1, 2]}
                path, _ = self.write_registry({"version": version, "pairs": {
                    "good": {"session_id": "s"}, "bad": bad}})
                loaded = R.load()
                self.assertEqual(set(loaded.pairs), {"good"})
                self.assertIn("bad", R.quarantined())
                R.add_pair(self.spec("new"))  # writes are not frozen
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(raw["version"], 3)
                self.assertEqual(set(raw["pairs"]), {"good", "bad", "new"})
                self.assertEqual(raw["pairs"]["bad"], bad)  # verbatim, no injected name
                with self.assertRaisesRegex(PairError, "unreadable"):
                    R.get_pair("bad")
                with self.assertRaisesRegex(PairError, "unreadable"):
                    R.add_pair(self.spec("bad"))
                # Repairing the file by hand clears the quarantine on the next load.
                path.write_text('{"version":3,"pairs":{}}', encoding="utf-8")
                R.load()
                self.assertEqual(R.quarantined(), {})

    def test_future_version_is_readable_but_never_written(self):
        self.assert_preserved({"version": 99, "pairs": {"good": {"session_id": "s"}}})
        self.assertIn("good", R.load().pairs)

    def test_invalid_root_and_pairs_shapes_are_preserved(self):
        for data in ([], {"pairs": []}, {"version": "bad", "pairs": {}}):
            with self.subTest(data=data):
                path, original = self.write_registry(data)
                R.load()
                with self.assertRaises(PairError):
                    R.add_pair(self.spec("new"))
                self.assertEqual(path.read_bytes(), original)

    def test_unknown_permission_does_not_become_auto(self):
        with self.assertRaises(ValueError):
            PairSpec(name="bad", session_id="s", permission_mode="read-onyl")
        bad = {"session_id": "s", "permission_mode": "read-onyl"}
        path, _ = self.write_registry({"version": 3, "pairs": {"bad": bad}})
        self.assertNotIn("bad", R.load().pairs)  # never widened to auto
        R.add_pair(self.spec("new"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pairs"]["bad"], bad)

    def test_additive_fields_survive_mutation_including_null(self):
        self.write_registry({"version": 3, "future_root": None, "pairs": {
            "pair": {"session_id": "s", "future_pair": {"x": 1}, "future_null": None}}})
        R.update_pair("pair", purpose="changed")
        raw = json.loads(R.registry_path().read_text(encoding="utf-8"))
        self.assertIn("future_root", raw)
        self.assertIn("future_null", raw["pairs"]["pair"])
        self.assertEqual(raw["pairs"]["pair"]["future_pair"], {"x": 1})

    def test_valid_legacy_registry_migrates_with_backup(self):
        path, original = self.write_registry({"version": 2, "pairs": {
            "pair": {"session_id": "s", "model": "opus[1m]", "permission_mode": "acceptEdits"}}})
        loaded = R.load()
        self.assertEqual(loaded.pairs["pair"].permission_mode, "workspace")
        self.assertEqual(loaded.pairs["pair"].context_window, "1m")
        self.assertEqual(json.loads(path.read_text())["version"], 3)
        self.assertEqual(path.with_name("registry.v2.backup.json").read_bytes(), original)

    def test_unsafe_registry_blocks_backend_work_and_releases_pair_lock(self):
        # Root-level damage (here: a newer version) still refuses all writes
        # BEFORE any backend work starts, and the pair lock is released.
        path, _ = self.write_registry({"version": 99, "pairs": {"pair": {"session_id": "s"}}})
        runner = S._build_send_runner("pair", "must not execute", hard_timeout_seconds=None,
            override_model=None, override_effort=None, override_permission_mode=None)
        with patch.object(S, "_adapter_for") as adapter:
            with self.assertRaisesRegex(PairError, "refusing to write"):
                runner()
            adapter.assert_not_called()
        path.write_text('{"version":3,"pairs":{}}', encoding="utf-8")
        with S._with_pair_lock("pair", timeout_s=0.2):
            pass

    def test_quarantined_entry_does_not_block_other_pairs(self):
        bad = {"permission_mode": "read-onyl"}
        path, _ = self.write_registry({"version": 3, "pairs": {
            "pair": {"session_id": "s"}, "bad": bad}})
        with S._with_pair_lock("pair", timeout_s=0.2):
            pass  # the writability preflight passes
        R.update_pair("pair", purpose="still works")
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["pairs"]["pair"]["purpose"], "still works")
        self.assertEqual(raw["pairs"]["bad"], bad)
        listing = tool(S.pair_list)()
        self.assertIn("bad (UNREADABLE", listing)


class CancellationTests(IsolatedCase):
    def test_transient_replace_denial_still_publishes_terminal_state(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def runner(tid):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test runner not released")
            return SendResult(name="pair", session_id="s", response="durable completion",
                              model_used="opus", duration_ms=1)
        task = A.start_task("pair", "test", runner)
        self.assertTrue(entered.wait(5))
        denied = []
        replace = Path.replace
        def once(path, target):
            if str(target).endswith(task.task_id + ".json") and not denied:
                denied.append(True)
                raise PermissionError("simulated Windows reader sharing conflict")
            return replace(path, target)
        event = A._get_or_create_event(task.task_id)
        with patch.object(Path, "replace", once):
            release.set()
            self.assertTrue(event.wait(5))
        self.assertTrue(denied)
        # Read the file itself, not the local unsaved-result fallback.
        disk = json.loads(A._task_path(task.task_id).read_text(encoding="utf-8"))
        self.assertEqual(disk["status"], "done")
        self.assertEqual(disk["result"]["response"], "durable completion")

    def run_queue_case(self, drain):
        spec = self.spec()
        R.add_pair(spec)
        entered = threading.Event()
        calls = []

        class Adapter:
            def send(self, current, message, **kwargs):
                calls.append(message)
                if message == "first":
                    entered.set()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if kwargs["should_stop"]():
                            raise TaskStopped("interrupted")
                        time.sleep(0.01)
                    raise AssertionError("stop request never reached the active task")
                return SendResult(name=current.name, session_id=current.session_id,
                    response=message, model_used="opus", cost_usd=0, duration_ms=1)

        runtime = SimpleNamespace(get_or_none=lambda name: None, evict=lambda name: None)
        with patch.object(S, "_adapter_for", return_value=Adapter()), patch.object(S.runtime_mod, "registry", return_value=runtime):
            def start(message):
                runner = S._build_send_runner("pair", message, hard_timeout_seconds=None,
                    override_model=None, override_effort=None, override_permission_mode=None)
                return A.start_task("pair", message, runner, queued=True)
            first = start("first")
            self.assertTrue(entered.wait(5))
            second = start("second")
            self.assertTrue(A.load_task(second.task_id).queued)
            # A queued task must not borrow the running task's log.
            log = R.logs_dir() / "pair" / "main.log"
            log.parent.mkdir(exist_ok=True)
            log.write_text("PRIVATE_FIRST_TASK_LOG\n", encoding="utf-8")
            queued_view = tool(S.pair_poll)(second.task_id, with_turn_log=True)
            self.assertNotIn("PRIVATE_FIRST_TASK_LOG", queued_view)
            tool(S.pair_stop)("pair", drain_queue=drain)
            first_done = A.wait_for_task(first.task_id, 5)
            second_done = A.wait_for_task(second.task_id, 5)
        self.assertEqual(first_done.status, "stopped")
        if drain:
            self.assertEqual(calls, ["first"])
            self.assertEqual(second_done.status, "stopped")
            self.assertIsNone(second_done.execution_started_at)
        else:
            self.assertEqual(calls, ["first", "second"])
            self.assertEqual(second_done.status, "done")
            self.assertEqual(second_done.result.response, "second")

    def test_default_stop_preserves_queued_send(self):
        self.run_queue_case(False)

    def test_drained_queue_never_runs(self):
        self.run_queue_case(True)

    def test_foreign_process_can_cancel_a_queued_task(self):
        R.add_pair(self.spec())
        selector = patch.object(S, "_adapter_for", side_effect=AssertionError("cancelled task reached backend"))
        selected = selector.start()
        self.addCleanup(selector.stop)
        with S._with_pair_lock("pair"):
            runner = S._build_send_runner("pair", "must-not-run", hard_timeout_seconds=None,
                override_model=None, override_effort=None, override_permission_mode=None)
            task = A.start_task("pair", "queued", runner, queued=True)
            result = subprocess.run([sys.executable, "-B", "-c",
                "from claude_squared.async_tasks import request_task_stop; import sys; "
                "assert request_task_stop(sys.argv[1])", task.task_id],
                env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        done = A.wait_for_task(task.task_id, 5)
        self.assertEqual(done.status, "stopped")
        self.assertIsNone(done.execution_started_at)
        selected.assert_not_called()

    def test_result_is_retained_when_completion_races_stop(self):
        def runner(tid):
            A.request_task_stop(tid)
            return SendResult(name="pair", session_id="s", response="work already completed",
                              model_used="opus", cost_usd=0, duration_ms=1)
        task = A.start_task("pair", "race", runner)
        done = A.wait_for_task(task.task_id, 5)
        self.assertEqual(done.status, "stopped")
        self.assertIn("work already completed", tool(S.pair_poll)(task.task_id))
        started = time.monotonic()
        self.assertEqual(A.wait_for_task(task.task_id, 2).status, "stopped")
        self.assertLess(time.monotonic() - started, 0.5)


class CodexInspectionTests(IsolatedCase):
    def test_claude_result_distinguishes_native_and_fallback_windows(self):
        from claude_squared.adapters.claude import ClaudeAdapter
        adapter = ClaudeAdapter()
        with patch.object(adapter, "_read_last_turn_context_fill", return_value=123):
            native = adapter._build_send_result(self.spec(), {"result": "ok", "modelUsage": {
                "claude-opus-5": {"contextWindow": 1000000}}})
            fallback = adapter._build_send_result(self.spec(), {"result": "ok", "modelUsage": {
                "claude-opus-5": {}}})
        self.assertEqual(native.context.window_source, "reported")
        self.assertEqual(fallback.context.window_source, "estimated")

    def test_codex_markers_scope_the_next_live_turn(self):
        directory = R.logs_dir() / "pair"
        directory.mkdir()
        for marker in ("=== TURN COMPLETED (in 10 / cached 2 / out 3 / reasoning 0 tokens) ===",
                       "=== TURN FAILED: example ===", "=== TURN SUCCESS (15ms) ==="):
            (directory / "main.log").write_text("old\n" + marker + "\nnew\n", encoding="utf-8")
            lines, _ = S._read_current_or_last_turn_log("pair")
            self.assertEqual(lines, ["new"])

    def test_full_items_survive_restart_and_reused_cli_item_ids(self):
        directory = R.logs_dir() / "pair"
        directory.mkdir()
        counter = ToolCounter(directory / "main.idx.json")
        for n, body in enumerate(("A" * 700, "B" * 700), 1):
            counter = ToolCounter(directory / "main.idx.json")
            # Completion-only items with the same CLI id in distinct turns.
            C.CodexAdapter._format_event({"type": "item.completed", "item": {
                "id": "item_0", "type": "command_execution", "command": f"command-{n}",
                "aggregated_output": body, "exit_code": 0, "status": "completed"}},
                counter, {}, detail_dir=directory, run_id=f"task-{n}")
            text = codex_tool_detail(directory, str(n))
            self.assertIn(body, text)
            self.assertIn(f"task-{n}", text)
        self.assertIn("A" * 700, codex_tool_detail(directory, "T-1"))
        self.assertIn("B" * 700, codex_tool_detail(directory, "T-2"))
        self.assertIn("truncated", codex_tool_detail(directory, "T-2", max_chars=50))

    def test_codex_tool_dispatch_does_not_look_for_claude_jsonl(self):
        spec = PairSpec(name="pair", backend="codex", model="gpt-5.6-sol", session_id="s")
        R.add_pair(spec)
        with patch.object(S, "_resolve_main_jsonl_path", side_effect=AssertionError("Claude path used")):
            with self.assertRaisesRegex(PairError, "Older turns"):
                tool(S.pair_tool_detail)("pair", "T-1")

    def test_projection_failure_rolls_back_every_delete(self):
        db = self.directory / "history.sqlite"
        path = self.directory / "rollout.jsonl"
        path.write_text("kept\n")
        with sqlite3.connect(db) as con:
            con.executescript("create table thread_items(thread_id text, rollout_ordinal integer);"
                "insert into thread_items values('s',10);"
                "create table thread_turns(thread_id text, changed_column integer);"
                "create table thread_history_projection_state(thread_id text, next_rollout_ordinal integer, next_rollout_byte_offset integer);"
                "insert into thread_history_projection_state values('s',20,1000);")
        with patch.object(C, "_history_db_path", return_value=db):
            note = C.resync_history_after_truncate("s", 5, path)
        self.assertIn("failed", note)
        with sqlite3.connect(db) as con:
            self.assertEqual(con.execute("select count(*) from thread_items").fetchone()[0], 1)
            self.assertEqual(con.execute("select next_rollout_ordinal from thread_history_projection_state").fetchone()[0], 20)

    def test_captured_claude_window_overrides_old_200k_assumption(self):
        spec = self.spec()
        path = Path(CLI._transcript_path(spec))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-08T11:59:59Z",
            "message": {"model": "claude-opus-5", "usage": {"input_tokens": 180000}}}), encoding="utf-8")
        (R.async_dir() / "captured.json").write_text(json.dumps({"pair_name": "pair",
            "finished_at": "2026-09-08T12:00:00", "result": {"session_id": spec.session_id,
                "model_used": "claude-opus-5", "context": {"tokens_used": 180000, "tokens_max": 1000000,
                "window_source": "reported"}}}), encoding="utf-8")
        with patch("claude_squared.adapters.claude.ClaudeAdapter._read_last_turn_context_fill", return_value=180000):
            source = {}
            self.assertEqual(CLI._context_fill(spec, provenance=source), (180000, 1000000, 18.0))
            self.assertFalse(source["estimated_window"])

    def test_old_or_estimated_window_is_not_reported_as_observed(self):
        spec = self.spec()
        path = Path(CLI._transcript_path(spec))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-08T12:01:00Z",
            "message": {"model": "claude-opus-5", "usage": {"input_tokens": 500000}}}), encoding="utf-8")
        data = {"pair_name": "pair", "finished_at": "2026-09-08T12:00:00", "result": {
            "session_id": spec.session_id, "model_used": "claude-opus-5",
            "context": {"tokens_used": 500000, "tokens_max": 200000, "window_source": "reported"}}}
        record = R.async_dir() / "captured.json"
        record.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(CLI._observed_claude_window(spec, 500000))
        data["finished_at"] = "2026-09-08T12:02:00"
        for source in (None, "estimated"):
            data["result"]["context"]["window_source"] = source
            record.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(CLI._observed_claude_window(spec, 500000))

    def test_malformed_detail_sidecar_does_not_suppress_completion(self):
        directory = R.logs_dir() / "pair"
        directory.mkdir()
        for malformed in ([], None, 123):
            with self.subTest(root=malformed):
                counter = ToolCounter(directory / "main.idx.json")
                tags = {}
                item = {"id": "item_0", "type": "command_execution", "command": "test"}
                C.CodexAdapter._format_event({"type": "item.started", "item": item}, counter, tags,
                                            detail_dir=directory, run_id="run")
                tag = tags["item_0"]
                sidecar = directory / "codex-tool-items" / (tag + ".json")
                sidecar.write_text(json.dumps(malformed), encoding="utf-8")
                with self.assertRaisesRegex(PairError, "JSON object"):
                    codex_tool_detail(directory, tag)
                lines = C.CodexAdapter._format_event({"type": "item.completed", "item": {
                    **item, "status": "completed", "exit_code": 0, "aggregated_output": "COMPLETE"}},
                    counter, tags, detail_dir=directory, run_id="run")
                self.assertIn("COMPLETE", "\n".join(lines))
                self.assertIn("COMPLETE", codex_tool_detail(directory, tag))

    def test_detail_write_failure_does_not_suppress_normal_logging(self):
        event = {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
                 "command": "test", "status": "completed", "exit_code": 0, "aggregated_output": "COMPLETE"}}
        for error in (PermissionError("fixture"), ValueError("fixture")):
            with self.subTest(error=type(error).__name__), patch.object(C, "save_codex_item", side_effect=error):
                lines = C.CodexAdapter._format_event(event, ToolCounter(None), {}, detail_dir=self.directory)
                self.assertIn("COMPLETE", "\n".join(lines))
                self.assertIn("tool detail unavailable", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
