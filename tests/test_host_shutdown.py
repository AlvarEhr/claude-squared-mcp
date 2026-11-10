"""Offline Claude host-handoff regressions. Only fake child processes, never models."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
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
HOME = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="host-shutdown-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
sys.path.insert(0, str(ROOT / "src"))
from claude_squared import activity as V, async_tasks as A, claude_handoff as H
from claude_squared import registry as R, runtime as RT, server as S
from claude_squared import _auto_watch as AW, _wait_script as W
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.models import AsyncTaskState, PairSpec, SendResult
from claude_squared.errors import CLIError, PairError


def tool(fn):
    return getattr(fn, "fn", fn)


class HostShutdownTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=HOME.name, ignore_cleanup_errors=True)))
        self.stack.enter_context(patch.dict(os.environ, {"CLAUDE_HOME": str(self.home), "CODEX_HOME": str(self.home / "codex")}))
        self.stack.enter_context(patch.object(RT, "_SHUTTING_DOWN", threading.Event()))
        self.stack.enter_context(patch.object(RT, "_FAST_CLAUDE_SHUTDOWN", threading.Event()))
        self.spec = PairSpec(name="survivor", session_id=str(uuid.uuid4()), cwd=str(self.home), model="haiku")
        R.add_pair(self.spec)
        self.tid = str(uuid.uuid4())
        self.started = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.record = {"version": 1, "pair": self.spec.name, "runtime_pid": 987654321,
                       "runtime_started_at": 100.0, "server_pid": 987654320, "server_started_at": 90.0,
                       "session_id": self.spec.session_id, "cwd": self.spec.cwd, "model": self.spec.model,
                       "task_id": self.tid, "turn_started_at": self.started.isoformat()}
        self.state = AsyncTaskState(task_id=self.tid, pair_name=self.spec.name, message="sleep then canary",
                                   status="running", owner_pid=self.record["server_pid"], started_at=self.started,
                                   claude_handoff_path=str(H.path_for(self.spec.name)))
        A._save(self.state)
        self.alive = True
        self.born = 100.0
        original = V._av_proc
        def proc(pid):
            if pid == self.record["runtime_pid"]:
                return self.alive, self.born if self.alive else None
            if pid == self.record["server_pid"]:
                return False, None
            return original(pid)
        self.stack.enter_context(patch.object(V, "_av_proc", side_effect=proc))
        owner_alive = A.owner_alive
        self.stack.enter_context(patch.object(A, "owner_alive", side_effect=lambda s:
            False if s.owner_pid == self.record["server_pid"] else owner_alive(s)))
        H.publish(self.record)

    def transcript(self, final=True):
        path = ClaudeAdapter().transcript_path(self.spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "assistant", "timestamp": (self.started - timedelta(seconds=10)).isoformat(),
             "message": {"content": [{"type": "text", "text": "OLD CANARY"}], "stop_reason": "end_turn"}},
            {"type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
             "message": {"content": "Prompt contains CANARY; never recover this as the reply"}},
            {"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
             "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 20"}}],
                         "stop_reason": "tool_use"}},
        ]
        if final:
            rows.append({"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                         "message": {"model": "claude-haiku-5-5", "stop_reason": "end_turn",
                                     "content": [{"type": "text", "text": "CANARY done"}]}})
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_live_receipt_keeps_orphan_and_watch_views_running(self):
        self.assertEqual(A.reap_orphan(self.tid).status, "running")
        A._sweep_dead_predecessor_orphans()
        self.assertEqual(A.load_task(self.tid).status, "running")
        view = V.pair_activity_view(R.pairs_dir(), self.spec.name)
        self.assertEqual(view["verdict"], "busy")
        self.assertEqual(view["running_tasks"][0]["state"], "finishing_after_restart")
        self.assertIn("finishing after a host restart", tool(S.pair_status)(self.spec.name))
        path = W.install_wait_script(R.pairs_dir() / "wait.py")
        reader = runpy.run_path(str(path))
        with patch.dict(reader["_dead_owner_exit"].__globals__, {"_av_proc": V._av_proc}):
            self.assertIsNone(reader["_dead_owner_exit"](self.state.model_dump(mode="json"), V._av_now()))
        notice = AW._notice(R.pairs_dir(), self.spec.name, 3, "", {
            **reader, "pair_activity_view": lambda *a: view})
        self.assertIn("finishing after a host restart", notice)
        self.assertNotIn("orphaned", notice)

    def test_recovery_done_and_receipt_removed_once(self):
        self.transcript()
        self.alive = False
        state = A.reap_orphan(self.tid)
        self.assertEqual(state.status, "done")
        self.assertEqual(state.result.response, "CANARY done")
        self.assertEqual(state.result.model_used, "claude-haiku-5-5")
        self.assertFalse(H.path_for(self.spec.name).exists())
        self.assertEqual(A.reap_orphan(self.tid).result.response, "CANARY done")

    def test_prepared_receipt_and_owner_note_retire_on_normal_completion(self):
        H.path_for(self.spec.name).unlink()
        self.state.owner_pid = os.getpid()
        A._save(self.state)
        rt = RT.PairRuntime(self.spec, ClaudeAdapter())
        rt.proc = SimpleNamespace(pid=os.getpid(), stdin=io.BytesIO(), poll=lambda: None)
        rt._process_started_at = RT.process_identity.started_at(os.getpid())
        rt._turn_opened(self.tid, "send")
        record = json.loads(H.path_for(self.spec.name).read_text())
        for key in ("runtime_pid", "runtime_started_at", "session_id", "cwd", "task_id", "turn_started_at", "server_pid"):
            self.assertIsNotNone(record[key])
        state = A.load_task(self.tid)
        self.assertIn(str(H.path_for(self.spec.name)), state.owner_note)
        self.assertEqual(state.status, "running")
        state.status = "done"
        A._finish_task(state, threading.Event())
        self.assertFalse(H.path_for(self.spec.name).exists())

    def test_placeholder_then_shutdown_protects_background_and_pending_wake(self):
        for pending in ("background", "wake"):
            with self.subTest(pending=pending):
                RT._SHUTTING_DOWN.clear()
                RT._FAST_CLAUDE_SHUTDOWN.clear()
                self.alive = True
                rt = RT.PairRuntime(self.spec, ClaudeAdapter())
                rt.proc = SimpleNamespace(stdin=io.BytesIO(), pid=self.record["runtime_pid"], poll=lambda: None)
                rt._process_started_at = self.record["runtime_started_at"]
                rt._server_started_at = self.record["server_started_at"]
                rt._handoff_record = dict(self.record)
                H.publish(self.record)
                if pending == "background":
                    rt._bg_tasks["bg"] = {"kind": "bash", "started_at": self.started.isoformat()}
                else:
                    rt._wake_at = time.monotonic()
                self.state.status = "done"
                self.state.result = SendResult(name=self.spec.name, session_id=self.spec.session_id,
                    response="Checks running in background", model_used="claude-haiku-5-5", duration_ms=1)
                reg = RT.RuntimeRegistry()
                reg._runtimes[self.spec.name] = rt
                with patch.object(RT, "_REGISTRY", reg):
                    A._finish_task(self.state, threading.Event())
                    self.assertTrue(H.path_for(self.spec.name).exists(), "completion must retain background protection")
                    self.assertIsNone(rt._current_scope)
                    self.assertIsNone(rt._implicit_scope)
                    RT._shutdown_runtimes()
                self.assertTrue(rt.proc.stdin.closed)
                receipt = H.view(self.spec.name)
                self.assertTrue(receipt["owner_exiting"])
                self.assertTrue(receipt["alive"])
                self.assertEqual(V.pair_activity_view(R.pairs_dir(), self.spec.name)["verdict"], "busy")
                with patch.object(RT, "_SHUTTING_DOWN", threading.Event()), \
                        patch.object(RT.subprocess, "Popen", side_effect=AssertionError("duplicate spawn")):
                    with self.assertRaisesRegex(PairError, "still finishing"):
                        RT.RuntimeRegistry().get_or_start(self.spec, ClaudeAdapter())
                self.alive = False
                H.guard(self.spec.name)
                self.assertFalse(H.path_for(self.spec.name).exists())
                self.assertEqual(A.load_task(self.tid).result.response, "Checks running in background")

    def test_shutdown_rebuilds_missing_receipt_for_background_without_scope(self):
        H.path_for(self.spec.name).unlink()
        self.state.status = "done"
        A._save(self.state)
        rt = RT.PairRuntime(self.spec, ClaudeAdapter())
        rt.proc = SimpleNamespace(stdin=io.BytesIO(), pid=self.record["runtime_pid"], poll=lambda: None)
        rt._process_started_at = self.record["runtime_started_at"]
        rt._bg_tasks["bg"] = {"kind": "bash"}
        reg = RT.RuntimeRegistry()
        reg._runtimes[self.spec.name] = rt
        with patch.object(RT, "_REGISTRY", reg):
            RT._shutdown_runtimes()
        self.assertTrue(H.view(self.spec.name)["owner_exiting"])
        with self.assertRaisesRegex(PairError, "still finishing"):
            H.guard(self.spec.name)

    def test_placeholder_launch_flag_keeps_receipt_without_a_runtime_snapshot(self):
        self.state.status = "done"
        self.state.result = SendResult(name=self.spec.name, session_id=self.spec.session_id,
            response="Background launched", model_used="claude-haiku-5-5", duration_ms=1,
            background_launches=["bash"])
        with patch.object(RT, "_REGISTRY", None):
            A._finish_task(self.state, threading.Event())
        self.assertTrue(H.path_for(self.spec.name).exists())

    def test_self_wake_registration_does_not_block_interrupt_ack_on_control(self):
        H.path_for(self.spec.name).unlink()
        A._task_path(self.tid).unlink()
        rt = RT.PairRuntime(self.spec, ClaudeAdapter())
        scope_open, interrupted = threading.Event(), threading.Event()
        receipt_path = str(H.path_for(self.spec.name))
        def write(frame):
            self.assertEqual(json.loads(frame)["request"]["subtype"], "interrupt")
            interrupted.set()
            return len(frame)
        rt.proc = SimpleNamespace(pid=os.getpid(), stdin=Mock(write=write), poll=lambda: None)
        rt._process_started_at = RT.process_identity.started_at(os.getpid())
        rt.stop = Mock(return_value="tree-killed")
        reg = RT.RuntimeRegistry()
        reg._runtimes[self.spec.name] = rt
        register = A.register_external_task
        registered, errors, stopped = [], [], []
        def held_registration(*args, **kwargs):
            scope_open.set()
            if not interrupted.wait(3):
                raise AssertionError("stop never wrote its interrupt")
            task = register(*args, **kwargs)
            registered.append(task.task_id)
            return task
        def read():
            try:
                rt._on_event_for_log(json.dumps({"type": "assistant", "message": {"content": []}}))
                rt._on_event_for_log(json.dumps({"type": "result", "subtype": "error_during_execution",
                                                "result": "Interrupted", "duration_ms": 1}))
            except BaseException as exc:
                errors.append(exc)
        def stop():
            try:
                stopped.append(tool(S.pair_stop)(self.spec.name))
            except BaseException as exc:
                errors.append(exc)
        with patch.object(A, "register_external_task", side_effect=held_registration), \
                patch.object(S.runtime_mod, "registry", return_value=reg), patch.object(RT, "_REGISTRY", reg):
            reader = threading.Thread(target=read)
            stopper = threading.Thread(target=stop)
            reader.start()
            try:
                self.assertTrue(scope_open.wait(3))
                stopper.start()
                stopper.join(4)
            finally:
                interrupted.set()
                reader.join(5)
                if stopper.ident is not None:
                    stopper.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(reader.is_alive())
        self.assertFalse(stopper.is_alive())
        rt.stop.assert_not_called()
        self.assertIn("sent in-band interrupt", stopped[0])
        self.assertEqual(A.load_task(registered[0]).claude_handoff_path, receipt_path)

    def test_recovery_keeps_receipt_when_result_cannot_be_persisted(self):
        self.transcript()
        self.alive = False
        with patch.object(A, "_save", side_effect=PermissionError("locked")), \
                patch.object(A, "_retry_persist_in_background"):
            self.assertEqual(H.reconcile(self.spec.name).status, "done")
            self.assertTrue(H.path_for(self.spec.name).exists())
        self.assertEqual(H.reconcile(self.spec.name).status, "done")
        self.assertFalse(H.path_for(self.spec.name).exists())

    def test_dead_runtime_without_final_text_ignores_subagent_and_interim_text(self):
        path = self.transcript(final=False)
        with path.open("a", encoding="utf-8") as stream:
            for event in ({"type": "assistant", "message": {"stop_reason": "tool_use",
                          "content": [{"type": "text", "text": "INTERIM canary"}]}},
                          {"type": "assistant", "isSidechain": True, "message": {"stop_reason": "end_turn",
                          "content": [{"type": "text", "text": "SUBAGENT canary"}]}}):
                event["timestamp"] = datetime.now(timezone.utc).isoformat()
                stream.write(json.dumps(event) + "\n")
        self.alive = False
        self.assertEqual(A.reap_orphan(self.tid).status, "failed")
        self.assertFalse(H.path_for(self.spec.name).exists())

    def test_startup_cleans_a_dead_receipt_for_an_already_finished_task(self):
        self.state.status = "done"
        A._save(self.state)
        self.alive = False
        A._sweep_dead_predecessor_orphans()
        self.assertFalse(H.path_for(self.spec.name).exists())

    def test_unknown_process_identity_blocks_a_replacement(self):
        self.born = None
        self.assertTrue(H.view(self.spec.name)["identity_unknown"])
        with self.assertRaisesRegex(PairError, "still finishing"):
            H.guard(self.spec.name)

    def test_reused_server_pid_does_not_hide_its_surviving_child(self):
        self.record["server_started_at"] = 99.8
        H.publish(self.record)
        with patch.object(V, "_av_proc", side_effect=lambda pid:
                (True, 100.2 if pid == self.record["server_pid"] else 100.0)):
            self.assertTrue(H.view(self.spec.name)["detached"])
            with self.assertRaisesRegex(PairError, "still finishing"):
                H.guard(self.spec.name)

    def test_spawn_without_creation_time_delivers_no_input(self):
        H.path_for(self.spec.name).unlink()
        adapter = Mock(spec=ClaudeAdapter)
        adapter._spawn_env.return_value = {}
        adapter._common_create_args.return_value = []
        adapter._extra_args.return_value = []
        adapter.cli_model.return_value = "haiku"
        adapter.native_permission.return_value = "manual"
        proc = SimpleNamespace(pid=123456789, stdin=io.BytesIO())
        rt = RT.PairRuntime(self.spec, adapter)
        with patch.object(RT.subprocess, "Popen", return_value=proc), \
                patch.object(RT, "_claude_executable", return_value="fixture"), \
                patch.object(RT.process_identity, "started_at", return_value=None):
            with self.assertRaisesRegex(CLIError, "creation time; no message was delivered"):
                rt.start()
        self.assertTrue(proc.stdin.closed)
        self.assertIsNone(rt.proc)

    def test_missing_receipt_with_bounded_pane_reader_is_not_a_handoff(self):
        H.path_for(self.spec.name).unlink()
        with patch.object(V, "_av_read_json", return_value=(None, "missing or bounded read")):
            self.assertIsNone(H.view(self.spec.name))

    def test_self_wake_registration_gap_matches_only_the_new_executing_task(self):
        self.record["task_id"] = None
        H.publish(self.record)
        self.assertEqual(A.reap_orphan(self.tid).status, "running")
        queued = self.state.model_copy(update={"task_id": str(uuid.uuid4()), "queued": True})
        A._save(queued)
        self.assertTrue(A.reap_orphan(queued.task_id).error.startswith("NOT DELIVERED"))
        self.transcript()
        self.alive = False
        self.assertEqual(A.reap_orphan(self.tid).status, "done")
        self.assertFalse(H.path_for(self.spec.name).exists())

    def test_old_completion_cannot_unlink_the_next_receipt(self):
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        original_view = H.view
        def read(name):
            record = original_view(name)
            entered.set()
            release.wait(3)
            return record
        def publish():
            H.publish({**self.record, "task_id": "next-turn"})
            published.set()
        with patch.object(H, "view", side_effect=read):
            old = threading.Thread(target=H.discard, args=(self.spec.name, self.tid))
            old.start()
            self.assertTrue(entered.wait(3))
            new = threading.Thread(target=publish)
            new.start()
            try:
                self.assertFalse(published.wait(.05))
            finally:
                release.set()
                old.join(3)
                new.join(3)
        self.assertTrue(published.is_set())
        self.assertEqual(H.view(self.spec.name)["task_id"], "next-turn")

    def test_slow_exit_publication_has_one_shared_budget(self):
        rt = RT.PairRuntime(self.spec, ClaudeAdapter())
        rt.proc = SimpleNamespace(stdin=io.BytesIO(), pid=self.record["runtime_pid"])
        rt._current_scope = RT.TurnLogScope(rt.main_log_path, 1)
        rt._current_scope.task_id = self.tid
        rt._handoff_record = self.record
        reg = RT.RuntimeRegistry()
        reg._runtimes[self.spec.name] = rt
        finished = threading.Event()
        def blocked(record):
            time.sleep(.25)
            finished.set()
        with patch.object(H, "publish", side_effect=blocked), patch.object(RT, "_REGISTRY", reg):
            started = time.perf_counter()
            RT._shutdown_runtimes()
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, .23)
        self.assertTrue(rt.proc.stdin.closed)
        self.assertTrue(H.path_for(self.spec.name).exists())  # pre-delivery receipt survived
        self.assertTrue(finished.wait(2))

    def test_atexit_without_thread_creation_keeps_prepared_receipt(self):
        rt = RT.PairRuntime(self.spec, ClaudeAdapter())
        rt.proc = SimpleNamespace(stdin=io.BytesIO(), pid=self.record["runtime_pid"])
        rt._current_scope = RT.TurnLogScope(rt.main_log_path, 1)
        rt._current_scope.task_id = self.tid
        rt._handoff_record = self.record
        reg = RT.RuntimeRegistry()
        reg._runtimes[self.spec.name] = rt
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("interpreter shutdown")), \
                patch.object(RT, "_REGISTRY", reg):
            RT._shutdown_runtimes()
        self.assertTrue(rt.proc.stdin.closed)
        self.assertTrue(H.path_for(self.spec.name).exists())

    def test_missing_final_and_stale_pid_fail_honestly(self):
        self.transcript(final=False)
        self.born = 200.0  # the PID was reused; this is not our old runtime
        state = A.reap_orphan(self.tid)
        self.assertEqual(state.status, "failed")
        self.assertTrue(state.error.startswith(A.ORPHAN_ERROR_PREFIX))
        self.assertIn("No final assistant text", state.error)
        self.assertFalse(H.path_for(self.spec.name).exists())

    def test_spawn_and_mutation_paths_refuse_while_finishing(self):
        adapter = ClaudeAdapter()
        with patch.object(RT.subprocess, "Popen", side_effect=AssertionError("must not spawn")), \
                patch.object(adapter, "_spawn_env", side_effect=AssertionError("must not prepare a spawn")):
            for action in (lambda: RT.RuntimeRegistry().get_or_start(self.spec, adapter),
                           lambda: RT.PairRuntime(self.spec, adapter).start(),
                           lambda: adapter._run_print([], timeout_seconds=1, pair_name=self.spec.name),
                           lambda: adapter._run_stream_json(self.spec, ["/compact"], timeout_seconds=1),
                           lambda: tool(S.pair_update)(self.spec.name, cwd=str(self.home / "new")),
                           lambda: tool(S.pair_clear)(self.spec.name)):
                with self.assertRaisesRegex(PairError, "previous runtime still finishing"):
                    action()

    def test_send_queues_until_receipt_exits_then_delivers_once(self):
        self.transcript()
        adapter = Mock(spec=ClaudeAdapter)
        adapter.send.return_value = SendResult(name=self.spec.name, session_id=self.spec.session_id,
                                               response="next reply", model_used="claude-haiku-5-5", duration_ms=1)
        with patch.object(S, "_adapter_for", return_value=adapter), \
                patch.object(S, "_alias_resolution_note", return_value=None), \
                patch.object(S.completion, "arm", return_value=None):
            ack = tool(S.pair_send_async)(self.spec.name, "next message")
            self.assertIn(H.FINISHING, ack)
            queued = A.latest_task_id_for_pair(self.spec.name)
            time.sleep(.15)
            adapter.send.assert_not_called()
            self.assertTrue(A.load_task(queued).queued)
            self.alive = False
            done = A.wait_for_task(queued, 5)
            self.assertEqual(done.status, "done", done.error)
            adapter.send.assert_called_once()
            self.assertEqual(A.load_task(self.tid).status, "done")

    def test_owner_exits_during_foreign_self_wake_wait(self):
        self.transcript()
        exiting = threading.Event()
        original_view = H.view
        def view(name):
            record = original_view(name)
            return {**record, "detached": False} if record and not exiting.is_set() else record
        adapter = Mock(spec=ClaudeAdapter)
        adapter.send.return_value = SendResult(name=self.spec.name, session_id=self.spec.session_id,
                                               response="next", model_used="claude-haiku-5-5", duration_ms=1)
        with patch.object(H, "view", side_effect=view), \
                patch.object(S, "_wait_for_foreign_self_woken", side_effect=lambda *a: exiting.set()), \
                patch.object(S, "_adapter_for", return_value=adapter), \
                patch.object(S, "_alias_resolution_note", return_value=None), \
                patch.object(S.completion, "arm", return_value=None):
            tool(S.pair_send_async)(self.spec.name, "next")
            queued = A.latest_task_id_for_pair(self.spec.name)
            self.assertTrue(exiting.wait(3))
            time.sleep(.1)
            adapter.send.assert_not_called()
            self.alive = False
            result = A.wait_for_task(queued, 5)
            self.assertEqual(result.status, "done", result.error)
            adapter.send.assert_called_once()

    def test_shutdown_six_busy_and_two_idle_runtimes_stays_under_budget(self):
        reg = RT.RuntimeRegistry()
        for i in range(8):
            spec = self.spec.model_copy(update={"name": f"timing-{i}"})
            rt = RT.PairRuntime(spec, ClaudeAdapter())
            rt.proc = SimpleNamespace(stdin=io.BytesIO(), pid=os.getpid(), poll=lambda: None)
            rt.stop = Mock(side_effect=AssertionError("no tree kill at shutdown"))
            if i < 6:
                rt._current_scope = RT.TurnLogScope(rt.main_log_path, 1)
                rt._current_scope.task_id = self.tid
                rt._handoff_record = {**self.record, "pair": spec.name, "runtime_pid": os.getpid(),
                                      "runtime_started_at": RT.process_identity.started_at(os.getpid())}
                H.publish(rt._handoff_record)
            reg._runtimes[spec.name] = rt
        with patch.object(RT, "_REGISTRY", reg), \
                patch.object(RT._activity, "shutdown", side_effect=AssertionError("slow bookkeeping off exit path")):
            started = time.perf_counter()
            RT._shutdown_runtimes()
            elapsed = time.perf_counter() - started
        print(f"Shutdown, 6 busy + 2 idle: {elapsed * 1000:.3f} ms", flush=True)
        self.assertLess(elapsed, .2)
        self.assertTrue(all(rt.proc.stdin.closed for rt in reg._runtimes.values()))
        self.assertTrue(all(rt._handed_off for rt in reg._runtimes.values()))
        self.assertTrue(all(H.path_for(f"timing-{i}").exists() for i in range(6)))
        self.assertTrue(all(json.loads(H.path_for(f"timing-{i}").read_text())["owner_exiting"] for i in range(6)))
        A._cleanup_orphaned_running_tasks()
        self.assertEqual(A.load_task(self.tid).status, "running")

    def test_stdio_eof_exits_quickly_and_fake_child_finishes(self):
        child = self.home / "child.py"
        child.write_text('''import sys,time,json
from pathlib import Path
from datetime import datetime,timezone
sys.stdin.readline()
Path(sys.argv[2]).write_text('started')
while not Path(sys.argv[3]).exists(): time.sleep(.02)
event={'type':'assistant','timestamp':datetime.now(timezone.utc).isoformat(),
       'message':{'model':'claude-haiku-fixture','stop_reason':'end_turn','content':[{'type':'text','text':'CANARY finished'}]}}
path=Path(sys.argv[1]); path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(event)+'\\n')
sys.stdin.read()
''', encoding="utf-8")
        ready, marker, release = self.home / "ready.json", self.home / "started", self.home / "release"
        bootstrap = self.home / "server.py"
        bootstrap.write_text('''import json,os,subprocess,sys,uuid
from pathlib import Path
from datetime import datetime
from claude_squared import server as S, runtime as RT, registry as R, async_tasks as A, process_identity as P
from claude_squared.models import PairSpec, AsyncTaskState
from claude_squared.adapters.claude import ClaudeAdapter
home=Path(os.environ['CLAUDE_HOME'])
spec=PairSpec(name='stdio-survivor',session_id=str(uuid.uuid4()),cwd=str(home),model='haiku')
R.add_pair(spec)
rt=RT.PairRuntime(spec,ClaudeAdapter())
rt.proc=subprocess.Popen([sys.executable,str(home/'child.py'),str(rt._jsonl_path),str(home/'started'),str(home/'release')],
                         stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,bufsize=0)
rt._process_started_at=P.started_at(rt.proc.pid)
tid=str(uuid.uuid4())
A._save(AsyncTaskState(task_id=tid,pair_name=spec.name,message='fixture',status='running',owner_pid=os.getpid(),started_at=datetime.utcnow()))
rt._current_scope=RT.TurnLogScope(rt.main_log_path,1); rt._current_scope.task_id=tid
rt._current_scope.input_sent=True
RT._REGISTRY=RT.RuntimeRegistry(); RT._REGISTRY._runtimes[spec.name]=rt
rt._turn_opened(tid,'send')
rt.proc.stdin.write(b'go\\n'); rt.proc.stdin.flush()
(home/'ready.json').write_text(json.dumps({'task_id':tid,'pid':rt.proc.pid,'born':rt._process_started_at}))
S.run()
''', encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
        parent = subprocess.Popen([sys.executable, "-B", str(bootstrap)], env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            end = time.monotonic() + 15
            while not (ready.exists() and marker.exists()):
                if parent.poll() is not None:
                    self.fail(parent.stderr.read().decode("utf-8", "replace"))
                if time.monotonic() > end:
                    self.fail("fake server did not become ready")
                time.sleep(.02)
            data = json.loads(ready.read_text())
            # Read readiness through the actual MCP transport so EOF cannot race
            # the hook installation. Only this server and its fake child are ours.
            parent.stdin.write((json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
                "protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"offline-test","version":"1"}}})+"\n").encode())
            parent.stdin.flush()
            self.assertIn(b'"id":1', parent.stdout.readline().replace(b" ", b""))
            started = time.perf_counter()
            parent.stdin.close()
            parent.wait(timeout=2)
            elapsed = time.perf_counter() - started
            print(f"Real stdio EOF -> server exit: {elapsed * 1000:.1f} ms", flush=True)
            self.assertLess(elapsed, .3)
            receipt = H.view("stdio-survivor")
            self.assertIsNotNone(receipt)
            self.assertTrue(receipt["alive"])
            self.assertEqual(A.load_task(data["task_id"]).status, "running")
            release.touch()
            end = time.monotonic() + 5
            while H.view("stdio-survivor")["alive"]:
                if time.monotonic() > end:
                    self.fail("fake child did not finish after parent exit")
                time.sleep(.05)
            done = A.reap_orphan(data["task_id"])
            self.assertEqual(done.status, "done", done.error)
            self.assertEqual(done.result.response, "CANARY finished")
        finally:
            release.touch()  # allow our fake child to exit even after a failed assertion
            if parent.poll() is None:
                parent.terminate()  # only the exact server PID spawned above
            parent.wait(timeout=5)
            if parent.stdin and not parent.stdin.closed:
                parent.stdin.close()
            parent.stdout.close()
            parent.stderr.close()

    def test_stdio_restore_tolerates_a_closed_sdk_buffer(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
               "CLAUDE_HOME": str(self.home / "closed-buffer"), "CODEX_HOME": str(self.home / "closed-codex")}
        result = subprocess.run([sys.executable, "-B", "-c",
            "from claude_squared import runtime as R; import sys; "
            "original=sys.stdin; restore=R.install_stdin_eof_exit(); "
            "sys.stdin.buffer.close(); restore(); assert sys.stdin is original"],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
