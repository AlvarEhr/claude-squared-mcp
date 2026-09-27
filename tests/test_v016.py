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
from claude_squared.models import AsyncTaskState, CreateResult, PairSpec, PermissionDenial, SendResult  # noqa: E402
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
        self.feed(rt, {"type": "system", "subtype": "init"})  # a fresh runtime's: not a wake
        self.assertIsNone(self.snap()["wake_pending"])
        self.assertIsNone(rt._wake_at)
        rt._result_seq = 1  # this runtime has finished a turn
        self.feed(rt, {"type": "system", "subtype": "init"})
        self.assertIsNotNone(self.snap()["wake_pending"])
        self.assertIsNotNone(rt._wake_at)
        self.feed(rt, assistant("hi"))
        self.assertIsNone(self.snap()["wake_pending"])
        self.assertIsNone(rt._wake_at)

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
    def test_evictor_never_kills_background_and_marks_it_stale_at_the_ceiling(self):
        # Persistent or not: past 24 h the work is flagged stale ("unknown"),
        # never killed (critic, 2026-09-25 — a timeout doesn't end work).
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
        self.assertFalse(self.snap()["background"][0].get("stale"))
        rt.last_activity = datetime.utcnow() - timedelta(hours=25)
        reg._evict_idle()
        self.assertIn(self.pair, reg._runtimes)
        self.assertEqual(reasons, [])
        entry = self.snap()["background"][0]
        self.assertEqual((entry["task_id"], entry["stale"]), ("bg", True))
        self.assertIn("may already have finished", entry["stale_reason"])
        self.assertEqual(self.snap()["background_lost"], [])

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
        own = self.pairs / "activity" / self.pair / f"{os.getpid()}.json"
        data = json.loads(own.read_text(encoding="utf-8"))  # the tombstone
        self.assertEqual(data["state"], "forgotten")
        self.assertEqual([b["task_id"] for b in data["background_lost"]], ["bgP"])

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


def wait_module():
    """The installed wait.py, loaded in-process so a test can shorten its
    timing constants (the source is the one wait.py gets)."""
    import types
    mod = types.ModuleType("wait_inproc")
    exec(compile(W.WAIT_SCRIPT_SOURCE, "wait.py", "exec"), mod.__dict__)
    return mod


