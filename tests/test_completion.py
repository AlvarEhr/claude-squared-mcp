"""Completion delivery regressions. Isolated homes; no live model calls."""
from contextlib import contextmanager, ExitStack
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
HOME = tempfile.TemporaryDirectory(prefix="cs-completion-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
from claude_squared import completion as N, desktop_bridge as B, registry as R, async_tasks as A, activity
from claude_squared import server as S
from claude_squared.models import AsyncTaskState, PairSpec, SendResult


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=HOME.name))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "CLAUDE_HOME": str(self.root / "claude"), "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_PAIR_CODEX_COMPLETION": "desktop"}))
        self.stack.enter_context(patch.object(N, "start"))
        self.stack.enter_context(patch.object(B, "desktop_ancestor", return_value={"build": "fixture"}))
        N._stop.clear()
        self.thread, self.turn, self.session = [str(uuid.uuid4()) for _ in range(3)]
        self.ctx = SimpleNamespace(request_context=SimpleNamespace(meta={
            "x-codex-turn-metadata": {"thread_id": self.thread, "turn_id": self.turn}}))
        self.rollout = self.root / "codex/sessions/2026/10/06" / ("rollout-fixture-" + self.thread + ".jsonl")
        self.rollout.parent.mkdir(parents=True)
        self.events = [
            {"type": "session_meta", "payload": {"id": self.thread, "originator": "Codex Desktop",
                                                    "source": "vscode", "thread_source": "user"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": self.turn}},
            {"type": "turn_context", "payload": {"turn_id": self.turn, "model": "fixture",
                "approval_policy": "never", "sandbox_policy": {"type": "danger-full-access"},
                "cwd": str(self.root)}}]
        self.write_rollout()
        R.add_pair(PairSpec(name="child", session_id=self.session, cwd=str(self.root)))
        self.view = {"verdict": "idle", "losses": [], "window_turns": []}
        self.stack.enter_context(patch.object(activity, "view", side_effect=lambda *a, **k: self.view.copy()))
        self.sent = []
        self.send_error = None
        case = self
        class Connection:
            def request(self, method, params, **options):
                case.sent.append((method, params, options))
                if case.send_error:
                    raise case.send_error
                return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps({"threadId": params["threadId"]})}]}
        @contextmanager
        def endpoint():
            yield Connection()
        self.endpoint = self.stack.enter_context(patch.object(B, "endpoint", side_effect=endpoint))

    def write_rollout(self):
        self.rollout.write_text("\n".join(json.dumps(e) for e in self.events) + "\n", encoding="utf-8")

    def task(self, status="done"):
        task = AsyncTaskState(task_id=str(uuid.uuid4()), pair_name="child", message="fixture",
                              status=status, started_at=datetime.utcnow(),
                              finished_at=datetime.utcnow() if status != "running" else None,
                              owner_pid=os.getpid())
        A._save(task)
        return task

    def arm(self, task=None):
        task = task or self.task()
        self.assertIn("notification armed", N.arm(task, self.ctx))
        path = next(p for p in N.directory().glob("*.json") if task.task_id in N._read(p)["tasks"])
        return path

    def test_disabled_has_no_effects_and_codex_hint_is_honest(self):
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_COMPLETION": ""}):
            hint = N.arm(self.task(), self.ctx)
            N.start(); N.dispatch_once()
        self.assertIn("disabled", hint)
        self.assertFalse(N.directory().exists())
        B.desktop_ancestor.assert_not_called()
        self.endpoint.assert_not_called()

    def test_non_codex_disabled_hint_remains_unchanged(self):
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_COMPLETION": ""}):
            self.assertIsNone(N.arm(self.task(), None))

    def test_metadata_all_aliases_and_serialized_nested_values(self):
        for keys in [("threadId", "turnId"), ("openai/thread_id", "codexTurnId")]:
            ctx = SimpleNamespace(request_context=SimpleNamespace(meta=dict(zip(keys, [self.thread, self.turn]))))
            self.assertEqual(N.caller_ids(ctx), (self.thread, self.turn))
        self.ctx.request_context.meta["x-codex-turn-metadata"] = json.dumps({"thread_id": self.thread, "turn_id": self.turn})
        self.assertEqual(N.caller_ids(self.ctx), (self.thread, self.turn))

    def test_metadata_conflicts_bad_ids_remote_and_absent_rejected(self):
        for extra in ({"threadId": str(uuid.uuid4())}, {"turnId": "../other"}, {"hostId": "ssh:other"}):
            with self.subTest(extra=extra):
                meta = dict(self.ctx.request_context.meta, **extra)
                with self.assertRaises(N.Refused):
                    N.caller_ids(SimpleNamespace(request_context=SimpleNamespace(meta=meta)))
        with self.assertRaises(N.Refused):
            N.caller_ids(None)

    def test_restricted_profiles_and_approval_modes_refused(self):
        for updates in ({"sandbox_policy": {"type": "read-only"}},
                        {"sandbox_policy": {"type": "workspace-write"}},
                        {"approval_policy": "on-request"}, {"permissions": "named-profile"},
                        {"collaboration_mode": {"mode": "plan"}}):
            with self.subTest(updates=updates):
                original = self.events[-1]["payload"].copy()
                self.events[-1]["payload"].update(updates); self.write_rollout()
                self.assertIn("not armed", N.arm(self.task(), self.ctx))
                self.events[-1]["payload"] = original
        self.assertFalse(N.directory().exists())

    def test_cli_pair_and_rewound_parent_refused(self):
        for updates in ({"originator": "codex_exec", "source": "exec"}, {"thread_source": "claude-squared"}):
            original = self.events[0]["payload"].copy()
            self.events[0]["payload"].update(updates); self.write_rollout()
            self.assertIn("not armed", N.arm(self.task(), self.ctx))
            self.events[0]["payload"] = original
        self.events.pop(1); self.write_rollout()
        self.assertIn("not armed", N.arm(self.task(), self.ctx))

    def test_own_pair_is_not_a_manager(self):
        R.update_pair("child", session_id=self.thread)
        self.assertIn("own thread", N.arm(self.task(), self.ctx))

    def test_desktop_builtin_full_access_profile_is_supported(self):
        self.events[0]["payload"]["thread_source"] = "agent_created_thread"
        self.events[-1]["payload"].update(permission_profile={"type": "disabled"},
                                       active_permission_profile={"id": ":danger-full-access"})
        self.write_rollout(); path = self.arm(); N.process_one(path)
        self.assertEqual(N._read(path)["status"], "delivered")

    def test_fast_completion_and_exact_destination_no_overrides(self):
        path = self.arm()
        N.process_one(path); N.process_one(path)
        self.assertEqual(len(self.sent), 1)
        _, params, options = self.sent[0]
        self.assertEqual(params["threadId"], self.thread)
        self.assertEqual(params["arguments"]["threadId"], self.thread)
        self.assertEqual(params["turnId"], self.turn)
        self.assertEqual(set(params["arguments"]), {"threadId", "prompt"})
        self.assertTrue(options["delivery"])
        self.assertEqual(N._read(path)["status"], "delivered")

    def test_multiple_sends_coalesce_but_different_managers_do_not(self):
        first, second = self.task(), self.task()
        path = self.arm(first); self.arm(second)
        self.assertEqual(len(N.records()), 1)
        self.assertEqual(set(N._read(path)["tasks"]), {first.task_id, second.task_id})
        other = str(uuid.uuid4())
        with patch.object(N, "caller_ids", return_value=(other, self.turn)), patch.object(N, "caller_snapshot", return_value=N.caller_snapshot(self.thread, self.turn)):
            self.assertIn("armed", N.arm(self.task(), self.ctx))
        self.assertEqual(len(N.records()), 2)

    def test_background_settling_and_running_prevent_premature_notice(self):
        task = self.task("running"); path = self.arm(task)
        N.process_one(path)
        task.status = "done"; A._save(task)
        for verdict in ("busy", "settling"):
            self.view["verdict"] = verdict
            N.process_one(path)
        self.assertFalse(self.sent)
        self.view["verdict"] = "idle"; N.process_one(path)
        self.assertEqual(len(self.sent), 1)

    def test_busy_tasks_do_not_rescan_entire_manager_rollout(self):
        path = self.arm(self.task("running"))
        with patch.object(N, "caller_snapshot") as inspect:
            N.process_one(path)
        inspect.assert_not_called()

    def test_policy_change_before_delivery_blocks_without_transmission(self):
        path = self.arm()
        self.events[-1]["payload"]["sandbox_policy"] = {"type": "read-only"}
        self.write_rollout(); N.process_one(path)
        self.assertEqual(N._read(path)["status"], "blocked")
        self.assertFalse(self.sent)

    def test_workspace_change_since_registration_blocks(self):
        path = self.arm()
        self.events[-1]["payload"]["cwd"] = str(self.root / "another-workspace")
        self.write_rollout(); N.process_one(path)
        self.assertEqual(N._read(path)["status"], "blocked")
        self.assertFalse(self.sent)

    def test_generation_and_loss_baseline_captured_before_launch(self):
        captured = N.prepare(self.ctx, R.get_pair("child"))
        task = self.task()
        self.view["losses"] = [{"task_id": task.task_id, "reason": "lost during fast worker"}]
        N.arm(task, self.ctx, captured)
        path = next(N.directory().glob("*.json")); N.process_one(path)
        self.assertIn("background work was lost", self.sent[0][1]["arguments"]["prompt"])
        captured = N.prepare(self.ctx, R.get_pair("child"))
        R.update_pair("child", session_id=str(uuid.uuid4()))
        next_task = self.task(); N.arm(next_task, self.ctx, captured)
        path = next(p for p in N.directory().glob("*.json") if next_task.task_id in N._read(p)["tasks"])
        N.process_one(path)
        self.assertEqual(N._read(path)["status"], "canceled")

    def test_pair_changes_during_discovery_are_revalidated(self):
        for operation in (lambda: R.update_pair("child", session_id=str(uuid.uuid4())),
                          lambda: self.view.update(verdict="busy"),
                          lambda: R.remove_pair("child")):
            with self.subTest(operation=operation):
                path = self.arm()
                @contextmanager
                def change():
                    operation()
                    yield SimpleNamespace(request=lambda *a, **k: self.sent.append(a))
                self.endpoint.side_effect = change
                N.process_one(path)
                self.assertFalse(self.sent)
                self.view["verdict"] = "idle"
                if "child" not in R.load().pairs:
                    break

    def test_logical_rollback_removes_source_turn(self):
        path = self.arm()
        self.events.append({"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}})
        self.write_rollout(); N.process_one(path)
        self.assertEqual(N._read(path)["status"], "blocked")
        self.assertFalse(self.sent)

    def test_callback_only_turn_does_not_hide_user_boundary_rollback(self):
        path = self.arm()
        callback_turn = str(uuid.uuid4())
        self.events.extend([
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": callback_turn}},
            {"type": "turn_context", "payload": dict(self.events[-1]["payload"], turn_id=callback_turn)},
            {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}}])
        self.write_rollout(); N.process_one(path)
        self.assertEqual(N._read(path)["status"], "blocked")
        self.assertFalse(self.sent)

    def test_missing_latest_context_defers_instead_of_inheriting_old_permissions(self):
        path = self.arm(); next_turn = str(uuid.uuid4())
        self.events.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": next_turn}})
        self.write_rollout(); N.process_one(path)
        self.assertEqual(N._read(path)["status"], "watching")
        self.assertFalse(self.sent)
        self.events.append({"type": "turn_context", "payload": dict(self.events[-2]["payload"],
                           turn_id=next_turn, sandbox_policy={"type": "read-only"})})
        self.write_rollout(); r=N._read(path); r["retry_at"]=0; N._save(path,r); N.process_one(path)
        self.assertEqual(N._read(path)["status"], "blocked")

    def test_new_source_after_rollback_gets_a_fresh_registration(self):
        old_task = self.task("running"); old = self.arm(old_task)
        new_turn = str(uuid.uuid4())
        context = self.events[-1]["payload"].copy()
        self.events.extend([
            {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": new_turn}},
            {"type": "turn_context", "payload": dict(context, turn_id=new_turn)}])
        self.write_rollout()
        self.ctx.request_context.meta["x-codex-turn-metadata"]["turn_id"] = new_turn
        task = self.task(); new = self.arm(task)
        self.assertNotEqual(new, old)
        self.assertNotIn(task.task_id, N._read(old)["tasks"])
        N.process_one(new)
        self.assertEqual(N._read(new)["status"], "delivered")

    def test_pair_clear_and_forget_invalidate_old_notices(self):
        for operation in (lambda: R.update_pair("child", session_id=str(uuid.uuid4())), lambda: R.remove_pair("child")):
            path = self.arm(); operation(); N.process_one(path)
            self.assertEqual(N._read(path)["status"], "canceled")
            if "child" not in R.load().pairs:
                break
            self.session = R.get_pair("child").session_id
        self.assertFalse(self.sent)

    def test_parent_archive_blocks_without_reopening(self):
        path = self.arm()
        self.rollout.unlink(); N.process_one(path)
        self.assertEqual(N._read(path)["status"], "blocked")
        self.assertFalse(self.sent)

    def test_unknown_activity_is_not_reported_as_idle(self):
        path = self.arm(); self.view.update(verdict="unknown", reasons=["publication lag"])
        N.process_one(path); self.assertFalse(self.sent)
        r = N._read(path); r["unknown_since"] -= 121; N._save(path, r)
        N.process_one(path)
        self.assertIn("activity is unresolved", self.sent[0][1]["arguments"]["prompt"])
        self.assertNotIn(" is idle.", self.sent[0][1]["arguments"]["prompt"])

    def test_old_losses_ignored_new_losses_reported(self):
        self.view["losses"] = [{"task_id": "old", "reason": "past"}]
        path = self.arm()
        self.view["losses"].append({"task_id": "new", "reason": "lost"})
        N.process_one(path)
        self.assertIn("background work was lost", self.sent[0][1]["arguments"]["prompt"])

    def test_discovery_failure_retries_without_marking_sent(self):
        path = self.arm()
        self.endpoint.side_effect = B.Unavailable("bridge gone")
        N.process_one(path)
        r = N._read(path)
        self.assertEqual(r["status"], "watching")
        self.assertGreater(r["retry_at"], time.time())
        self.assertFalse(self.sent)

    def test_uncertain_delivery_is_never_blindly_resent(self):
        path = self.arm(); self.send_error = B.Uncertain("ack lost")
        N.process_one(path); N.process_one(path)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(N._read(path)["status"], "uncertain")
        r = N._read(path)
        self.events.append({"type": "response_item", "payload": {
            "type": "function_call_output", "namespace": "codex_app", "name": "send_message_to_thread",
            "output": "CLAUDE_PAIR_COMPLETION:" + r["id"]}})
        self.write_rollout(); N.process_one(path)
        r = N._read(path); r["retry_at"] = 0; N._save(path, r)
        N.process_one(path)
        self.assertEqual(N._read(path)["status"], "delivered")
        self.assertEqual(len(self.sent), 1)

    def test_sending_intent_survives_dispatcher_death(self):
        path = self.arm(); r = N._read(path); r["status"] = "sending"; N._save(path, r)
        N.process_one(path)
        self.assertEqual(N._read(path)["status"], "uncertain")
        self.assertFalse(self.sent)

    def test_uncertain_receipt_check_has_backoff(self):
        path = self.arm(); r = N._read(path); r["status"] = "sending"; N._save(path, r)
        N.process_one(path)
        with patch.object(N, "caller_snapshot") as inspect:
            N.process_one(path)
        inspect.assert_not_called()

    def test_duplicate_dispatchers_only_one_sends(self):
        path = self.arm()
        gate = threading.Event()
        @contextmanager
        def slow_endpoint():
            gate.set(); time.sleep(.1)
            yield SimpleNamespace(request=lambda *a, **k: self.sent.append(a) or {"success": True,
                "contentItems": [{"type": "inputText", "text": json.dumps({"threadId": self.thread})}]})
        self.endpoint.side_effect = slow_endpoint
        first = threading.Thread(target=N.process_one, args=(path,))
        first.start(); self.assertTrue(gate.wait(2)); N.process_one(path); first.join(2)
        self.assertEqual(len(self.sent), 1)

    def test_persistence_failure_keeps_pair_result_available(self):
        task = self.task()
        with patch.object(N, "_save", side_effect=OSError("disk full")):
            self.assertIn("not armed", N.arm(task, self.ctx))
        self.assertEqual(A.load_task(task.task_id).status, "done")
        self.assertFalse(self.sent)

    def test_unknown_outbox_version_preserved(self):
        path = self.arm(); data = json.loads(path.read_text()); data["version"] = 99
        original = json.dumps(data); path.write_text(original)
        N.process_one(path)
        self.assertEqual(path.read_text(), original)
        self.assertFalse(self.sent)

    def test_atomic_record_write_retries_transient_windows_reader_lock(self):
        path = self.arm(); record = N._read(path)
        original = Path.replace
        failures = []
        def replacement(tmp, target):
            if not failures:
                failures.append(True)
                raise PermissionError("temporary reader lock")
            return original(tmp, target)
        with patch.object(Path, "replace", replacement):
            N._save(path, record)
        self.assertEqual(N._read(path)["id"], record["id"])

    def test_completed_work_after_long_outage_is_reported_as_idle(self):
        path = self.arm(); r=N._read(path); r["created"] -= N.MAX_WAIT + 1; N._save(path,r)
        N.process_one(path)
        self.assertIn(" is idle.", self.sent[0][1]["arguments"]["prompt"])
        self.assertNotIn("wait expired", self.sent[0][1]["arguments"]["prompt"])

    def test_cli_notifications_is_read_only(self):
        path = self.arm(); before = path.read_bytes()
        result = subprocess.run([sys.executable, "-m", "claude_squared", "notifications"],
                                env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["id"], path.stem)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.sent)

    def test_codex_handle_does_not_claim_claude_bash_auto_notification(self):
        text = S._format_async_handle(str(uuid.uuid4()), "running", "child", "Codex completion disabled")
        self.assertNotIn("Bash(run_in_background", text)
        self.assertIn("pair_poll", text)

    def test_inline_background_placeholder_arms_but_final_reply_does_not(self):
        result = SendResult(name="child", session_id=self.session, response="INTERIM",
                            model_used="fixture", duration_ms=1, background_launches=["Agent"])
        self.view["background"] = [{"started_at": (datetime.utcnow() + timedelta(seconds=1)).isoformat()}]
        runner = lambda task_id: result
        with patch.object(S, "_build_send_runner", return_value=runner):
            text = asyncio.run(getattr(S.pair_send, "fn", S.pair_send)("child", "work", ctx=self.ctx))
        self.assertIn("notification armed", text)
        self.assertEqual(len(N.records()), 1)

        # Launch history survives a completion during the same turn. It must
        # not make that final inline reply arm a duplicate notification.
        result.background_launches = ["Agent"]
        self.view["background"] = []
        with patch.object(S, "_build_send_runner", return_value=runner):
            text = asyncio.run(getattr(S.pair_send, "fn", S.pair_send)("child", "work", ctx=self.ctx))
        self.assertNotIn("notification armed", text)
        self.assertEqual(len(N.records()), 1)

    def test_fast_completed_self_woken_final_still_notifies_interim_caller(self):
        result = SendResult(name="child", session_id=self.session, response="INTERIM",
                            model_used="fixture", duration_ms=1, background_launches=["Agent"])
        self.view["window_turns"] = [{"kind": "self_woken", "task_id": str(uuid.uuid4()), "status": "done",
                                     "started_at": (datetime.utcnow() + timedelta(seconds=1)).isoformat()}]
        runner = lambda task_id: result
        with patch.object(S, "_build_send_runner", return_value=runner):
            text = asyncio.run(getattr(S.pair_send, "fn", S.pair_send)("child", "work", ctx=self.ctx))
        self.assertIn("notification armed", text)
        self.assertEqual(len(N.records()), 1)
        result.background_launches = []
        with patch.object(S, "_build_send_runner", return_value=runner):
            text = asyncio.run(getattr(S.pair_send, "fn", S.pair_send)("child", "work", ctx=self.ctx))
        self.assertNotIn("notification armed", text)
        self.assertEqual(len(N.records()), 1)

    def test_disabled_legacy_inline_reply_does_not_read_notification_activity(self):
        result = SendResult(name="child", session_id=self.session, response="FINAL",
                            model_used="fixture", duration_ms=1, background_launches=["Agent"])
        with patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_COMPLETION": ""}), patch.object(S, "_build_send_runner", return_value=lambda _: result), patch.object(activity, "view") as inspect:
            text = asyncio.run(getattr(S.pair_send, "fn", S.pair_send)("child", "work"))
        self.assertNotIn("notification armed", text)
        inspect.assert_not_called()


class BridgeTests(unittest.TestCase):
    def test_destination_ack_required(self):
        thread = str(uuid.uuid4())
        B.validate_delivery({"success": True, "contentItems": [{"type": "inputText", "text": json.dumps({"threadId": thread})}]}, thread)
        for reply in ({"success": True, "contentItems": []},
                      {"success": True, "contentItems": [{"type": "inputText", "text": '{"threadId":"other"}'}]}):
            with self.assertRaises(B.Uncertain): B.validate_delivery(reply, thread)

    def test_real_stdio_context_injection_and_dispatch_after_rpc_returns(self):
        with tempfile.TemporaryDirectory(prefix="cs-notify-stdio-", ignore_cleanup_errors=True) as state:
            result = subprocess.run([sys.executable, str(ROOT / "experiments/completion_smoke.py"),
                                     "--state-dir", state], capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads((Path(state) / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["context_injection_hidden"])
            self.assertEqual(summary["registration"]["status"], "delivered")

    def test_pending_notification_survives_owned_stdio_server_restart(self):
        with tempfile.TemporaryDirectory(prefix="cs-notify-restart-", ignore_cleanup_errors=True) as state:
            result = subprocess.run([sys.executable, str(ROOT / "experiments/completion_smoke.py"),
                                     "--restart", "--state-dir", state], capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads((Path(state) / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["restarted"])
            self.assertEqual(summary["registration"]["status"], "delivered")

    def test_framing_and_response_validation(self):
        data = B.encode_frame({"id": 1})
        self.assertEqual(int.from_bytes(data[:4], "little"), len(data) - 4)
        self.assertEqual(B.decode_response({"jsonrpc": "2.0", "id": 1, "result": {}}), {})
        for reply in ({}, {"jsonrpc": "2.0", "id": 2, "result": {}},
                      {"jsonrpc": "2.0", "id": 1, "error": {}}):
            with self.assertRaises(ValueError): B.decode_response(reply)
        with self.assertRaises(B.Unavailable): B.encode_frame({"big": "x" * B.MAX_FRAME})

    def test_catalog_absence_ambiguity_and_changed_schema_rejected(self):
        item = {"name": "send_message_to_thread", "namespace": "codex_app", "inputSchema": {
            "required": ["threadId", "prompt"], "properties": {
                "threadId": {"type": "string"}, "prompt": {"type": "string"}}}}
        B.validate_catalog({"tools": [item]})
        for tools in ([], [item, item], [dict(item, inputSchema={})], [dict(item, inputSchema=None)],
                      [dict(item, inputSchema={"required": None})],
                      [dict(item, inputSchema={"required": ["threadId", "prompt"], "properties": None})]):
            with self.assertRaises(B.Unavailable): B.validate_catalog({"tools": tools})

    def test_invalid_candidate_is_closed_and_ambiguous_endpoints_refused(self):
        pipe = "\\\\.\\pipe\\codex-browser-use-" + str(uuid.uuid4())
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"CLAUDE_PAIR_CODEX_PIPE": pipe}))
            stack.enter_context(patch.object(B, "desktop_ancestor", return_value={}))
            connection = SimpleNamespace(request=lambda *a, **k: {"tools": [{"name": "send_message_to_thread",
                "namespace": "codex_app", "inputSchema": None}]}, close=unittest.mock.Mock())
            stack.enter_context(patch.object(B, "Connection", return_value=connection))
            with self.assertRaises(B.Unavailable):
                with B.endpoint(): pass
            connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
