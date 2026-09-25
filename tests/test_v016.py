"""Offline tests for v0.16 phase 2, Claude side: runtime -> activity wiring,
the stale-resume ("phantom") result guard, background protection, the
cross-process background wait, wait.py pair mode, interim triage, PID
liveness and the allowed_tools denial hint."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import io
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
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-v016-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = TEST_HOME.name

from claude_squared import activity as A  # noqa: E402
from claude_squared import async_tasks as AT  # noqa: E402
from claude_squared import registry as R  # noqa: E402
from claude_squared import runtime as RT  # noqa: E402
from claude_squared import server as S  # noqa: E402
from claude_squared import settings as ST  # noqa: E402
from claude_squared import triage as TR  # noqa: E402
from claude_squared import _wait_script as W  # noqa: E402
from claude_squared.adapters.claude import ClaudeAdapter  # noqa: E402
from claude_squared.errors import PairError  # noqa: E402
from claude_squared.models import CreateResult, PairSpec, PermissionDenial, SendResult  # noqa: E402
from claude_squared.runtime import PairRuntime  # noqa: E402

# Recorded 2026-09-25 (probes/measure.py run F, CLI 2.1.280): what a fresh
# --resume printed on its own, 0.74 s after spawn, after the previous runtime
# was tree-killed with a background shell still running.
STALE_NOTICE = {"type": "system", "subtype": "task_notification", "task_id": "b3133hf8g",
                "tool_use_id": "toolu_01WYzqSK1MpZFKoUxp36gnZ9", "status": "stopped",
                "output_file": "",
                "summary": "Background shell command didn't finish before the previous session ended"}
STALE_INIT = {"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001", "tools": []}
STALE_RESULT = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 52,
                "result": "", "total_cost_usd": 0,
                "usage": {"input_tokens": 0, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
                "modelUsage": {"claude-haiku-4-5-20251001": {"contextWindow": 200000}}}


def tool(value):
    return getattr(value, "fn", value)


def assistant(text="ok", *, used=0, parent=None, msg_id="m1"):
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"id": msg_id, "role": "assistant", "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": used,
                                  "cache_creation_input_tokens": 0}}}


def result(text="done", *, terminal="completed", window=200000, subtype="success"):
    ev = {"type": "result", "subtype": subtype, "is_error": False, "duration_ms": 900,
          "result": text, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
          "modelUsage": {"claude-haiku-4-5-20251001": {"contextWindow": window}},
          "stop_reason": "end_turn", "permission_denials": []}
    if terminal:
        ev["terminal_reason"] = terminal
    return ev


def task_started(tid, backgrounded=True, kind="local_bash"):
    ev = {"type": "system", "subtype": "task_started", "task_id": tid, "task_type": kind,
          "description": f"work {tid}"}
    if backgrounded is not None:
        ev["is_backgrounded"] = backgrounded
    return ev


def task_done(tid, status="completed"):
    return {"type": "system", "subtype": "task_notification", "task_id": tid, "status": status,
            "summary": "done"}


class _AliveProc:
    """A stand-in claude process: alive, and never really killed (stop() is
    wrapped to drop it before the taskkill path)."""
    pid = 0
    returncode = None
    stdin = None

    def poll(self):
        return None


class V016Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        cls.dead = subprocess.Popen([sys.executable, "-c", "pass"])
        cls.dead.wait(30)
        end = time.monotonic() + 10
        while A._av_proc(cls.live.pid)[1] is None and time.monotonic() < end:
            time.sleep(0.05)
        cls.live_started = A._av_proc(cls.live.pid)[1]

    @classmethod
    def tearDownClass(cls):
        cls.live.kill()  # our own child, by PID
        cls.live.wait(10)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=TEST_HOME.name)))
        self.stack.enter_context(patch.dict(os.environ, {"CLAUDE_HOME": str(self.home)}))
        self.pairs = self.home / "pairs"
        self.pair = f"p{uuid.uuid4().hex[:6]}"
        self.addCleanup(self.cleanup_process_state)

    def cleanup_process_state(self):
        RT.registry()._runtimes.pop(self.pair, None)
        with A._WRITERS_LOCK:
            keys = [k for k in A._WRITERS if k[0] == str(self.pairs)]
            retired = [A._WRITERS.pop(k) for k in keys]
        for w in retired:
            with w._io_lock:
                with w._lock:
                    w._forgotten = True

    # -- helpers --

    def runtime(self, **spec_fields) -> PairRuntime:
        work = self.home / "work"
        work.mkdir(exist_ok=True)
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(work),
                        model="haiku", effort=None, **spec_fields)
        R.add_pair(spec)
        rt = PairRuntime(spec, ClaudeAdapter())
        rt._drift_checked = True
        return rt

    def live_runtime(self, **spec_fields) -> PairRuntime:
        """Registered in the process registry, 'alive', and safe to stop."""
        rt = self.runtime(**spec_fields)
        rt.proc = _AliveProc()
        real_stop = rt.stop

        def safe_stop(reason=None):
            rt.proc = None  # never reach the taskkill path with a fake pid
            return real_stop(reason)
        rt.stop = safe_stop
        RT.registry()._runtimes[self.pair] = rt
        return rt

    @staticmethod
    def feed(rt, ev):
        return rt._on_event_for_log(json.dumps(ev))

    def snap(self):
        return A.writer(self.pair).snapshot()

    def main_log(self, rt) -> str:
        return rt.main_log_path.read_text(encoding="utf-8")

    def foreign_activity(self, pid, started, **fields):
        now = A._av_iso(A._av_now())
        data = {"version": 1, "pair": self.pair, "backend": "claude", "owner_pid": pid,
                "owner_started_at": started, "state": "live", "updated_at": now,
                "last_transition_at": now, "window_started_at": None, "turn": None,
                "background": [], "wake_pending": None, "context": None, "window_turns": [],
                "background_finished": [], "background_lost": [], "compactions": []}
        data.update(fields)
        path = self.pairs / "activity" / self.pair / f"{pid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


class StaleResumeResultTests(V016Case):
    def drive_reader(self, rt, events):
        """Run the real stdout reader over recorded lines."""
        raw = b"".join(json.dumps(e).encode("utf-8") + b"\n" for e in events)
        rt.proc = SimpleNamespace(stdout=io.BytesIO(raw))
        rt._stdout_reader()

    def open_solicited(self, rt, task_id="t-send"):
        with patch.object(AT, "mark_task_executing", lambda tid: None):
            rt._prepare_solicited_turn(5, None, task_id)

    def test_phantom_result_is_not_the_reply(self):
        rt = self.runtime()
        self.open_solicited(rt)
        self.drive_reader(rt, [STALE_NOTICE, STALE_INIT, STALE_RESULT,
                               {"type": "system", "subtype": "init"}, assistant("PING-OK"),
                               result("PING-OK")])
        queued = []
        while not rt._stdout_q.empty():
            queued.append(json.loads(rt._stdout_q.get_nowait()))
        results = [e for e in queued if e.get("type") == "result"]
        self.assertEqual([r["result"] for r in results], ["PING-OK"])
        self.assertEqual(rt._result_seq, 1)  # the phantom never counts as an interrupt ack
        log = self.main_log(rt)
        self.assertIn("STALE RESUME NOTICE", log)
        self.assertEqual(log.count("=== TURN "), 1)  # no phantom turn marker for pair_poll
        self.assertIsNone(rt._implicit_scope)

    def test_phantom_without_a_send_leaves_no_turn_or_wake(self):
        rt = self.runtime()
        self.drive_reader(rt, [STALE_NOTICE, STALE_INIT, STALE_RESULT])
        snap = self.snap()
        self.assertIsNone(snap["turn"])
        self.assertIsNone(snap["wake_pending"])
        self.assertEqual(rt._result_seq, 0)
        self.assertEqual(R.get_pair(self.pair).self_woken_pending, [])

    def test_real_replies_are_never_swallowed(self):
        # A fast real reply has an assistant event; an interrupt ack has a
        # terminal_reason; a known task's notification never arms the guard.
        rt = self.runtime()
        self.open_solicited(rt)
        self.drive_reader(rt, [STALE_NOTICE, assistant("fast"), result("fast", terminal=None)])
        self.assertEqual(rt._result_seq, 1)
        rt._current_scope = None
        self.open_solicited(rt, "t2")
        self.drive_reader(rt, [STALE_NOTICE, result("", terminal="aborted_streaming",
                                                     subtype="error_during_execution")])
        self.assertEqual(rt._result_seq, 2)
        rt._current_scope = None
        self.open_solicited(rt, "t3")
        self.drive_reader(rt, [task_started("known"), task_done("known", "stopped"),
                               result("", terminal=None)])
        self.assertEqual(rt._result_seq, 3)


class WiringTests(V016Case):
    def test_foreground_work_is_never_pending_background(self):
        rt = self.runtime()
        self.feed(rt, task_started("fg", backgrounded=False))
        self.feed(rt, task_started("agent", backgrounded=False, kind="local_agent"))
        self.assertEqual(rt.pending_background_tasks(), [])
        self.assertEqual(self.snap()["background"], [])
        self.assertIn("(foreground)", self.main_log(rt))
        self.feed(rt, task_done("fg"))
        self.assertIsNone(rt._stale_notice)  # a known task, not a stale resume notice
        self.feed(rt, task_started("legacy", backgrounded=None))  # pre-2.1.280: counts
        self.assertEqual([b["task_id"] for b in rt.pending_background_tasks()], ["legacy"])

    def test_a_foreground_command_moved_to_the_background_becomes_pending(self):
        rt = self.runtime()
        self.feed(rt, task_started("slow", backgrounded=False))
        self.feed(rt, {"type": "system", "subtype": "task_updated", "task_id": "slow",
                       "patch": {"is_backgrounded": True}})
        self.assertEqual([b["task_id"] for b in self.snap()["background"]], ["slow"])
        self.feed(rt, {"type": "system", "subtype": "task_updated", "task_id": "slow",
                       "patch": {"status": "completed"}})
        self.feed(rt, task_done("slow"))  # the same completion again: reported once
        self.assertEqual(len(self.snap()["background_finished"]), 1)

    def test_turns_background_wake_and_triage_reach_the_activity_file(self):
        rt = self.runtime()
        with patch.object(AT, "mark_task_executing", lambda tid: None):
            rt._prepare_solicited_turn(5, None, "t-send")
        self.assertEqual(self.snap()["turn"]["kind"], "send")
        self.feed(rt, task_started("bg1"))
        self.feed(rt, task_started("bg2"))
        self.feed(rt, assistant("launched", used=50_000))
        self.feed(rt, result("launched"))
        rt._current_scope = None  # send()'s finally
        entry = self.snap()["window_turns"][-1]
        self.assertEqual((entry["task_id"], entry["interim"], entry["bg_pending_after"]),
                         ("t-send", True, 2))
        self.assertIsNone(entry["triage"])  # solicited replies are never triaged
        self.feed(rt, task_done("bg1"))
        self.assertIsNotNone(self.snap()["wake_pending"])
        self.feed(rt, {"type": "system", "subtype": "init"})
        self.feed(rt, assistant("bg1 finished; should I delete the old files?"))
        snap = self.snap()
        self.assertEqual(snap["turn"]["kind"], "self_woken")
        self.assertIsNone(snap["wake_pending"])
        self.feed(rt, {"type": "system", "subtype": "post_turn_summary",
                       "status_category": "blocked", "status_detail": "asks about deleting files",
                       "needs_action": "confirm deletion"})
        self.feed(rt, result("bg1 finished; should I delete the old files?"))
        entry = self.snap()["window_turns"][-1]
        self.assertEqual((entry["kind"], entry["interim"]), ("self_woken", True))
        self.assertEqual((entry["triage"]["source"], entry["triage"]["needs_attention"]),
                         ("cli_summary", True))
        self.assertEqual(self.snap()["window_started_at"], snap["window_started_at"])

    def test_init_with_no_turn_open_expects_a_wake(self):
        rt = self.runtime()
        self.feed(rt, {"type": "system", "subtype": "init"})
        self.assertIsNotNone(self.snap()["wake_pending"])
        self.feed(rt, assistant("hi"))
        self.assertIsNone(self.snap()["wake_pending"])

    def test_context_reported_window_and_compaction(self):
        rt = self.runtime()
        self.feed(rt, assistant("x", used=90_000))
        ctx = self.snap()["context"]
        self.assertEqual((ctx["used"], ctx["window"], ctx["window_source"]),
                         (90_010, 200_000, "inferred"))
        self.feed(rt, assistant("sub", used=5, parent="toolu_x"))  # a sub-agent's own context
        self.assertEqual(self.snap()["context"]["used"], 90_010)
        self.feed(rt, result("x", window=1_000_000))
        self.feed(rt, assistant("y", used=100_000, msg_id="m2"))
        ctx = self.snap()["context"]
        self.assertEqual((ctx["window"], ctx["window_source"]), (1_000_000, "reported"))
        self.assertTrue(A.writer(self.pair).flush())
        self.assertEqual(A.reported_window(self.pair, f"haiku|default")["tokens"], 1_000_000)
        self.assertEqual(PairRuntime(rt.spec, ClaudeAdapter())._reported_window, 1_000_000)
        self.feed(rt, {"type": "system", "subtype": "compact_boundary",
                       "compact_metadata": {"trigger": "auto", "pre_tokens": 967_000,
                                            "post_tokens": 61_000}})
        self.assertEqual(self.snap()["compactions"][-1]["pre"], 967_000)

    def test_stop_records_lost_background_and_the_footer_says_so(self):
        rt = self.runtime()
        self.feed(rt, task_started("bgX"))
        self.assertEqual(rt.stop("evicted for the test"), "no runtime")
        self.assertEqual([b["task_id"] for b in rt.last_lost_background], ["bgX"])
        self.assertEqual(self.snap()["background_lost"][-1]["reason"], "evicted for the test")
        pending = R.get_pair(self.pair).self_woken_pending
        self.assertEqual(pending[-1]["status"], "background_lost")
        out = S._fmt_send_result(SendResult(name=self.pair, response="r", session_id="s",
                                            model_used="haiku", duration_ms=1,
                                            self_woken_completed=pending))
        self.assertIn("BACKGROUND WORK LOST", out)
        self.assertNotIn("SELF-WOKEN TURN", out)
        self.assertIn("BACKGROUND LOST", self.main_log(rt))


class RegistryTests(V016Case):
    def test_evictor_spares_background_until_the_ceiling(self):
        reg = RT.RuntimeRegistry(idle_timeout_seconds=1)
        rt = self.runtime()
        rt.proc = _AliveProc()
        reasons = []
        rt.stop = lambda reason=None: reasons.append(reason) or "stopped"
        self.feed(rt, task_started("bg"))
        rt.last_activity = datetime.utcnow() - timedelta(hours=2)
        reg._runtimes[self.pair] = rt
        reg._evict_idle()
        self.assertIn(self.pair, reg._runtimes)
        rt.last_activity = datetime.utcnow() - timedelta(hours=25)
        reg._evict_idle()
        self.assertNotIn(self.pair, reg._runtimes)
        self.assertIn("may already have finished", reasons[0])

    def test_runtimes_are_stopped_outside_the_registry_lock(self):
        reg = RT.RuntimeRegistry()
        rt = self.runtime()
        seen = []
        rt.stop = lambda reason=None: seen.append(reg._lock.locked()) or "stopped"
        reg._runtimes[self.pair] = rt
        reg.evict(self.pair, "x")
        reg._runtimes[self.pair] = rt
        reg.evict_if_current(self.pair, rt)
        reg._runtimes[self.pair] = rt
        reg.stop_all()
        self.assertEqual(seen, [False, False, False])


class ProtectionTests(V016Case):
    def with_background(self):
        rt = self.live_runtime()
        self.feed(rt, task_started("bgP", kind="local_agent"))
        return rt

    def test_clear_refuses_then_force_kills_and_reports(self):
        self.with_background()
        with self.assertRaises(PairError) as cm:
            tool(S.pair_clear)(self.pair)
        self.assertIn("force=True", str(cm.exception))
        self.assertIn("bgP", str(cm.exception))
        with patch.object(ClaudeAdapter, "create",
                          lambda self_, spec, initial_message=None: CreateResult(
                              name=spec.name, session_id=spec.session_id)):
            out = tool(S.pair_clear)(self.pair, force=True)
        self.assertIn("killed and recorded as lost", out)
        self.assertEqual(self.snap()["background_lost"][-1]["task_id"], "bgP")

    def test_update_refuses_only_spawn_pinned_changes(self):
        self.with_background()
        self.assertIn("purpose", tool(S.pair_update)(self.pair, purpose="new purpose"))
        with self.assertRaises(PairError):
            tool(S.pair_update)(self.pair, permission_mode="read-only")
        out = tool(S.pair_update)(self.pair, permission_mode="read-only", force=True)
        self.assertIn("killed and recorded as lost", out)

    def test_compact_refuses_at_entry(self):
        self.with_background()
        with self.assertRaises(PairError):
            tool(S.pair_compact)(self.pair)

    def test_forget_kills_and_lists_losses(self):
        self.with_background()
        out = tool(S.pair_forget)(self.pair, archive=False)
        self.assertIn("killed and recorded as lost", out)
        self.assertFalse((self.pairs / "activity" / self.pair / f"{os.getpid()}.json").exists())

    def test_soft_stop_reports_surviving_background(self):
        rt = self.with_background()
        out = tool(S.pair_stop)(self.pair)
        self.assertIn("still running and will wake the pair", out)
        self.assertEqual(rt.pending_background_tasks()[0]["task_id"], "bgP")
        out = tool(S.pair_stop)(self.pair, force=True)
        self.assertIn("killed and recorded as lost", out)

    def test_foreign_background_blocks_a_send_bounded(self):
        self.runtime()
        self.foreign_activity(self.live.pid, self.live_started, background=[
            {"task_id": "fbg", "kind": "local_bash", "description": "", "started_at": A._av_iso(A._av_now())}])
        self.assertIn(f"MCP process {self.live.pid}", S._foreign_activity_blocker(self.pair))
        t0 = time.monotonic()
        with self.assertRaises(PairError) as cm:
            S._wait_for_foreign_self_woken(self.pair, 1, None)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("fork the pair's history", str(cm.exception))
        self.foreign_activity(self.dead.pid, None, background=[{"task_id": "x"}])
        (self.pairs / "activity" / self.pair / f"{self.live.pid}.json").unlink()
        self.assertIsNone(S._foreign_activity_blocker(self.pair))  # dead owners don't block

    def test_status_shows_live_context_and_alert(self):
        rt = self.runtime()
        self.feed(rt, result("r", window=1_000_000))
        self.feed(rt, assistant("x", used=830_000))
        lines = "\n".join(S._activity_status_lines(self.pair))
        self.assertIn("830,010 / 1,000,000", lines)
        self.assertIn("past the context alert (820,000)", lines)
        self.assertIn("pair_compact", lines)


class WaitScriptTests(V016Case):
    def setUp(self):
        super().setUp()
        self.wait_py = W.install_wait_script(self.pairs / "wait.py")
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        self.w = A.writer(self.pair)

    def start(self, *args):
        env = dict(os.environ, CLAUDE_HOME=str(self.home), PYTHONIOENCODING="utf-8")
        return subprocess.Popen([sys.executable, str(self.wait_py), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", env=env)

    def finish(self, proc, timeout=40):
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err

    def test_script_embeds_the_view_verbatim(self):
        src = Path(A.__file__).read_text(encoding="utf-8")
        start = src.index(W._BEGIN)
        self.assertIn(src[start:src.index(W._END) + len(W._END)], W.WAIT_SCRIPT_SOURCE)

    def test_pair_mode_returns_when_idle_with_a_summary(self):
        self.w.turn_opened("t1", "send")
        self.w.background_started("b1", "local_bash")
        self.w.turn_closed("t1", preview="launched")
        self.assertTrue(self.w.flush())
        proc = self.start(self.pair, "--poll", "0.5", "--timeout", "60")
        time.sleep(1.5)
        self.w.background_finished("b1")
        self.w.turn_opened("sw", "self_woken")
        self.w.turn_closed("sw", preview="all done")
        self.assertTrue(self.w.flush())
        code, out, err = self.finish(proc)
        self.assertEqual(code, 0, out + err)
        self.assertIn("idle", out)
        self.assertIn("turns: 2 (1 send/compact/invoke, 1 self-woken; 1 interim)", out)
        self.assertIn("final reply: pair_poll('sw'", out)

    def test_each_turn_returns_at_the_next_turn_end(self):
        self.w.turn_opened("t1", "send")
        self.assertTrue(self.w.flush())
        proc = self.start(self.pair, "--each-turn", "--poll", "0.5", "--timeout", "60")
        time.sleep(1.5)
        self.w.background_started("b1", "local_bash")
        self.w.turn_closed("t1", preview="placeholder")
        self.assertTrue(self.w.flush())
        code, out, err = self.finish(proc)
        self.assertEqual(code, 0, out + err)
        self.assertIn("turn ended", out)
        self.assertIn("interim", out)

    def test_context_alert_fires_on_a_crossing_only(self):
        self.w.turn_opened("t1", "send")
        self.w.note_context(600_000, 1_000_000, window_source="reported")
        self.assertTrue(self.w.flush())
        proc = self.start(self.pair, "--context-alert", "50", "--poll", "0.5", "--timeout", "4")
        code, out, err = self.finish(proc)
        self.assertEqual(code, 3, out + err)  # already above at start: no alert, times out
        self.assertIn("still working", err)
        self.assertIn("--context-alert", err)  # the re-arm command keeps the flags
        self.w.note_context(400_000, 1_000_000, window_source="reported")
        self.assertTrue(self.w.flush())
        proc = self.start(self.pair, "--poll", "0.5", "--timeout", "60")
        time.sleep(1.5)
        self.w.note_context(830_000, 1_000_000, window_source="reported")
        self.assertTrue(self.w.flush())
        code, out, err = self.finish(proc)
        self.assertEqual(code, 8, out + err)
        self.assertIn("CONTEXT ALERT", out)
        self.assertIn("pair_compact", out)
        self.assertIn("--context-alert", out)

    def test_background_lost_exits_9(self):
        self.w.background_started("b1", "local_agent", "recon")
        self.assertTrue(self.w.flush())
        proc = self.start(self.pair, "--poll", "0.5", "--timeout", "60")
        time.sleep(1.5)
        self.w.lose_background("runtime evicted")
        self.assertTrue(self.w.flush())
        code, out, err = self.finish(proc)
        self.assertEqual(code, 9, out + err)
        self.assertIn("BACKGROUND LOST", err)

    def write_task(self, **fields):
        tid = str(uuid.uuid4())
        data = {"task_id": tid, "pair_name": self.pair, "message": "m", "status": "running",
                "started_at": datetime.utcnow().isoformat()}
        data.update(fields)
        (self.pairs / "async").mkdir(parents=True, exist_ok=True)
        (self.pairs / "async" / f"{tid}.json").write_text(json.dumps(data), encoding="utf-8")
        return tid

    def test_task_mode_detach_contract(self):
        events = self.home / "turn.events.jsonl"
        events.write_text("{}\n", encoding="utf-8")
        old = time.time() - 60
        os.utime(events, (old, old))
        tid = self.write_task(owner_pid=self.dead.pid, events_path=str(events))
        code, out, err = self.finish(self.start(tid, "--poll", "0.5"))
        self.assertEqual(code, 7, out + err)
        self.assertIn("finished detached", err)
        tid = self.write_task(owner_pid=self.dead.pid, events_path=str(events),
                              child_pid=self.live.pid)
        code, out, err = self.finish(self.start(tid, "--poll", "0.5", "--timeout", "2"))
        self.assertEqual(code, 3, out + err)  # the detached child still runs: keep waiting
        tid = self.write_task(owner_pid=self.dead.pid)
        code, out, err = self.finish(self.start(tid, "--poll", "0.5"))
        self.assertEqual(code, 4, out + err)

    def test_not_found_and_usage(self):
        code, _out, _err = self.finish(self.start("no-such-thing", "--poll", "0.5"))
        self.assertEqual(code, 2)
        code, _out, _err = self.finish(self.start(self.pair, "--bogus"))
        self.assertEqual(code, 64)


class TriageTests(V016Case):
    def test_cli_summary_mapping(self):
        self.assertIsNone(TR.from_summary(None))
        self.assertIsNone(TR.from_summary({"status_category": "", "status_detail": ""}))
        tri = TR.from_summary({"status_category": "blocked", "status_detail": "awaiting approval",
                               "needs_action": "approve the command"})
        self.assertEqual((tri["importance"], tri["needs_attention"], tri["source"]),
                         (3, True, "cli_summary"))
        tri = TR.from_summary({"status_category": "completed", "status_detail": "progress",
                               "needs_action": ""})
        self.assertEqual((tri["importance"], tri["needs_attention"]), (1, False))

    def test_classifier_reply_parsing_and_heuristic(self):
        fenced = '```json\n{"importance": 2, "needs_attention": false, "reason": "r", "summary": "s"}\n```'
        self.assertEqual(TR.parse_classifier_reply(fenced)["importance"], 2)
        self.assertIsNone(TR.parse_classifier_reply("no json here"))
        self.assertIsNone(TR.parse_classifier_reply('{"importance": 1}'))
        self.assertTrue(TR.heuristic("Done with part 1.\n\nShould I continue with part 2?")["needs_attention"])
        self.assertFalse(TR.heuristic("Part 1 finished; part 2 running.")["needs_attention"])

    def test_submit_paths(self):
        w = A.writer(self.pair)
        for tid in ("a", "b"):
            w.turn_opened(tid, "self_woken")
            w.turn_closed(tid, triage_pending=True)
        TR.submit(w, "a", {"status_category": "completed", "status_detail": "ok"}, "r", mode="auto")
        self.assertEqual(w.snapshot()["window_turns"][0]["triage"]["source"], "cli_summary")
        with patch.object(TR, "classify_with_haiku", lambda reply: {
                "importance": 3, "needs_attention": True, "reason": "x", "summary": "y", "source": "haiku"}):
            TR.submit(w, "b", {"status_category": "completed"}, "r", mode="haiku")
            end = time.monotonic() + 5
            while time.monotonic() < end and w.snapshot()["window_turns"][1]["triage"]["state"] == "pending":
                time.sleep(0.05)
        self.assertEqual(w.snapshot()["window_turns"][1]["triage"]["source"], "haiku")

    def test_usage_limit_reply_backs_off_to_the_heuristic(self):
        class FakePopen:
            pid = 0

            def __init__(self, *a, **kw):
                pass

            def communicate(self, data, timeout=None):
                return (json.dumps({"result": "You've hit your session limit · resets 5pm",
                                    "is_error": False}).encode(), b"")

        TR._BACKOFF_UNTIL[0] = 0.0
        with patch.object(TR.subprocess, "Popen", FakePopen):
            self.assertIsNone(TR.classify_with_haiku("progress"))
        self.assertGreater(TR._BACKOFF_UNTIL[0], time.monotonic())
        TR._BACKOFF_UNTIL[0] = 0.0

    def test_mode_setting(self):
        self.assertEqual(TR.current_mode(), "auto")
        ST.update_defaults(interim_triage="off")
        self.assertEqual(TR.current_mode(), "off")
        with self.assertRaises(ValueError):
            ST.update_defaults(interim_triage="sometimes")


class MiscTests(V016Case):
    def test_pid_liveness_reads_exit_codes(self):
        self.assertFalse(AT._is_pid_alive(self.dead.pid))  # exited, handle still held
        self.assertTrue(AT._is_pid_alive(self.live.pid))

    def test_allowed_tools_denial_hint(self):
        spec = PairSpec(name=self.pair, session_id="s", cwd=str(self.home), permission_mode="read-only",
                        allowed_tools=["Bash(git log*)"])
        R.add_pair(spec)
        r = SendResult(name=self.pair, response="blocked", session_id="s", model_used="haiku",
                       duration_ms=1, permission_denials=[PermissionDenial(tool_name="Bash")])
        self.assertIn("`Bash(git -C * log*)`", S._fmt_send_result(r))
        R.update_pair(self.pair, allowed_tools=None)
        self.assertNotIn("git -C", S._fmt_send_result(r))


if __name__ == "__main__":
    unittest.main()