class ReviewHelpers(V016Case):
    """Helpers shared by the review regression tests (no tests of its own)."""

    def run_wait(self, mod, *argv):
        """mod.main(argv) on a thread with captured output. Returns (thread, box)."""
        from contextlib import redirect_stderr, redirect_stdout
        box = {"out": io.StringIO(), "err": io.StringIO()}

        def go():
            with redirect_stdout(box["out"]), redirect_stderr(box["err"]):
                box["code"] = mod.main(list(argv))
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t, box

    def open_solicited(self, rt, task_id="t-send"):
        with patch.object(AT, "mark_task_executing", lambda tid: None):
            rt._prepare_solicited_turn(5, None, task_id)

    def transcript(self, rt, n=2):
        """A minimal session JSONL with n genuine user turns (rewind fixture)."""
        path = ClaudeAdapter().transcript_path(rt.spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(n):
            lines.append({"type": "user", "message": {"role": "user", "content": f"ask {i}"}})
            lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": f"ans {i}"}]}})
        path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        return path

    def foreign_self_woken_task(self, pid):
        tid = str(uuid.uuid4())
        (self.pairs / "async").mkdir(parents=True, exist_ok=True)
        (self.pairs / "async" / f"{tid}.json").write_text(json.dumps({
            "task_id": tid, "pair_name": self.pair, "message": AT.SELF_WOKEN_MESSAGE,
            "status": "running", "started_at": datetime.utcnow().isoformat(),
            "owner_pid": pid}), encoding="utf-8")
        return tid


class ReviewFixTests(ReviewHelpers):
    """Regression tests for the anvil cross-review of 19dcd73 (2026-09-25)."""

    # -- HIGH 1: force never bypasses a foreign owner --

    def test_force_refuses_under_a_foreign_owner(self):
        rt = self.live_runtime()
        self.transcript(rt)
        self.foreign_activity(self.live.pid, self.live_started, background=[
            {"task_id": "fbg", "kind": "local_bash", "description": "", "started_at": A._av_iso(A._av_now())}])
        calls = [lambda: tool(S.pair_clear)(self.pair, force=True),
                 lambda: tool(S.pair_rewind)(self.pair, to_point=2, force=True),
                 lambda: tool(S.pair_compact)(self.pair, force=True),
                 lambda: tool(S.pair_update)(self.pair, permission_mode="read-only", force=True)]
        for call in calls:
            with self.assertRaises(PairError) as cm:
                call()
            msg = str(cm.exception)
            self.assertIn(f"another MCP process (PID {self.live.pid}", msg)
            self.assertIn("force=True can't help", msg)
        self.assertIs(RT.registry().get_or_none(self.pair), rt)  # nothing was killed here
        self.assertEqual(len(self.transcript(rt).read_text(encoding="utf-8").splitlines()), 4)
        out = tool(S.pair_stop)(self.pair, hard=True)
        self.assertIn("hard reset skipped", out)
        self.assertNotIn("rotated session", out)

    # -- HIGH 2: a pending wake / open self-woken turn is protected --

    def test_wake_and_self_woken_turn_block_mutations(self):
        rt = self.live_runtime()
        self.transcript(rt)
        self.feed(rt, task_started("bgW"))
        self.feed(rt, task_done("bgW"))  # no turn open: the wake-up is imminent
        self.assertEqual(rt.pending_background_tasks(), [])
        self.assertEqual([w["kind"] for w in rt.pending_work()], ["wake-up"])
        for call in (lambda: tool(S.pair_clear)(self.pair),
                     lambda: tool(S.pair_rewind)(self.pair, to_point=2),
                     lambda: tool(S.pair_compact)(self.pair),
                     lambda: tool(S.pair_update)(self.pair, permission_mode="read-only")):
            with self.assertRaises(PairError) as cm:
                call()
            self.assertIn("wake-up", str(cm.exception))
        self.feed(rt, {"type": "system", "subtype": "init"})
        self.feed(rt, assistant("resuming"))  # the self-woken turn is open now
        self.assertEqual([w["kind"] for w in rt.pending_work()], ["self-woken turn"])
        with self.assertRaises(PairError) as cm:
            tool(S.pair_clear)(self.pair)
        self.assertIn("self-woken turn", str(cm.exception))
        self.feed(rt, result("resumed"))
        self.assertEqual(rt.pending_work(), [])

    def test_foreign_wake_or_self_woken_turn_block_mutations_and_sends(self):
        self.runtime()
        self.foreign_activity(self.live.pid, self.live_started, wake_pending={
            "since": A._av_iso(A._av_now()), "task_id": "b",
            "until": A._av_iso(A._av_now() + timedelta(seconds=60))})
        self.assertIn("self-wake pending", S._foreign_activity_blocker(self.pair))
        with self.assertRaises(PairError) as cm:
            tool(S.pair_clear)(self.pair, force=True)
        self.assertIn("wake-up", str(cm.exception))
        self.foreign_activity(self.live.pid, self.live_started, turn={
            "task_id": None, "kind": "self_woken", "started_at": A._av_iso(A._av_now())})
        self.assertIn("self-woken turn running", S._foreign_activity_blocker(self.pair))
        with self.assertRaises(PairError) as cm:
            tool(S.pair_compact)(self.pair, force=True)
        self.assertIn("self-woken turn", str(cm.exception))

    # -- HIGH 3: a send never steals a self-wake's reply --

    def test_send_waits_for_an_announced_wake(self):
        rt = self.live_runtime()
        rt._result_seq = 1  # a turn has finished in this runtime
        self.feed(rt, {"type": "system", "subtype": "init"})  # a queued turn starts, no scope open
        box = {}

        def promote():
            with patch.object(AT, "mark_task_executing", lambda tid: None):
                box["waited"] = rt._prepare_solicited_turn(30, None, "t-send")
        t = threading.Thread(target=promote, daemon=True)
        t.start()
        time.sleep(0.8)
        self.assertIsNone(rt._current_scope)  # held back: the wake owns the next result
        self.feed(rt, assistant("continuing on my own"))
        self.feed(rt, result("self-woken reply"))
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(rt._current_scope.task_id, "t-send")
        rec = R.get_pair(self.pair).self_woken_pending[-1]
        self.assertEqual((rec["status"], rec["response_preview"]), ("done", "self-woken reply"))
        # The reply went to the self-woken task, not to the send.
        self.assertTrue(rt._stdout_q.empty())

    def test_startup_init_and_expiry_do_not_hold_a_send(self):
        rt = self.live_runtime()
        self.feed(rt, {"type": "system", "subtype": "init"})  # fresh runtime: not a wake
        t0 = time.monotonic()
        self.open_solicited(rt)
        self.assertLess(time.monotonic() - t0, 1.0)
        rt._current_scope = None
        rt._result_seq = 1
        with patch.object(A, "WAKE_PENDING_SECONDS", 0.6):
            self.feed(rt, {"type": "system", "subtype": "init"})
            time.sleep(0.4)
            self.feed(rt, {"type": "system", "subtype": "thinking_tokens"})  # still coming
            time.sleep(0.4)
            self.assertTrue(rt.pending_work())  # refreshed: 0.8 s after the init, still live
            t0 = time.monotonic()
            self.open_solicited(rt, "t2")
            self.assertLess(time.monotonic() - t0, 3.0)  # released at expiry
        self.assertEqual(rt._current_scope.task_id, "t2")

    # -- HIGH 4: an unchanged pair_update neither refuses nor evicts --

    def test_unchanged_update_does_not_evict(self):
        rt = self.live_runtime()
        self.feed(rt, task_started("bgU"))
        current = R.get_pair(self.pair)
        tool(S.pair_update)(self.pair, permission_mode=current.permission_mode, model=current.model)
        self.assertIs(RT.registry().get_or_none(self.pair), rt)
        self.assertEqual([b["task_id"] for b in rt.pending_background_tasks()], ["bgU"])
        self.assertEqual(self.snap()["background_lost"], [])

    # -- HIGH 5 + suspected: the phantom guard --

    def test_phantom_during_a_send_closes_nothing(self):
        rt = self.runtime()
        self.open_solicited(rt)
        for ev in (STALE_NOTICE, STALE_INIT, STALE_RESULT):
            self.feed(rt, ev)
        snap = self.snap()
        self.assertEqual(snap["turn"]["task_id"], "t-send")  # still open
        self.assertEqual(snap["window_turns"], [])
        self.assertIsNone(rt._current_scope.result_snapshot)

    def test_result_without_scope_never_closes_another_turn(self):
        rt = self.runtime()
        A.writer(self.pair).turn_opened("one-shot", "send")  # a per-call override's turn
        self.feed(rt, result("", terminal=None))
        self.assertEqual(self.snap()["turn"]["task_id"], "one-shot")

    def test_phantom_guard_only_matches_the_measured_shape(self):
        rt = self.runtime()
        # An immediate API error before any assistant event is a real reply.
        for n, ev in enumerate((
                dict(STALE_RESULT, is_error=True, result="API Error: 529 overloaded"),
                dict(STALE_RESULT, result="short but real"),
                dict(STALE_RESULT, subtype="error_during_execution"),
                dict(STALE_RESULT, duration_ms=4000)), start=1):
            rt._current_scope = None
            self.open_solicited(rt, f"t{n}")
            self.feed(rt, STALE_NOTICE)
            self.assertIsNot(self.feed(rt, ev), False)
            self.assertEqual(rt._result_seq, n)
        # Only a "stopped" notification arms it.
        rt._current_scope = None
        self.feed(rt, dict(STALE_NOTICE, status="completed", task_id="other"))
        self.assertIsNone(rt._stale_notice)

    # -- HIGH 6: the triage wait revalidates --

    def test_triage_wait_does_not_return_idle_over_new_work(self):
        mod = wait_module()
        mod.PAIR_GRACE_S, mod.TRIAGE_WAIT_S = 0.3, 2.0
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        w = A.writer(self.pair)
        w.turn_opened("sw", "self_woken")
        w.turn_closed("sw", triage_pending=True)
        self.assertTrue(w.flush())
        time.sleep(0.5)
        t, box = self.run_wait(mod, self.pair, "--poll", "0.5", "--timeout", "30")
        time.sleep(0.6)  # the watcher is inside the triage wait
        w.turn_opened("t2", "send")
        self.assertTrue(w.flush())
        t.join(3.5)  # past the 2 s triage wait
        self.assertTrue(t.is_alive(), box["out"].getvalue())
        w.turn_closed("t2", preview="second")
        self.assertTrue(w.flush())
        t.join(15)
        self.assertEqual(box.get("code"), 0, box["out"].getvalue() + box["err"].getvalue())
        self.assertIn("t2", box["out"].getvalue())

    def test_timeout_while_settling_names_the_verdict(self):
        mod = wait_module()
        mod.PAIR_GRACE_S = 60.0  # never quiet long enough within the timeout
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        w.turn_closed("t1")
        self.assertTrue(w.flush())
        t, box = self.run_wait(mod, self.pair, "--poll", "0.5", "--timeout", "1")
        t.join(15)
        self.assertEqual(box.get("code"), 3, box["err"].getvalue())
        self.assertIn("still working (settling)", box["err"].getvalue())

    # -- HIGH 7: task mode judges a detached child by its own creation time --

    def test_task_mode_child_and_owner_identity(self):
        if self.live_started is None:
            self.skipTest("no process creation times on this platform")
        mod = wait_module()
        (self.pairs / "async").mkdir(parents=True, exist_ok=True)

        def task(**fields):
            tid = str(uuid.uuid4())
            data = {"task_id": tid, "pair_name": self.pair, "message": "m", "status": "running"}
            data.update(fields)
            (self.pairs / "async" / f"{tid}.json").write_text(json.dumps(data), encoding="utf-8")
            return tid
        events = self.home / "q.events.jsonl"
        events.write_text("{}\n", encoding="utf-8")
        old = time.time() - 60
        os.utime(events, (old, old))
        long_ago = datetime.utcfromtimestamp(self.live_started - 600).isoformat()
        # A queued task whose live child was spawned 10 min after the task started.
        tid = task(owner_pid=self.dead.pid, events_path=str(events), started_at=long_ago,
                   child_pid=self.live.pid, child_started_at=self.live_started)
        t, box = self.run_wait(mod, tid, "--poll", "0.5", "--timeout", "2")
        t.join(15)
        self.assertEqual(box.get("code"), 3, box["err"].getvalue())  # still running: keep waiting
        # An owner PID now held by a process created after the task started is not the owner.
        tid = task(owner_pid=self.live.pid, started_at=long_ago)
        t, box = self.run_wait(mod, tid, "--poll", "0.5", "--timeout", "5")
        t.join(15)
        self.assertEqual(box.get("code"), 4, box["err"].getvalue())

    # -- MEDIUM 8: a crashed (persistent) runtime --

    def test_reader_eof_records_lost_background(self):
        rt = self.runtime(persistent=True)
        self.feed(rt, task_started("bgE"))
        rt.proc = SimpleNamespace(stdout=io.BytesIO(b""), wait=lambda timeout=None: 3)
        rt._stdout_reader()  # the process exited: stdout at EOF
        self.assertEqual(rt.pending_background_tasks(), [])
        lost = self.snap()["background_lost"]
        self.assertEqual([b["task_id"] for b in lost], ["bgE"])
        self.assertIn("exit code 3", lost[0]["reason"])
        self.assertEqual(R.get_pair(self.pair).self_woken_pending[-1]["status"], "background_lost")

    def test_evictor_handles_dead_and_stale_persistent_runtimes(self):
        reg = RT.RuntimeRegistry(idle_timeout_seconds=1)
        rt = self.runtime(persistent=True)
        rt.proc = _AliveProc()
        rt.stop = lambda reason=None: "stopped"
        self.feed(rt, task_started("bgS"))
        rt.last_activity = datetime.utcnow() - timedelta(hours=25)
        reg._runtimes[self.pair] = rt
        reg._evict_idle()
        self.assertIn(self.pair, reg._runtimes)  # persistent: kept warm ...
        # ... and still protected: flagged stale, never dropped (anvil
        # re-review, 2026-09-25 — the work may be real and wake the pair).
        self.assertEqual([b["task_id"] for b in rt.pending_background_tasks()], ["bgS"])
        self.assertIn("stale", rt.pending_work()[0]["description"])
        entry = self.snap()["background"][0]
        self.assertEqual((entry["task_id"], entry["stale"]), ("bgS", True))
        self.assertEqual(self.snap()["background_lost"], [])
        RT.registry()._runtimes[self.pair] = rt  # the tools consult the process registry
        with self.assertRaises(PairError) as cm:
            tool(S.pair_clear)(self.pair)
        self.assertIn("stale", str(cm.exception))
        view = A.view(self.pair, grace_seconds=0)
        self.assertEqual(view["verdict"], "unknown")
        self.assertIn("no completion event for 24 h", " ".join(view["reasons"]))
        reg._evict_idle()  # idempotent: nothing new flagged
        self.assertEqual(len(self.snap()["background"]), 1)
        rt.proc = SimpleNamespace(poll=lambda: 1, pid=0, returncode=1, stdin=None)
        reg._evict_idle()
        self.assertNotIn(self.pair, reg._runtimes)  # dead: evicted despite persistent

    # -- MEDIUM 9: losses reach a running watcher --

    def test_watcher_sees_losses_from_forced_compact_and_forget(self):
        mod = wait_module()
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        w = A.writer(self.pair)
        w.background_started("b1", "local_bash")
        self.assertTrue(w.flush())
        t, box = self.run_wait(mod, self.pair, "--poll", "0.5", "--timeout", "30")
        time.sleep(0.8)
        w.lose_background("pair_compact(force=True) replaced the runtime")
        w.turn_opened("c1", "compact")  # the new window must not hide the loss
        self.assertTrue(w.flush())
        t.join(15)
        self.assertEqual(box.get("code"), 9, box["out"].getvalue() + box["err"].getvalue())
        self.assertIn("pair_compact(force=True)", box["err"].getvalue())
        w.turn_closed("c1")
        w.background_started("b2", "local_agent")
        self.assertTrue(w.flush())
        t, box = self.run_wait(mod, self.pair, "--poll", "0.5", "--timeout", "30")
        time.sleep(0.8)
        A.forget(self.pair)
        t.join(15)
        self.assertEqual(box.get("code"), 9, box["out"].getvalue() + box["err"].getvalue())
        self.assertIn("pair_forget", box["err"].getvalue())

    def test_watcher_sees_a_dead_owners_background_as_lost(self):
        mod = wait_module()
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (victim.poll() is None and victim.kill(), victim.wait(10)))
        end = time.monotonic() + 10
        while A._av_proc(victim.pid)[1] is None and time.monotonic() < end:
            time.sleep(0.05)
        self.foreign_activity(victim.pid, A._av_proc(victim.pid)[1], background=[
            {"task_id": "vbg", "kind": "local_bash", "description": "",
             "started_at": A._av_iso(A._av_now() - timedelta(minutes=5))}],
            updated_at=A._av_iso(A._av_now() - timedelta(minutes=5)))
        t, box = self.run_wait(mod, self.pair, "--poll", "0.5", "--timeout", "30")
        time.sleep(0.8)
        victim.kill()  # our own child, by PID
        victim.wait(10)
        t.join(15)
        self.assertEqual(box.get("code"), 9, box["out"].getvalue() + box["err"].getvalue())
        self.assertIn("vbg", box["err"].getvalue())

    # -- MEDIUM 10: the summary shows the triage verdict that landed later --

    def test_summary_uses_the_latest_triage_verdict(self):
        mod = wait_module()
        mod.PAIR_GRACE_S = 0.3
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        w = A.writer(self.pair)
        w.background_started("b2", "local_bash")
        # Published BEFORE the watcher starts: on a slow CI runner the watcher's
        # first look otherwise saw no activity and returned idle at once
        # (every CI job, 2026-09-27; in real use a send's task is already
        # running before any watcher starts).
        self.assertTrue(w.flush())
        t, box = self.run_wait(mod, self.pair, "--poll", "0.5", "--timeout", "30")
        time.sleep(0.5)
        w.turn_opened("sw", "self_woken")
        w.turn_closed("sw", preview="should I delete the old files?", triage_pending=True)
        self.assertTrue(w.flush())
        time.sleep(1.2)  # the watcher has cached "sw" with its triage pending
        w.set_triage("sw", {"importance": 3, "needs_attention": True, "source": "cli_summary",
                            "summary": "asks whether to delete the old files"})
        w.background_finished("b2")
        w.turn_opened("sw2", "self_woken")
        w.turn_closed("sw2", preview="all done")
        self.assertTrue(w.flush())
        t.join(15)
        out = box["out"].getvalue()
        self.assertEqual(box.get("code"), 0, out + box["err"].getvalue())
        self.assertIn("flagged for attention (1)", out)
        self.assertIn("asks whether to delete the old files", out)
        self.assertNotIn("triage pending", out)

    # -- HIGH 11: one deadline for a foreign self-woken turn --

    def test_foreign_self_woken_wait_is_bounded_without_a_hard_timeout(self):
        self.runtime()
        self.foreign_self_woken_task(self.live.pid)
        box = {}

        def go():
            try:
                S._wait_for_foreign_self_woken(self.pair, None, None)
            except PairError as e:
                box["err"] = str(e)
        with patch.object(S, "_FOREIGN_BACKGROUND_WAIT_S", 1.0):
            t = threading.Thread(target=go, daemon=True)
            t.start()
            t.join(10)
        self.assertFalse(t.is_alive())
        self.assertIn("mid self-woken turn in another MCP process", box.get("err", ""))

    # -- MEDIUM 12: unknown is not idle --

    def test_unknown_exits_10_not_0(self):
        mod = wait_module()
        mod.PAIR_GRACE_S = 0.3
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        (self.pairs / "async").mkdir(parents=True, exist_ok=True)
        tid = str(uuid.uuid4())
        (self.pairs / "async" / f"{tid}.json").write_text(json.dumps({
            "task_id": tid, "pair_name": self.pair, "message": "m", "status": "done",
            "started_at": (datetime.utcnow() - timedelta(seconds=2)).isoformat(),
            "finished_at": (datetime.utcnow() - timedelta(seconds=1)).isoformat(),
            "owner_pid": self.live.pid}), encoding="utf-8")  # a live owner we can't see into
        t, box = self.run_wait(mod, self.pair, "--poll", "0.5", "--timeout", "30")
        t.join(15)
        self.assertEqual(box.get("code"), 10, box["out"].getvalue() + box["err"].getvalue())
        self.assertIn(f"activity unknown for PID {self.live.pid}", box["err"].getvalue())
        self.assertIn("part of it is invisible", box["out"].getvalue())

    # -- MEDIUM 13: redirected non-UTF-8 output --

    def test_redirected_cp1252_output_keeps_the_exit_code(self):
        wait_py = W.install_wait_script(self.pairs / "wait.py")
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        w.note_context(400_000, 1_000_000, window_source="reported")
        self.assertTrue(w.flush())
        env = dict(os.environ, CLAUDE_HOME=str(self.home), PYTHONIOENCODING="cp1252:strict")
        proc = subprocess.Popen([sys.executable, str(wait_py), self.pair, "--each-turn",
                                 "--poll", "0.5", "--timeout", "30"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        time.sleep(1.5)
        w.turn_closed("t1", preview="done 🎉 → next: ✅")
        self.assertTrue(w.flush())
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, (out + err).decode("utf-8", "replace"))
        self.assertIn("🎉", out.decode("utf-8"))
        # The context alert path, same encoding.
        w.turn_opened("t2", "send")
        self.assertTrue(w.flush())
        proc = subprocess.Popen([sys.executable, str(wait_py), self.pair,
                                 "--poll", "0.5", "--timeout", "30"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        time.sleep(1.5)
        w.note_context(830_000, 1_000_000, window_source="reported")
        self.assertTrue(w.flush())
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 8, (out + err).decode("utf-8", "replace"))
        self.assertIn("CONTEXT ALERT", out.decode("utf-8"))


class ReReviewFixTests(ReviewHelpers):
    """Regression tests for the anvil re-review of 93289a1 (2026-09-25)."""

    def spec(self):
        spec = PairSpec(name=self.pair, session_id=str(uuid.uuid4()), cwd=str(self.home))
        R.add_pair(spec)
        return spec

    # -- 1: the hard reset re-checks foreign work under the pair lock --

    def test_hard_reset_rechecks_foreign_work_under_the_lock(self):
        rt = self.live_runtime()
        calls = []
        real = S._pending_work_split

        def split(name):
            calls.append(name)
            local, _foreign = real(name)
            if len(calls) == 1:
                return local, []  # nothing foreign yet at the unlocked check
            return local, [{"kind": "local_bash", "task_id": "fbg", "pid": 4242}]
        with patch.object(S, "_pending_work_split", split), \
                patch.object(ClaudeAdapter, "create", side_effect=AssertionError("rotated")):
            out = tool(S.pair_stop)(self.pair, hard=True)
        self.assertEqual(len(calls), 2)
        self.assertIn("hard reset skipped", out)
        self.assertNotIn("rotated session", out)
        self.assertIs(RT.registry().get_or_none(self.pair), rt)

    # -- 2+S2: a wake past its deadline limits waiting, not protection --

    def test_expired_wake_limits_waiting_not_protection(self):
        rt = self.live_runtime()
        rt._result_seq = 1
        with patch.object(A, "WAKE_PENDING_SECONDS", 0.3):
            self.feed(rt, {"type": "system", "subtype": "init"})
            time.sleep(0.5)
            work = rt.pending_work()
            self.assertEqual(work[0]["kind"], "wake-up")
            self.assertIn("unconfirmed", work[0]["description"])
            with self.assertRaises(PairError) as cm:
                tool(S.pair_clear)(self.pair)
            self.assertIn("wake-up", str(cm.exception))
            self.assertEqual(A.view(self.pair, grace_seconds=0)["verdict"], "unknown")
            reg = RT.RuntimeRegistry(idle_timeout_seconds=1)
            reg._runtimes[self.pair] = rt
            rt.last_activity = datetime.utcnow() - timedelta(hours=2)
            reg._evict_idle()
            self.assertIn(self.pair, reg._runtimes)  # still protected from idle eviction
            t0 = time.monotonic()
            self.open_solicited(rt)  # waiting is what the deadline limits
            self.assertLess(time.monotonic() - t0, 1.0)
        self.feed(rt, assistant("reply"))
        self.feed(rt, result("reply"))  # evidence: a result resolves it
        rt._current_scope = None
        self.assertEqual(rt.pending_work(), [])

    def test_foreign_unresolved_work_protects_and_a_send_proceeds_with_a_note(self):
        self.runtime()
        past = A._av_now() - timedelta(hours=1)
        self.foreign_activity(self.live.pid, self.live_started, wake_pending={
            "since": A._av_iso(past - timedelta(minutes=2)), "task_id": "b", "until": A._av_iso(past)})
        view = A.view(self.pair, grace_seconds=0)
        self.assertEqual((view["verdict"], len(view["wake_stale"])), ("unknown", 1))
        with self.assertRaises(PairError) as cm:
            tool(S.pair_clear)(self.pair)
        self.assertIn("unconfirmed", str(cm.exception))
        self.assertIn("force=True", str(cm.exception))
        self.assertIsNone(S._foreign_activity_blocker(self.pair))
        notes = []
        with patch.object(S, "_FOREIGN_BACKGROUND_WAIT_S", 1.0):
            waited = S._wait_for_foreign_self_woken(self.pair, None, None, notes)
        self.assertGreaterEqual(waited, 0.9)  # waited its bound ...
        self.assertIn("never started", notes[0])  # ... then proceeded, saying so
        # Unresolved work is already past its own bound: a short grace, not the
        # full foreign wait (main-session review, 2026-09-25).
        notes = []
        with patch.object(S, "_FOREIGN_BACKGROUND_WAIT_S", 60.0), \
                patch.object(S, "_FOREIGN_UNRESOLVED_GRACE_S", 1.0):
            waited = S._wait_for_foreign_self_woken(self.pair, None, None, notes)
        self.assertLess(waited, 5.0)
        self.assertIn("never started", notes[0])
        self.foreign_activity(self.live.pid, self.live_started, background=[
            {"task_id": "old", "kind": "local_bash", "description": "", "stale": True,
             "started_at": A._av_iso(past - timedelta(days=1))}])
        self.assertIsNone(S._foreign_activity_blocker(self.pair))
        self.assertIn("no completion event for 24 h", S._foreign_unresolved(self.pair))
        self.assertIn("stale", S._describe_background(S._pending_work_split(self.pair)[1]))

    def test_unresolved_wake_exits_10(self):
        mod = wait_module()
        mod.PAIR_GRACE_S = 0.3
        self.spec()
        past = A._av_now() - timedelta(hours=1)
        self.foreign_activity(self.live.pid, self.live_started, wake_pending={
            "since": A._av_iso(past), "task_id": "b", "until": A._av_iso(past)})
        t, box = self.run_wait(mod, self.pair, "--poll", "0.5", "--timeout", "30")
        t.join(15)
        self.assertEqual(box.get("code"), 10, box["out"].getvalue() + box["err"].getvalue())
        self.assertIn("never started", box["err"].getvalue())

    # -- 3: a wake published during promotion is caught at the commit --

    def test_wake_published_during_promotion_is_revalidated(self):
        rt = self.live_runtime()
        self.feed(rt, task_started("bgR"))  # background out, no turn open
        calls = []

        def promote(tid):
            calls.append(tid)
            self.feed(rt, task_done("bgR"))  # the reader publishes the wake mid-promotion
        box = {}

        def go():
            with patch.object(AT, "mark_task_executing", promote):
                box["waited"] = rt._prepare_solicited_turn(30, None, "t-send")
        th = threading.Thread(target=go, daemon=True)
        th.start()
        time.sleep(0.8)
        self.assertEqual(calls, ["t-send"])
        self.assertIsNone(rt._current_scope)  # not committed over the wake
        self.feed(rt, {"type": "system", "subtype": "init"})
        self.feed(rt, assistant("bgR finished"))
        self.feed(rt, result("bgR finished"))
        th.join(10)
        self.assertFalse(th.is_alive())
        self.assertEqual(rt._current_scope.task_id, "t-send")
        # promoted, re-queued while the wake ran (round 4), promoted again
        self.assertEqual(calls, ["t-send", "t-send"])
        self.assertEqual(R.get_pair(self.pair).self_woken_pending[-1]["response_preview"], "bgR finished")

    # -- 9: a recreated pair keeps what its forget killed --

    def test_recreated_pair_does_not_hide_a_forget_loss_from_a_watcher(self):
        mod = wait_module()
        self.spec()
        w = A.writer(self.pair)
        w.background_started("b1", "local_bash")
        self.assertTrue(w.flush())
        t, box = self.run_wait(mod, self.pair, "--poll", "2", "--timeout", "30")
        time.sleep(0.5)  # the watcher polled once and sleeps 2 s
        A.forget(self.pair)
        self.assertTrue(A.writer(self.pair).flush())  # recreated: same file name, tombstone gone
        t.join(15)
        self.assertEqual(box.get("code"), 9, box["out"].getvalue() + box["err"].getvalue())
        self.assertIn("b1", box["err"].getvalue())

    # -- 13: tasks carry the publication watermark --

    def test_tasks_are_stamped_with_the_publication_watermark(self):
        rt = self.runtime()  # registers the pair
        w = A.writer(self.pair)

        def runner(tid):
            w.background_started("b1", "local_bash")
            return SendResult(name=self.pair, response="ok", session_id="s", model_used="haiku",
                              duration_ms=1)
        st = AT.start_task(self.pair, "m", runner)
        AT.wait_for_task(st.task_id, 10)
        self.assertEqual(AT.load_task(st.task_id).activity_seq, w.mutation_seq())
        self.feed(rt, assistant("on my own"))
        tid = rt._implicit_task.task_id
        self.feed(rt, result("done"))
        stamped = AT.load_task(tid).activity_seq
        self.assertIsInstance(stamped, int)
        self.assertLessEqual(stamped, w.mutation_seq())

    # -- A: a stopped runtime's late reader can't touch its replacement --

    def test_late_reader_cleanup_cannot_touch_the_replacement(self):
        rt1 = self.live_runtime()
        rt1._reader_gen = 1  # as start() would have
        self.feed(rt1, task_started("old"))
        rt1.stop("replaced by a new runtime")
        rt2 = PairRuntime(rt1.spec, ClaudeAdapter())
        rt2.proc = _AliveProc()
        RT.registry()._runtimes[self.pair] = rt2
        self.feed(rt2, task_started("new"))
        # The old reader wakes up late: buffered output, then EOF.
        rt1.proc = SimpleNamespace(stdout=io.BytesIO(json.dumps(assistant("late")).encode() + b"\n"),
                                   wait=lambda timeout=None: 0)
        rt1._stdout_reader(1)
        self.assertIsNone(rt1._implicit_scope)  # retired: opened nothing
        snap = self.snap()
        self.assertEqual([b["task_id"] for b in snap["background"]], ["new"])
        self.assertEqual([b["task_id"] for b in snap["background_lost"]], ["old"])
        self.assertEqual(rt1._record_lost_background("late cleanup"), [])  # generation-scoped
        self.assertEqual([b["task_id"] for b in rt2.pending_background_tasks()], ["new"])
        self.assertEqual([b["task_id"] for b in self.snap()["background"]], ["new"])

    # -- C: --each-turn sees a turn that ends during the triage wait --

    def test_each_turn_sees_a_turn_that_ends_during_the_triage_wait(self):
        mod = wait_module()
        mod.PAIR_GRACE_S, mod.TRIAGE_WAIT_S = 0.3, 5.0
        self.spec()
        w = A.writer(self.pair)
        w.turn_opened("sw", "self_woken")
        w.turn_closed("sw", triage_pending=True)
        self.assertTrue(w.flush())
        time.sleep(0.5)
        t, box = self.run_wait(mod, self.pair, "--each-turn", "--poll", "0.5", "--timeout", "30")
        time.sleep(1.0)  # inside the triage wait
        w.turn_opened("t2", "send")
        w.turn_closed("t2", preview="second")
        self.assertTrue(w.flush())
        t.join(15)
        out = box["out"].getvalue()
        self.assertEqual(box.get("code"), 0, out + box["err"].getvalue())
        self.assertIn("turn ended", out)
        self.assertIn("t2", out)


class RoundFourFixTests(ReviewHelpers):
    """Regression tests for the critic review of round 3 (2026-09-25)."""

    def safe_runtime(self, spec):
        """Another PairRuntime of an already-registered pair, alive and safe to stop."""
        rt = PairRuntime(spec, ClaudeAdapter())
        rt.proc = _AliveProc()
        real_stop = rt.stop

        def safe_stop(reason=None):
            rt.proc = None
            return real_stop(reason)
        rt.stop = safe_stop
        return rt

    # -- 1: one exit handler — runtimes record their losses before activity closes --

    def test_one_exit_handler_runs_the_runtimes_first(self):
        from claude_squared.adapters import codex as C
        calls = []
        with patch.object(RT, "_EXIT_HOOKED", [False]), \
                patch.object(RT.atexit, "register", lambda fn, *a, **kw: calls.append(fn)), \
                patch.object(S.mcp, "run", lambda *a, **kw: None):
            RT.install_exit_handler()  # the first runtime / registry()
            S.run()  # the server
            spec = PairSpec(name=self.pair + "-cx", session_id="thread-x", cwd=str(self.home))
            C._new_codex_activity(spec, None).close()  # a Codex turn (or a CLI recovery)
        self.assertEqual(calls, [RT._shutdown_runtimes])
        # And that handler: the runtime's own loss record comes first.
        rt = self.live_runtime()
        self.feed(rt, task_started("bgX"))
        reg = RT.RuntimeRegistry()
        reg._runtimes[self.pair] = rt
        with patch.object(RT, "_REGISTRY", reg):
            RT._shutdown_runtimes()
        rec = R.get_pair(self.pair).self_woken_pending[-1]
        self.assertEqual(rec["status"], "background_lost")
        self.assertIn("the owning MCP process exited", rec["note"])
        self.assertIn("BACKGROUND LOST", self.main_log(rt))

    # -- 2a: only a completion or a failure announces a wake --

    def test_only_completions_and_failures_announce_a_wake(self):
        rt = self.runtime()
        for status, wakes in (("killed", False), ("stopped", False), ("cancelled", False),
                              ("failed", True), ("completed", True)):
            tid = f"bg-{status}"
            self.feed(rt, task_started(tid))
            self.feed(rt, task_done(tid, status))
            self.assertEqual(rt._wake_at is not None, wakes, status)
            self.assertEqual(self.snap()["wake_pending"] is not None, wakes, status)
            rt._clear_wake()

    # -- 2b: force overrides another process's UNCONFIRMED work only --

    def test_force_overrides_only_unconfirmed_foreign_work(self):
        self.live_runtime()
        past = A._av_now() - timedelta(hours=1)
        self.foreign_activity(self.live.pid, self.live_started, wake_pending={
            "since": A._av_iso(past), "task_id": "b", "until": A._av_iso(past)})
        with self.assertRaises(PairError) as cm:
            tool(S.pair_clear)(self.pair)
        self.assertIn("unconfirmed", str(cm.exception))
        with patch.object(ClaudeAdapter, "create", lambda self_, spec, initial_message=None:
                          CreateResult(name=spec.name, session_id=spec.session_id)):
            out = tool(S.pair_clear)(self.pair, force=True)
        self.assertNotIn("ERROR", out)
        self.foreign_activity(self.live.pid, self.live_started, background=[
            {"task_id": "real", "kind": "local_bash", "description": "",
             "started_at": A._av_iso(A._av_now())}])
        with self.assertRaises(PairError) as cm:
            tool(S.pair_clear)(self.pair, force=True)  # real foreign work: force can't help
        self.assertIn("force=True can't help", str(cm.exception))

    # -- 3: stop() settles an event that raced the reader's retirement --

    def test_stop_settles_a_reader_event_that_raced_it(self):
        rt = self.live_runtime()
        for label, line, slow_name in (
                ("background", task_started("late"), "_track_background"),
                ("self-woken turn", assistant("late turn"), "_open_implicit_turn")):
            if label == "self-woken turn":
                rt = self.safe_runtime(rt.spec)
            rt._reader_gen = 1
            entered, release = threading.Event(), threading.Event()
            real_slow = getattr(rt, slow_name)

            def slow(*a, _real=real_slow, _entered=entered, _release=release):
                _entered.set()
                _release.wait(5)
                return _real(*a)
            setattr(rt, slow_name, slow)
            rt.proc = SimpleNamespace(stdout=io.BytesIO(json.dumps(line).encode() + b"\n"),
                                      wait=lambda timeout=None: 0)
            th = threading.Thread(target=rt._stdout_reader, args=(1,), daemon=True)
            rt._reader_thread = th
            th.start()
            self.assertTrue(entered.wait(5), label)  # the event passed the generation check
            real_record = rt._record_lost_background

            def record_then_release(why, _real=real_record, _release=release):
                out = _real(why)
                _release.set()  # the late event completes right after stop()'s first sweep
                return out
            rt._record_lost_background = record_then_release
            rt.stop("replaced")
            self.assertFalse(th.is_alive(), label)
            self.assertIsNone(rt._implicit_scope, label)
            self.assertEqual(rt.pending_background_tasks(), [], label)
        self.assertNotIn("late", [b["task_id"] for b in self.snap()["background"]])
        self.assertIn("late", [b["task_id"] for b in self.snap()["background_lost"]])
        running = [t for t in AT.list_running_task_ids_for_pair(self.pair)]
        self.assertEqual(running, [])  # the late self-woken task was finalized, not left running
        self.assertIsNone(self.snap()["turn"])

    # -- 4: a promoted send that has to wait again reads queued --

    def test_a_send_that_waits_again_reads_queued(self):
        rt = self.live_runtime()
        tid = str(uuid.uuid4())
        AT._save(AsyncTaskState(task_id=tid, pair_name=self.pair, message="m", status="running",
                                started_at=datetime.utcnow(), owner_pid=os.getpid(), queued=True))
        self.feed(rt, task_started("bgQ"))
        real_mark = AT.mark_task_executing

        def promote(t):
            real_mark(t)
            self.feed(rt, task_done("bgQ"))  # the wake lands mid-promotion
        th = threading.Thread(target=lambda: rt._prepare_solicited_turn(30, None, tid), daemon=True)
        with patch.object(AT, "mark_task_executing", promote):
            th.start()
            time.sleep(0.8)
            st = AT.load_task(tid)
            self.assertIsNone(rt._current_scope)
            self.assertTrue(st.queued)  # waiting again: queued, not executing
            self.assertIsNone(st.execution_started_at)
            self.feed(rt, {"type": "system", "subtype": "init"})
            self.feed(rt, assistant("bgQ finished"))
            self.feed(rt, result("bgQ finished"))
            th.join(10)
        self.assertEqual(rt._current_scope.task_id, tid)
        self.assertFalse(AT.load_task(tid).queued)


class MiscTests(V016Case):
    def test_python_m_serves_through_server_run(self):
        # Every MCP host starts `python -m claude_squared` with no arguments;
        # that path must go through server.run() (activity beacon, dead-owner
        # sweep, exit handler), not mcp.run() — found in the real-MCP test,
        # 2026-09-27.
        from claude_squared import __main__ as M
        with patch.object(S, "run") as run, patch.object(S.mcp, "run") as bare:
            self.assertEqual(M._cmd_serve(), 0)
        run.assert_called_once_with()
        bare.assert_not_called()

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
