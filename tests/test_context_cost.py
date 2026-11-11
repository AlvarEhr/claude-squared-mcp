"""v0.23 context-cost regressions; synthetic state, never live model calls."""
from contextlib import ExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
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
        self.assertLess(len(notice), 390)

    def test_wait_ignore_alert_reaches_idle_and_default_still_exits_eight(self):
        for flags, expected in (([], 8), (["--ignore-alert"], 0)):
            views = iter([self.view(80000), self.view(140000), self.view(140000, idle=True)])
            namespace = self.reader["wait_pair"].__globals__
            with patch.dict(namespace, {"pair_activity_view": lambda *a, **kw: next(views),
                    "_registry_read": lambda name: (True, {"context_alert_at": 100000}),
                    "_idle_exit": lambda *a: 0, "print": lambda *a, **kw: None,
                    "time": SimpleNamespace(time=lambda: 0, monotonic=lambda: 0, sleep=lambda s: None)}):
                alert = self.reader["Alert"]("default", None)
                self.assertEqual(self.reader["wait_pair"]("scout", False, 10, 0, alert, flags), expected)

    def test_wait_and_resume_pass_ignore_alert(self):
        self.wait.write_text("def main(argv):\n assert '--ignore-alert' in argv\n return 0\n", encoding="utf-8")
        self.assertEqual(H._wait(self.pairs, "scout", 1), (0, ""))
        self.assertEqual(H._recovery_wait(self.pairs, "scout", 1, threading.Event()), (0, ""))

    def test_advisory_boundary_and_disabled_setting_agree_in_ack_and_notice(self):
        self.state(self.reply())
        R.update_pair("scout", context_alert_at=130000)
        for setting, used, expected in ((None, 80000, False), (None, 86000, True),
                                        (0.5, 86000, False), (0.5, 102000, True),
                                        (0, 190000, False)):
            with self.subTest(setting=setting, used=used):
                if setting is not None:
                    tool(S.pair_settings_set)(context_advisory_at=setting)
                view = self.view(used)
                with patch.object(S.activity_mod, "pair_activity_view", return_value=view):
                    ack = S._format_async_handle(self.tid, "", "scout", model_hint="claude-fable-5-1")
                notice = H._notice(self.pairs, "scout", 0, "", {
                    **self.reader, "pair_activity_view": lambda *a: view})
                for output in (ack, notice):
                    self.assertEqual("consider pair_clear/pair_compact" in output, expected)
                    if expected:
                        self.assertIn(f"ctx {used * 100 / 200000:.0f}%", output)
                        self.assertIn("before the next large task", output)
                self.assertEqual(len(ack.splitlines()), 2)
        self.assertEqual(R.get_pair("scout").context_alert_at, 130000)
        self.assertIn("over alert", notice)  # disabling the advisory leaves the existing alert intact

    def test_advisory_settings_validate_persist_reset_and_describe_scope(self):
        setter = tool(S.pair_settings_set)
        self.assertIn("0.4", tool(S.pair_settings_get)())
        for invalid in (-0.1, 1.01, float("nan"), float("inf")):
            with self.subTest(value=invalid), self.assertRaises(PairError):
                setter(context_advisory_at=invalid)
        text = setter(context_advisory_at=0.6)
        self.assertIn("ALL pairs", text)
        data = json.loads(tool(S.pair_settings_get)(verbose=True))
        self.assertEqual(data["writable_defaults"]["context_advisory_at"], 0.6)
        self.assertEqual(data["hardcoded_fallbacks"]["context_advisory_at"], 0.4)
        S.settings_mod.reset_defaults()
        self.assertIsNone(S.settings_mod.load_defaults().context_advisory_at)
        self.assertTrue(H.context_advisory(86000, 200000))

    def test_advisory_uses_reported_window_and_tolerates_missing_context(self):
        for view, expected in (({"context": {"used": 86000}, "reported_window": {"tokens": 200000}}, True),
                               ({"context": {"used": 86000}}, False), ({}, False)):
            with patch.object(S.activity_mod, "pair_activity_view", return_value=view):
                self.assertEqual(bool(S._context_advisory_for_pair("scout")), expected)

    def test_context_from_a_cleared_session_is_not_reported(self):
        # 2026-11-11: the ack right after pair_clear still said "ctx 58%".
        base = {"used": 120000, "window": 200000}
        for measured, expected in ((self.spec.session_id, True), ("old-session", False), (None, True)):
            ctx = dict(base, **({"session_id": measured} if measured else {}))
            view = {"context": ctx}
            with self.subTest(measured=measured), \
                    patch.object(S.activity_mod, "pair_activity_view", return_value=view), \
                    patch.object(S.activity_mod, "view", return_value=view):
                self.assertEqual(bool(S._context_advisory_for_pair("scout")), expected)
                status = "\n".join(S._activity_status_lines("scout"))
                self.assertEqual("120,000" in status, expected)
                self.assertEqual("not measured since the session changed" in status, not expected)

    def test_writer_records_the_measured_session(self):
        writer = S.activity_mod.ActivityWriter("scout", "claude", self.pairs)  # unregistered: no flusher
        writer.note_context(1000, 200000, window_source="reported", source="stream", session_id="sid-1")
        self.assertEqual(writer._state["context"]["session_id"], "sid-1")

    def test_single_documented_hook_is_detected(self):
        script = I.install_auto_watch_script(self.pairs / "auto_watch.py")
        data = json.loads((ROOT / "docs/auto-watch.settings.json").read_text(encoding="utf-8"))
        hooks = data["hooks"]["PostToolUse"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(len(hooks[0]["hooks"]), 1)
        hooks[0]["hooks"][0]["command"] = f'python "{script.as_posix()}"'
        (self.home / "settings.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(I.auto_watch_configured(script, "pair_send_async"))

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

    def test_cwd_filter_skips_adopted_pair_without_cwd(self):
        R.add_pair(self.spec.model_copy(update={"name": "adopted", "cwd": None}))
        self.assertIn("adopted", tool(S.pair_list)())
        self.assertIn("adopted", tool(S.pair_list)(prefix="adopt"))
        for verbose in (False, True):
            listing = tool(S.pair_list)(cwd=str(self.home), verbose=verbose)
            self.assertNotIn("adopted", listing)
            self.assertIn("scout", listing)
        self.assertEqual(tool(S.pair_list)(cwd=str(self.home), prefix="adopt"), "(no pairs registered)")

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
        extra = [str(self.home / "sibling")]
        state = self.state(status="running")
        (self.pairs / "async" / (self.tid + ".json")).unlink()
        def start(name, message, runner, **kwargs):
            self.assertEqual(R.get_pair(name).cwd, dest)
            self.assertEqual(R.get_pair(name).extra_dirs, extra)
            self.assertTrue(adapter.transcript_path(R.get_pair(name)).exists())
            return state
        with patch.object(A, "start_task", side_effect=start), patch.object(S.completion, "arm", return_value=None):
            answer = tool(S.pair_send_async)("scout", "go", cwd=dest, extra_dirs=extra)
        self.assertIn(self.tid, answer)
        self.assertFalse(old_path.exists())

    def test_extra_dirs_only_replaces_normalizes_and_clears_before_enqueue(self):
        state = self.state(status="running")
        (self.pairs / "async" / (self.tid + ".json")).unlink()
        expected = [str(self.home / "sibling")]
        def start(name, message, runner, **kwargs):
            spec = R.get_pair(name)
            self.assertEqual(spec.cwd, self.spec.cwd)
            self.assertEqual(spec.extra_dirs, expected)
            return state
        with patch.object(A, "start_task", side_effect=start), patch.object(S.completion, "arm", return_value=None), \
                patch.object(S, "_move_session_jsonl_for_cwd_change") as move:
            tool(S.pair_send_async)("scout", "review", extra_dirs=[str(self.home / "sibling" / "child" / "..")])
            expected = None
            tool(S.pair_send_async)("scout", "review", extra_dirs=[])
            move.assert_not_called()

    def test_extra_dirs_busy_background_and_steer_refuse_without_mutation(self):
        send = tool(S.pair_send_async)
        extra = [str(self.home / "sibling")]
        R.update_pair("scout", extra_dirs=extra)
        self.state(status="running")
        with patch.object(A, "start_task") as start:
            for paths in ({"extra_dirs": extra}, {"extra_dirs": []},
                          {"cwd": str(self.home / "new"), "extra_dirs": []}):
                with self.subTest(paths=paths), self.assertRaisesRegex(PairError, "busy"):
                    send("scout", "go", **paths)
            start.assert_not_called()
        (self.pairs / "async" / (self.tid + ".json")).unlink()
        with patch.object(S, "_refuse_if_background", side_effect=PairError("pending background")), \
                patch.object(A, "start_task") as start:
            with self.assertRaisesRegex(PairError, "background"):
                send("scout", "go", extra_dirs=[])
            start.assert_not_called()
        with self.assertRaisesRegex(PairError, "steer"):
            send("scout", "go", extra_dirs=extra, steer=True)
        self.assertEqual(R.get_pair("scout").extra_dirs, extra)
        self.assertEqual(R.get_pair("scout").cwd, self.spec.cwd)

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
