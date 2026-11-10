"""v0.23 context-cost regressions; synthetic state, never live model calls."""
from contextlib import ExitStack
from datetime import datetime
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".test-homes"
SCRATCH.mkdir(exist_ok=True)
HOME = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="context-cost-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
sys.path.insert(0, str(ROOT / "src"))

from claude_squared import server as S, registry as R, async_tasks as A, runtime as RT
from claude_squared import _auto_watch as H, _auto_watch_script as I, _wait_script as W
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.errors import PairError
from claude_squared.models import AsyncTaskState, PairSpec, SendResult, PermissionDenial


def tool(fn):
    return getattr(fn, "fn", fn)


class ContextCostTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=HOME.name)))
        self.stack.enter_context(patch.dict(os.environ, {
            "CLAUDE_HOME": str(self.home), "CODEX_HOME": str(self.home / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.home), "CLAUDE_SQUARED_PAIR": "",
        }))
        self.stack.enter_context(patch.object(S, "_detect_parent_model", return_value=None))
        self.stack.enter_context(patch.object(RT.subprocess, "Popen", side_effect=AssertionError("No live CLI")))
        S._premium_seen.clear()
        S._observed_models.clear()
        self.spec = PairSpec(name="scout", model="fable", session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(self.spec)
        self.pairs = R.pairs_dir()
        self.wait = W.install_wait_script(self.pairs / "wait.py")
        self.reader = runpy.run_path(str(self.wait))
        self.tid = str(uuid.uuid4())

    def reply(self, **kwargs):
        return SendResult(name="scout", response="Done.", session_id=self.spec.session_id,
                          model_used="claude-fable-5-1", duration_ms=1234, **kwargs)

    def state(self, reply=None, status="done"):
        state = AsyncTaskState(task_id=self.tid, pair_name="scout", message="task", status=status,
                               started_at=datetime.utcnow(), result=reply, owner_pid=os.getpid())
        directory = self.pairs / "async"
        directory.mkdir(exist_ok=True)
        (directory / (self.tid + ".json")).write_text(state.model_dump_json(), encoding="utf-8")
        return state

    def payload(self):
        return {"hook_event_name": "PostToolUse", "session_id": "manager",
                "tool_name": "mcp__pair__pair_send_async", "tool_input": {"name": "scout"},
                "tool_response": f"Async task: {self.tid}\nscout: fable (resolution pending); not armed"}

    def view(self, used=140000, idle=False):
        return {"context": {"used": used, "window": 200000}, "verdict": "idle" if idle else "busy",
                "turns": [] if idle else [{"task_id": self.tid}], "running_tasks": [],
                "window_turns": [], "losses": []}

    def test_notice_is_short_and_keeps_task_context_and_first_line(self):
        reply = self.reply()
        reply.response = "A" * 150 + "\nNever include this second line"
        self.state(reply)
        reader = {**self.reader, "pair_activity_view": lambda *a: self.view()}
        notice = H._notice(self.pairs, "scout", 0, "long wait output" * 100, reader)
        self.assertIn("idle (wait.py exit 0)", notice)
        self.assertIn("70% over alert", notice)
        self.assertIn(self.tid, notice)
        self.assertIn("A" * 120, notice)
        self.assertNotIn("A" * 121, notice)
        self.assertNotIn("second line", notice)
        self.assertLess(len(notice), 310)

    def test_wait_ignore_alert_reaches_idle_and_default_still_exits_eight(self):
        for flags, expected in (([], 8), (["--ignore-alert"], 0), (["--alert-only"], 8)):
            views = iter([self.view(80000), self.view(140000), self.view(140000, idle=True)])
            namespace = self.reader["wait_pair"].__globals__
            with patch.dict(namespace, {"pair_activity_view": lambda *a, **kw: next(views),
                    "_registry_read": lambda name: (True, {"context_alert_at": 100000}),
                    "_idle_exit": lambda *a: 0, "print": lambda *a, **kw: None,
                    "time": SimpleNamespace(time=lambda: 0, monotonic=lambda: 0, sleep=lambda s: None)}):
                alert = self.reader["Alert"]("default", None)
                self.assertEqual(self.reader["wait_pair"]("scout", False, 10, 0, alert, flags), expected)

    def test_alert_is_silent_if_turn_finishes_at_crossing(self):
        views = iter([self.view(80000), self.view(140000, idle=True)])
        namespace = self.reader["wait_pair"].__globals__
        with patch.dict(namespace, {"pair_activity_view": lambda *a, **kw: next(views),
                "_registry_read": lambda name: (True, {}), "print": Mock(),
                "time": SimpleNamespace(time=lambda: 0, monotonic=lambda: 0, sleep=lambda s: None)}):
            self.assertEqual(self.reader["wait_pair"]("scout", False, 10, 0,
                             self.reader["Alert"]("default", None), ["--alert-only"]), 0)
            namespace["print"].assert_not_called()

    def test_hook_modes_dedupe_independently_and_alert_fires_once(self):
        self.state(self.reply())
        reader = {**self.reader, "pair_activity_view": lambda *a: self.view()}
        with patch.object(H, "_host_identity", return_value={}), patch.object(H, "_wait", return_value=(8, "")) as wait:
            output = io.StringIO()
            self.assertEqual(H.watch(self.payload(), self.pairs, reader=reader, output=output, alert_only=True), 2)
            self.assertIn("30,000 tokens to auto-compact", output.getvalue())
            self.assertIn("pair_stop → pair_compact/pair_clear → re-send", output.getvalue())
            self.assertEqual(H.watch(self.payload(), self.pairs, reader=reader, output=output, alert_only=True), 0)
            wait.assert_called_once()
            wait.return_value = (0, "")
            self.assertEqual(H.watch(self.payload(), self.pairs, reader=reader, output=output), 2)
            self.assertIn("idle (wait.py exit 0)", output.getvalue())

    def test_wait_and_resume_pass_ignore_alert(self):
        self.wait.write_text("def main(argv):\n assert '--ignore-alert' in argv\n return 0\n", encoding="utf-8")
        self.assertEqual(H._wait(self.pairs, "scout", 1), (0, ""))
        import threading
        self.assertEqual(H._recovery_wait(self.pairs, "scout", 1, threading.Event()), (0, ""))

    def test_alert_and_completion_can_wait_concurrently(self):
        self.state(self.reply())
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        def wait(*args, alert_only=False):
            entered[int(alert_only)].set()
            release.wait(5)
            return (8 if alert_only else 0), ""
        results = []
        def run(alert):
            results.append(H.watch(self.payload(), self.pairs, reader=self.reader,
                                   output=io.StringIO(), alert_only=alert))
        with patch.object(H, "_host_identity", return_value={}), patch.object(H, "_wait", side_effect=wait):
            threads = [threading.Thread(target=run, args=(mode,)) for mode in (False, True)]
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(all(event.wait(3) for event in entered))
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
        self.assertEqual(results, [2, 2])

    def test_armed_detection_requires_both_entries(self):
        script = I.install_auto_watch_script(self.pairs / "auto_watch.py")
        hooks = []
        settings = {"hooks": {"PostToolUse": [{"matcher": H.TOOL_PATTERN, "hooks": hooks}]}}
        for flag, expected in (("", False), (" --alert-only", True)):
            hooks.append({"type": "command", "command": f'python "{script.as_posix()}"{flag}',
                          "asyncRewake": True, "timeout": H.HOOK_TIMEOUT_SECONDS})
            (self.home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            self.assertEqual(I.auto_watch_configured(script, "pair_send_async"), expected)

    def test_footers_one_line_each_and_verbose_details_preserved(self):
        reply = self.reply(background_launches=["Bash"], permission_denials=[PermissionDenial(tool_name="Bash", tool_input={})],
                           self_woken_completed=[{"task_id": "abc", "response_preview": "secret detail", "status": "done"}])
        short = S._fmt_send_result(reply)
        full = S._fmt_send_result(reply, verbose=True)
        self.assertIn("claude-fable-5-1", short)
        self.assertIn("USER's explicit authorization", short)
        self.assertNotIn("secret detail", short)
        self.assertIn("secret detail", full)
        self.assertIn("Do NOT retry autonomously", full)
        self.assertLess(len(short), len(full) // 2)

    def test_list_filters_are_path_aware_and_purpose_is_bounded(self):
        R.update_pair("scout", purpose="z" * 300)
        R.add_pair(self.spec.model_copy(update={"name": "scout-child", "cwd": str(self.home / "child")}))
        R.add_pair(self.spec.model_copy(update={"name": "sibling", "cwd": str(self.home) + "-other"}))
        listing = tool(S.pair_list)(cwd=str(self.home), prefix="scout")
        self.assertIn("2 pairs", listing)
        self.assertNotIn("sibling", listing)
        self.assertNotIn("z" * 81, listing)
        verbose = json.loads(tool(S.pair_list)(cwd=str(self.home), prefix="scout", verbose=True))
        self.assertEqual(verbose["pairs"][0]["purpose"], "z" * 300)

    def test_ack_is_two_lines_and_model_resolution_is_honest(self):
        with patch.object(ClaudeAdapter, "read_resolved_model", return_value="claude-fable-5-1"):
            model = S._ack_model(self.spec)
        self.assertIn("claude-fable-5-1 (last observed)", model)
        ack = S._format_async_handle(self.tid, "", "scout", model_hint=model)
        self.assertEqual(len(ack.splitlines()), 2)
        self.assertIn("not armed", ack)
        self.assertLess(len(ack), 200)
        with patch.object(ClaudeAdapter, "read_resolved_model", return_value=None):
            self.assertIn("resolution pending", S._ack_model(self.spec))
        with patch.object(S.codex_models, "resolve_codex_model", return_value=("gpt-6.1-sol", None)):
            self.assertEqual(S._ack_model(self.spec.model_copy(update={"backend": "codex", "model": "sol"})), "gpt-6.1-sol")

    def test_known_newer_alias_and_transcript_model(self):
        R.add_pair(self.spec.model_copy(update={"name": "newer", "model": "claude-fable-5-5"}))
        self.assertIn("claude-fable-5-5", S._alias_resolution_note(self.spec, "claude-fable-5-1"))
        adapter = ClaudeAdapter()
        path = adapter.transcript_path(self.spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(event) for event in [
            {"type": "assistant", "message": {"model": "claude-fable-5-1"}},
            {"type": "assistant", "parent_tool_use_id": "sub", "message": {"model": "claude-haiku-5-5"}},
            {"type": "assistant", "message": {"model": "<synthetic>"}},
        ]), encoding="utf-8")
        self.assertEqual(adapter.read_resolved_model(self.spec), "claude-fable-5-1")
        result = adapter._build_send_result(self.spec, {"result": "Done", "duration_ms": 1})
        self.assertEqual(result.model_used, "claude-fable-5-1")
        result = adapter._build_send_result(self.spec, {"result": "Done", "duration_ms": 1,
                                                      "_resolved_model": "claude-fable-5-5"})
        self.assertEqual(result.model_used, "claude-fable-5-5")

    def test_stream_model_is_scoped_to_top_level_result(self):
        rt = RT.PairRuntime(self.spec, ClaudeAdapter())
        rt._current_scope = RT.TurnLogScope(rt.main_log_path, 1)
        scope = rt._current_scope
        scope.task_id = self.tid
        scope.input_sent = True
        rt._on_event_for_log(json.dumps({"type": "system", "subtype": "init", "model": "claude-fable-5-1"}))
        self.assertEqual(rt._resolved_model, "claude-fable-5-1")
        for event in (
            {"type": "assistant", "message": {"model": "claude-fable-5-5", "content": []}},
            {"type": "assistant", "parent_tool_use_id": "sub", "message": {"model": "claude-haiku-5-5", "content": []}},
            {"type": "result", "subtype": "success", "result": "done", "duration_ms": 1},
        ):
            rt._on_event_for_log(json.dumps(event))
        self.assertEqual(rt._resolved_model, "claude-fable-5-5")
        self.assertEqual(scope.result_snapshot["_resolved_model"], "claude-fable-5-5")

    def test_model_update_does_not_label_previous_pin_as_next_model(self):
        spec = self.spec.model_copy(update={"model": "claude-fable-5-5"})
        with patch.object(ClaudeAdapter, "read_resolved_model", return_value="claude-fable-5-1"):
            self.assertEqual(S._ack_model(spec), "claude-fable-5-5 (resolution pending)")

    def test_update_only_changed_fields_and_premium_once(self):
        update = tool(S.pair_update)
        first = update("scout", model="claude-fable-5-5")
        self.assertIn("PREMIUM MODEL", first)
        self.assertNotIn("last_drift_notice", first)
        self.assertNotIn("effort=", first)
        update("scout", model="opus")
        second = update("scout", model="claude-fable-5-5")
        self.assertNotIn("PREMIUM MODEL", second)
        self.assertIn("No fields", update("scout", purpose=self.spec.purpose))

    def test_cwd_send_moves_claude_transcript_and_enqueues_after_update(self):
        adapter = ClaudeAdapter()
        old_path = adapter.transcript_path(self.spec)
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_text('{"type":"user"}\n', encoding="utf-8")
        dest = str(self.home / "new")
        state = self.state(status="running")
        (self.pairs / "async" / (self.tid + ".json")).unlink()
        def start(name, message, runner, **kwargs):
            self.assertEqual(R.get_pair(name).cwd, dest)
            self.assertTrue(adapter.transcript_path(R.get_pair(name)).exists())
            return state
        with patch.object(A, "start_task", side_effect=start), patch.object(S.completion, "arm", return_value=None):
            answer = tool(S.pair_send_async)("scout", "go", cwd=dest)
        self.assertIn(self.tid, answer)
        self.assertFalse(old_path.exists())

    def test_cwd_send_refuses_running_background_and_failed_move(self):
        send = tool(S.pair_send_async)
        self.state(status="running")
        with patch.object(A, "start_task") as start:
            with self.assertRaisesRegex(PairError, "busy"):
                send("scout", "go", cwd=str(self.home / "new"))
            start.assert_not_called()
        (self.pairs / "async" / (self.tid + ".json")).unlink()
        with patch.object(S, "_refuse_if_background", side_effect=PairError("pending background")):
            with self.assertRaisesRegex(PairError, "background"):
                send("scout", "go", cwd=str(self.home / "new"))
        with patch.object(S, "_move_session_jsonl_for_cwd_change", side_effect=OSError("locked")):
            with self.assertRaisesRegex(PairError, "Could not change cwd"):
                send("scout", "go", cwd=str(self.home / "new"))
        self.assertEqual(R.get_pair("scout").cwd, str(self.home))

    def test_summary_marked_fallback_default_and_exact_utf8_report(self):
        reply = self.reply()
        reply.response = "Long details ☃\n<summary>Ready to review.</summary>\nMore details"
        reply.permission_denials = [PermissionDenial(tool_name="Bash")]
        self.state(reply)
        summary = tool(S.pair_poll)("scout", summary=True)
        self.assertIn("Ready to review.", summary)
        self.assertIn("USER's explicit authorization", summary)
        self.assertNotIn("Long details", summary)
        path = self.pairs / "reports" / "scout" / (self.tid + ".md")
        self.assertEqual(path.read_bytes(), reply.response.encode("utf-8"))
        self.assertIn(f"{path.stat().st_size} bytes", summary)
        self.assertIn(reply.response, tool(S.pair_poll)(self.tid))
        reply.response = "\n".join(f"line {i}" for i in range(40))
        self.state(reply)
        summary = tool(S.pair_poll)(self.tid[:8], summary=True)
        self.assertIn("line 19", summary)
        self.assertNotIn("line 20", summary)
        verbose = json.loads(tool(S.pair_poll)(self.tid, verbose=True))
        self.assertEqual(verbose["result"]["response"], reply.response)


if __name__ == "__main__":
    unittest.main()
