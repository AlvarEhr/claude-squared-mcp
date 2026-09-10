"""Offline handoff regressions: synthetic stores, fake stdio, no live CLIs."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-handoff-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = TEST_HOME.name  # imports have startup/atexit side effects

from claude_squared import async_tasks as A, codex_models as CM, registry as R, server as S
from claude_squared import connectors
from claude_squared.adapters import codex as C
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.errors import CLIError, PairAlreadyExists, PairError
from claude_squared.models import PairSpec, SendResult


def tool(value):
    return getattr(value, "fn", value)


class RecordingInput(io.BytesIO):
    def close(self):
        self.recorded = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, messages):
        self.stdin = RecordingInput()
        self.stdout = io.BytesIO(b"".join((json.dumps(m) + "\n").encode() for m in messages))
        self.stderr = io.BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.staged: list[Path] = []
        self.imports = 0
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.stack.enter_context(patch.dict(os.environ, {"CLAUDE_HOME": str(self.directory)}))
        home = self.directory / "codex"
        home.mkdir()
        for module in (C, CM):
            self.stack.enter_context(patch.object(module, "codex_home", return_value=home))
        cache = {"models": [{
            "slug": "gpt-5.6-sol", "visibility": "list", "priority": 1,
            "context_window": 100_000, "max_context_window": 400_000,
            "effective_context_window_percent": 100,
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "high", "max", "ultra")],
        }, {
            "slug": "gpt-5.6-luna", "visibility": "list", "priority": 2,
            "context_window": 100_000, "max_context_window": 400_000,
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "high")],
        }]}
        (home / "models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
        self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("live subprocess")))
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("live subprocess")))
        self.stack.enter_context(patch.object(C, "codex_executable", return_value="fixture-codex"))
        self.stack.enter_context(patch.object(C, "config_readd_args", return_value=[]))
        self.stack.enter_context(patch.object(connectors, "inventory", return_value=[]))
        self.runtime = SimpleNamespace(get_or_none=lambda name: None)
        self.stack.enter_context(patch.object(S.runtime_mod, "registry", return_value=self.runtime))
        self.source = PairSpec(name="source", session_id="claude-source", model="opus", effort="max",
                               cwd=str(self.directory), purpose="Fix the parser", extra_dirs=[str(home)])
        R.add_pair(self.source)
        self.source_path = ClaudeAdapter().transcript_path(self.source)
        self.source_path.parent.mkdir(parents=True, exist_ok=True)
        self.source_path.write_text('{"type":"user","message":{"content":"Fix parser"}}\n', encoding="utf-8")
        self.rollout = home / "imported.jsonl"
        self.stack.enter_context(patch.object(C, "rollout_path_for", return_value=self.rollout))
        self.importer = self.stack.enter_context(patch.object(C, "import_claude_session", side_effect=self.import_session))
        self.deleter = self.stack.enter_context(patch.object(C, "delete_thread"))
        self.sender = self.stack.enter_context(patch.object(C.CodexAdapter, "send", side_effect=self.send))
        self.registration = self.stack.enter_context(patch.object(R, "add_pair", wraps=R.add_pair))

    def write_rollout(self, text="Prior visible conversation", usage=None, extra=()):
        events = [{"type": "session_meta", "payload": {"id": "codex-target"}},
                  {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": text}]}}]
        events.extend(extra)
        if usage is not None:
            events.append({"type": "event_msg", "payload": {"type": "token_count", "info": usage}})
        self.rollout.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    def import_session(self, path, *, cwd, title):
        self.assertTrue(S._get_lock("source").locked(), "source lock must cover import")
        # A uniquely named snapshot copy beside the live file, never the live
        # file itself (Codex's importer keys on the path and would otherwise
        # skip or append into an earlier handoff's thread).
        self.assertNotEqual(path, self.source_path)
        self.assertEqual(path.parent, self.source_path.parent)
        self.assertEqual(path.read_bytes(), self.source_path.read_bytes())
        self.staged.append(path)
        self.assertEqual(cwd, self.source.cwd)
        self.write_rollout()
        self.imports += 1
        thread_id = "codex-target" if self.imports == 1 else f"codex-target-{self.imports}"
        return {"thread_id": thread_id, "import_id": f"import-{self.imports}",
                "imported_at": "2026-09-10T10:00:00Z"}

    def send(self, spec, message, **kwargs):
        self.assertFalse(S._get_lock("source").locked(), "source lock held across briefing")
        return SendResult(name=spec.name, session_id=spec.session_id, model_used="gpt-5.6-sol",
                          response="Fix the parser; finish edge cases.", duration_ms=1, cost_usd=None, backend="codex")

    def handoff(self, **kwargs):
        return tool(S.pair_handoff)("source", **kwargs)

    def test_success_lineage_defaults_and_source_untouched(self):
        R.update_pair("source", turn_count=17, total_cost_usd=4.5,
                      self_woken_pending=[{"task_id": "old", "status": "done"}])
        original = R.get_pair("source").model_dump()
        transcript = self.source_path.read_bytes()
        real_add = R.add_pair
        def add_after_unlock(spec):
            self.assertFalse(S._get_lock("source").locked(), "source lock held at registration")
            return real_add(spec)
        with patch.object(A, "start_task", wraps=A.start_task) as start, patch.object(R, "add_pair", side_effect=add_after_unlock):
            result = self.handoff(verbose=True)
        self.assertTrue(start.call_args.kwargs["queued"])
        payload = json.loads(result)
        target = R.get_pair("source-codex")
        self.assertEqual((target.backend, target.model, target.effort), ("codex", "sol", "max"))
        self.assertEqual(target.extra_dirs, self.source.extra_dirs)
        self.assertEqual(target.context_window, "default")
        self.assertEqual(target.turn_count, 1)  # only the new briefing
        self.assertEqual(target.total_cost_usd, 0)
        self.assertEqual(target.self_woken_pending, [])
        self.assertEqual(target.handoff_from["session_id"], "claude-source")
        self.assertEqual(target.handoff_from["import_id"], "import-1")
        self.assertEqual(R.get_pair("source").model_dump(), original)
        self.assertEqual(self.source_path.read_bytes(), transcript)
        self.assertIn("Fix the parser", payload["text"])
        self.assertIn("snapshot as of 2026-09-10T10:00:00Z", payload["text"])
        self.assertIn("guardian reviewer", payload["text"])
        # No connectors were selected: no connector line at all (nothing to load).
        self.assertNotIn("MCP connector", payload["text"])
        # Findings from the real import (2026-09-10) are surfaced to the agent.
        self.assertIn("recorded by path only", payload["text"])
        self.assertIn("Parallel tool calls lose their pairing", payload["text"])
        self.deleter.assert_not_called()

    def test_import_uses_a_snapshot_copy_that_is_removed(self):
        self.handoff()
        self.handoff()  # a repeat handoff of the same source gets its own thread
        self.assertEqual(len(self.staged), 2)
        self.assertNotEqual(self.staged[0], self.staged[1])
        self.assertTrue(all(not p.exists() for p in self.staged), "snapshot copies must be removed")
        self.assertTrue(self.source_path.exists())
        self.assertEqual(R.get_pair("source-codex-2").session_id, "codex-target-2")

    def test_thread_owned_by_another_pair_is_refused_without_deleting(self):
        R.add_pair(PairSpec(name="other", backend="codex", model="gpt-5.6-sol", session_id="codex-target"))
        with self.assertRaisesRegex(PairError, "already belongs to pair 'other'"):
            self.handoff()
        self.deleter.assert_not_called()
        self.assertNotIn("source-codex", R.load().pairs)

    def test_allowed_tools_requires_explicit_permission(self):
        R.update_pair("source", allowed_tools=["Read"])
        with self.assertRaisesRegex(PairError, "permission_mode explicitly"):
            self.handoff()
        self.importer.assert_not_called()
        result = self.handoff(permission_mode="read-only")
        self.assertIn("allow-list ['Read'] is NOT enforced on Codex", result)
        self.assertIsNone(R.get_pair("source-codex").allowed_tools)
        self.assertIn("allowed_tools", R.get_pair("source-codex").handoff_from["omitted"])

    def test_claude_only_config_is_omitted_and_warned(self):
        R.update_pair("source", mcp_whitelist=["files"], allowed_invocations=[], ultracode=True,
                      fallback_model="sonnet", persistent=True, permission_mode="plan")
        result = self.handoff(model="luna")
        target = R.get_pair("source-codex")
        self.assertEqual(target.effort, "high")
        self.assertTrue(target.persistent)
        self.assertEqual(target.backend_options, {})
        for field in ("mcp_whitelist", "allowed_invocations", "fallback_model"):
            self.assertIsNone(getattr(target, field))
            self.assertIn(field, target.handoff_from["omitted"])
        self.assertFalse(target.ultracode)
        self.assertIn("effort='ultra'", result)
        self.assertIn("read-only sandboxing", result)
        # Each selected connector is named; none is activated on Codex yet.
        self.assertIn("MCP connector 'files' is not available to this Codex pair and was not activated", result)
        # pair_invoke doesn't exist on Codex, so its allow-list is recorded but not warned about.
        self.assertNotIn("allowed_invocations", result)

    def test_briefing_resolves_profile_instructions_and_probe(self):
        (R.profiles_dir() / "review.md").write_text("Profile instructions", encoding="utf-8")
        R.update_pair("source", profile_name="review", system_prompt_append="Pinned instructions")
        self.handoff(briefing="Focus on Unicode", probe="Describe the unfinished work")
        message = self.sender.call_args.args[1]
        for text in ("source model: opus", "[external_agent_tool_call]", "[external_agent_tool_result]",
                     "not actions you took", "Profile instructions", "Pinned instructions",
                     "user message, not a system instruction", "Focus on Unicode", "Describe the unfinished work"):
            self.assertIn(text, message)
        self.assertNotIn(S._HANDOFF_PROBE, message)
        self.assertEqual(R.get_pair("source-codex").profile_name, "review")

    def test_busy_source_is_refused_quickly_not_waited_on(self):
        # A running turn holds the source's pair lock. The handoff must refuse
        # within the short lock timeout instead of sitting out the turn.
        import time as _time
        held, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hold():
            with S._with_pair_lock("source"):
                held.set()
                release.wait(20)

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        self.assertTrue(held.wait(5))
        started = _time.monotonic()
        with self.assertRaisesRegex(PairError, "busy"):
            self.handoff()
        self.assertLess(_time.monotonic() - started, 15)
        self.importer.assert_not_called()
        release.set()
        worker.join(5)

    def test_ownership_check_failure_reports_thread_without_deleting(self):
        # Astra review catch: a registry failure AFTER a successful import must
        # neither leak the thread silently nor delete one that might be owned.
        real_load = R.load

        def load_after_import():
            if self.imports:
                raise OSError("registry unreadable")
            return real_load()

        with patch.object(R, "load", side_effect=load_after_import):
            with self.assertRaisesRegex(PairError, "codex-target.*NOT deleted"):
                self.handoff()
        self.deleter.assert_not_called()
        self.assertNotIn("source-codex", real_load().pairs)

    def test_stored_pair_connector_does_not_block_handoff(self):
        R.update_pair("source", mcp_whitelist=["pair"])
        result = self.handoff()
        self.assertIn("source-codex", R.load().pairs)
        self.assertIsNone(R.get_pair("source-codex").mcp_whitelist)
        self.assertIn("pair", result)

    def test_running_and_queued_tasks_refuse_before_import(self):
        with patch.object(A, "list_running_task_ids_for_pair", return_value=["queued-task"]):
            with self.assertRaisesRegex(PairError, "busy"):
                self.handoff()
        self.importer.assert_not_called()

    def test_local_open_turns_refuse_even_without_task_files(self):
        for tracked in (False, True):
            runtime = SimpleNamespace(active_task_ids=lambda: {"implicit"} if tracked else set(),
                                      has_untracked_turn=lambda: not tracked)
            self.runtime.get_or_none = lambda name: runtime
            with self.subTest(tracked=tracked), self.assertRaisesRegex(PairError, "busy"):
                self.handoff()
        self.importer.assert_not_called()

    def test_names_suffix_and_explicit_collision(self):
        R.add_pair(self.source.model_copy(update={"name": "source-codex"}))
        R.add_pair(self.source.model_copy(update={"name": "source-codex-2"}))
        with self.assertRaises(PairAlreadyExists):
            self.handoff(new_name="source-codex")
        self.importer.assert_not_called()
        self.handoff()
        self.assertEqual(R.get_pair("source-codex-3").session_id, "codex-target")

    def test_registration_race_deletes_and_reports_orphan(self):
        self.registration.side_effect = PairAlreadyExists("source-codex")
        self.deleter.side_effect = CLIError("delete refused")
        with self.assertRaisesRegex(PairAlreadyExists, "orphaned Codex thread codex-target"):
            self.handoff()
        self.deleter.assert_called_once_with("codex-target")
        self.sender.assert_not_called()

    def test_size_thresholds_both_windows_and_cleanup(self):
        for tokens, refuses, warns in ((49_999, False, False), (50_000, False, True),
                                       (69_999, False, True), (70_000, True, True)):
            size = C.HandoffSize(tokens, 100_000, 400_000, "default")
            self.assertEqual((size.refused, size.warning), (refuses, warns))
            self.assertIn("default window", size.render())
            self.assertIn("context_window='1m'", size.render())
            self.assertIn("before Codex auto-compacts", size.render())
        self.assertFalse(C.HandoffSize(70_000, 100_000, 400_000, "1m").refused)
        for delete_fails in (False, True):
            self.deleter.side_effect = CLIError("busy") if delete_fails else None
            with patch.object(C, "measure_imported_history", return_value=70_000):
                with self.assertRaises(PairError) as raised:
                    self.handoff()
            report = str(raised.exception)
            for text in ("70%", "18%", "pair_compact", "fresh Codex pair", "would fit", "no new pair"):
                self.assertIn(text.lower(), report.lower())
            self.assertEqual("orphaned" in report, delete_fails)
        self.assertNotIn("source-codex", R.load().pairs)
        self.sender.assert_not_called()

    def test_half_full_warning_and_full_generated_message_is_measured(self):
        with patch.object(C, "measure_imported_history", return_value=50_000) as measure:
            report = self.handoff(briefing="BRIEF", probe="PROBE")
        self.assertIn("Size warning", report)
        self.assertIn("BRIEF", measure.call_args.args[1])
        self.assertIn("PROBE", measure.call_args.args[1])
        self.assertIn("previous agent", measure.call_args.args[1])

    def test_extended_window_and_explicit_configuration(self):
        with patch.object(C, "measure_imported_history", return_value=80_000):
            report = self.handoff(model="gpt-5.6-luna", effort="low",
                                  permission_mode="acceptEdits", context_window="1m")
        target = R.get_pair("source-codex")
        self.assertEqual((target.model, target.effort, target.permission_mode, target.context_window),
                         ("gpt-5.6-luna", "low", "workspace", "1m"))
        self.assertIn("80% of the default window", report)
        self.assertIn("20% with context_window='1m'", report)
        self.assertNotIn("Size warning", report)

    def test_extended_window_also_refuses_at_seventy_percent(self):
        with patch.object(C, "measure_imported_history", return_value=280_000):
            with self.assertRaisesRegex(PairError, "would still NOT fit"):
                self.handoff(context_window="1m")
        self.registration.assert_not_called()
        self.deleter.assert_called_once_with("codex-target")

    def test_rollout_measurement_includes_tools_and_seeded_usage(self):
        call = {"type": "function_call", "name": "exec", "arguments": "echo hi", "call_id": "a"}
        output = {"type": "function_call_output", "call_id": "a", "output": "hi"}
        extras = [{"type": "response_item", "payload": p} for p in (call, output)]
        self.write_rollout(text="x" * 400, usage={"total_token_usage": {"total_tokens": 0}}, extra=extras)
        chars = 400 + sum(len(json.dumps(p, ensure_ascii=False)) for p in (call, output))
        self.assertEqual(C.measure_imported_history("codex-target", "seed"), 13_000 + (chars + 3) // 4 + 1)
        self.write_rollout(usage={"total_token_usage": {"total_tokens": 42_000}})
        self.assertEqual(C.measure_imported_history("codex-target", "seed"), 55_001)
        self.write_rollout(usage={"last_token_usage": {"input_tokens": 80, "output_tokens": 20},
                                  "total_token_usage": {"total_tokens": 42_000}})
        self.assertEqual(C.measure_imported_history("codex-target", "seed"), 13_101)
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}}) + "\n")
        self.assertEqual(C.measure_imported_history("codex-target", "seed"), 13_101)

    def test_measurement_failure_cleans_up(self):
        with patch.object(C, "measure_imported_history", side_effect=ValueError("broken rollout")):
            with self.assertRaisesRegex(PairError, "Cannot measure"):
                self.handoff()
        self.deleter.assert_called_once_with("codex-target")
        self.registration.assert_not_called()

    def test_async_handle_keeps_probe_stoppable(self):
        with patch.object(A, "wait_for_task", side_effect=lambda tid, **kw: A.load_task(tid).model_copy(update={"status": "running"})), \
                patch.object(S, "_sync_cap_seconds", return_value=1):
            report = self.handoff(timeout_seconds=45)
        self.assertIn("Async task:", report)
        self.assertIn("Sync wait held for 1s", report)
        # Join the real offline worker before deleting its temporary state.
        task_id = A.latest_task_id_for_pair("source-codex")
        self.assertIsNotNone(A.wait_for_task(task_id, timeout_s=5).result)

    def test_failed_briefing_keeps_imported_pair(self):
        self.sender.side_effect = CLIError("offline model failure")
        report = self.handoff()
        self.assertIn("failed", report)
        self.assertIn("remains registered", report)
        self.assertIn("corrective briefing", report)
        self.assertIn("source-codex", R.load().pairs)
        self.deleter.assert_not_called()

    def test_briefing_is_cancellable_through_pair_stop(self):
        entered = threading.Event()
        release = threading.Event()
        def waiting_send(spec, message, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release the offline turn")
            if kwargs["should_stop"]():
                raise CLIError("offline turn cancelled")
            return self.send(spec, message, **kwargs)
        self.sender.side_effect = waiting_send
        self.handoff(timeout_seconds=0)
        task_id = A.latest_task_id_for_pair("source-codex")
        try:
            self.assertTrue(entered.wait(5))
            tool(S.pair_stop)("source-codex")
        finally:
            release.set()
        state = A.wait_for_task(task_id, timeout_s=5)
        self.assertEqual(state.status, "stopped")
        self.assertIn("source-codex", R.load().pairs)
        self.deleter.assert_not_called()

    def test_reverse_direction_and_unknown_window_fail_before_import(self):
        with patch.object(CM, "context_windows", return_value=None):
            with self.assertRaisesRegex(PairError, "usable context windows"):
                self.handoff()
        R.update_pair("source", backend="codex", model="sol")
        with self.assertRaisesRegex(PairError, "not supported yet"):
            self.handoff()
        self.importer.assert_not_called()

    def test_cross_backend_overrides_refused_before_task_creation(self):
        with patch.object(A, "start_task") as start:
            with self.assertRaisesRegex(PairError, "pair_handoff") as sync_error:
                asyncio.run(tool(S.pair_send)("source", "hello", override_model="sol"))
            with self.assertRaisesRegex(PairError, "pair_handoff") as async_error:
                tool(S.pair_send_async)("source", "hello", override_model="sol")
            with self.assertRaisesRegex(PairError, "pair_handoff") as update_error:
                tool(S.pair_update)("source", model="sol")
            self.assertEqual(str(sync_error.exception), str(async_error.exception))
            self.assertEqual(str(sync_error.exception), str(update_error.exception))
            R.update_pair("source", backend="codex", model="sol")
            with self.assertRaisesRegex(PairError, "backend is fixed"):
                tool(S.pair_send_async)("source", "hello", override_model="opus")
            start.assert_not_called()

    def test_null_effort_persists_and_target_falls_back_to_policy_default(self):
        R.update_pair("source", model="haiku", effort=None)
        self.assertIsNone(json.loads(R.registry_path().read_text())["pairs"]["source"]["effort"])
        self.assertIsNone(R.get_pair("source").effort)
        self.handoff()
        # A source with no effort level must not leave the Codex pair on the
        # CLI's cache default (Sol: low); it gets the policy default instead.
        self.assertEqual(R.get_pair("source-codex").effort, "high")

    def test_import_stdio_handshake_notification_correlation_and_payload(self):
        completion = {"method": "externalAgentConfig/import/completed", "params": {
            "importId": "expected", "itemTypeResults": [{"itemType": "SESSIONS", "failures": [],
                "successes": [{"target": "native-thread"}]}]}}
        proc = FakeProcess([
            {"id": 1, "result": {}},
            {"method": "externalAgentConfig/import/completed", "params": {"importId": "other"}},
            completion,  # completion may precede the RPC response
            {"id": 2, "result": {"importId": "expected"}},
        ])
        # Bypass only this case's importer mock; exercise the real shared client.
        with patch.object(subprocess, "Popen", return_value=proc):
            imported = self.real_import(self.source_path, cwd=str(self.directory), title="target")
        sent = [json.loads(line) for line in proc.stdin.recorded.splitlines()]
        self.assertEqual([m["method"] for m in sent], ["initialize", "initialized", "externalAgentConfig/import"])
        items = sent[-1]["params"]["migrationItems"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["itemType"], "SESSIONS")
        self.assertEqual(items[0]["details"]["sessions"][0]["path"], str(self.source_path))
        self.assertEqual(imported["thread_id"], "native-thread")
        self.assertEqual(imported["import_id"], "expected")

    # Keep references before setUp patches the public helpers.
    real_import = staticmethod(C.import_claude_session)
    real_delete = staticmethod(C.delete_thread)

    def test_import_rpc_errors_and_session_failures_raise(self):
        for reply in ({"id": 2, "error": {"message": "unsupported"}},
                      {"method": "externalAgentConfig/import/completed", "params": {
                          "importId": "expected", "itemTypeResults": [{"itemType": "SESSIONS",
                              "successes": [], "failures": [{"error": "bad source"}]}]}}):
            proc = FakeProcess([{"id": 1, "result": {}}, reply,
                                {"id": 2, "result": {"importId": "expected"}}])
            with patch.object(subprocess, "Popen", return_value=proc), patch.object(C, "_tree_kill"), \
                    self.assertRaises(CLIError):
                self.real_import(self.source_path, cwd=str(self.directory), title="target")

    def test_compact_uses_same_client_and_preserves_request_configuration(self):
        spec = PairSpec(name="compact", backend="codex", session_id="thread", model="sol", effort="high",
                        cwd=str(self.directory), backend_options={"config": {"foo": 1}})
        proc = FakeProcess([{"id": i, "result": {}} for i in (1, 2, 3)] + [
            {"method": "item/completed", "params": {"threadId": "thread", "item": {"type": "contextCompaction"}}},
            {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"status": "completed"}}},
        ])
        with patch.object(subprocess, "Popen", return_value=proc):
            self.assertIsNone(C.CodexAdapter()._appserver_compact(spec, timeout_seconds=30))
        sent = [json.loads(line) for line in proc.stdin.recorded.splitlines()]
        self.assertEqual([m["method"] for m in sent], ["initialize", "initialized", "thread/resume", "thread/compact/start"])
        self.assertEqual(sent[2]["params"]["config"], {"mcp_servers": {}, "model_reasoning_effort": "high", "foo": 1})
        self.assertIsNone(C.inflight_info("compact"))

    def test_compact_cancellation_cleans_up_client_identity(self):
        spec = PairSpec(name="compact", backend="codex", session_id="thread", model="sol",
                        cwd=str(self.directory))
        proc = FakeProcess([{"id": 1, "result": {}}])
        with patch.object(subprocess, "Popen", return_value=proc), patch.object(C, "_tree_kill") as kill:
            with self.assertRaisesRegex(CLIError, "compaction stopped by pair_stop"):
                C.CodexAdapter()._appserver_compact(spec, timeout_seconds=30, should_stop=lambda: True)
            kill.assert_called_with(proc)
        self.assertIsNone(C.inflight_info("compact"))

    def test_delete_uses_force_and_reports_cli_failure(self):
        with patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="busy", stdout="")) as run:
            with self.assertRaisesRegex(CLIError, "busy"):
                self.real_delete("native-thread")
        self.assertEqual(run.call_args.args[0], ["fixture-codex", "delete", "--force", "native-thread"])


if __name__ == "__main__":
    unittest.main()
