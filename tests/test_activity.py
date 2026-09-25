"""Offline tests for the v0.16 pair activity files (activity.py)."""

from __future__ import annotations

import __future__
import ast
import builtins
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
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
TEST_HOME = tempfile.TemporaryDirectory(prefix="cs-activity-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = TEST_HOME.name

from claude_squared import activity as A  # noqa: E402
from claude_squared import _wait_script as W  # noqa: E402

BEGIN, END = "# ---- BEGIN STDLIB ACTIVITY VIEW ----", "# ---- END STDLIB ACTIVITY VIEW ----"


def stdlib_block() -> str:
    src = Path(A.__file__).read_text(encoding="utf-8")
    start = src.index(BEGIN)
    return src[start:src.index(END, start) + len(END)]


def utc_naive_iso(dt: datetime) -> str:
    """How async task files store times (datetime.utcnow().isoformat())."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


class ActivityTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A live foreign process (a stand-in for another MCP server) and a dead
        # one whose Popen handle we deliberately keep open: OpenProcess still
        # succeeds on it, so only the exit-code check can call it dead.
        cls.live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        cls.dead = subprocess.Popen([sys.executable, "-c", "pass"])
        cls.dead.wait(30)
        deadline = time.monotonic() + 10
        while A._av_proc(cls.live.pid)[1] is None and time.monotonic() < deadline:
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
        self.pair = f"p-{uuid.uuid4().hex[:6]}"
        self.addCleanup(self.retire_writers)  # runs before the temp home is removed

    def retire_writers(self):
        """The shared flusher must not write into a test's home after teardown."""
        with A._WRITERS_LOCK:
            mine = [k for k in A._WRITERS if k[0] == str(self.pairs)]
            retired = [A._WRITERS.pop(k) for k in mine]
        for w in retired:
            with w._io_lock:
                with w._lock:
                    w._forgotten = True

    # -- helpers --

    def own_file(self, pair=None) -> Path:
        return self.pairs / "activity" / (pair or self.pair) / f"{os.getpid()}.json"

    def read_own(self) -> dict:
        return json.loads(self.own_file().read_text(encoding="utf-8"))

    def foreign(self, pid: int, **fields) -> Path:
        """Write a foreign owner's activity file for self.pair."""
        now = A._av_iso(A._av_now())
        data = {
            "version": 1, "pair": self.pair, "backend": "claude", "owner_pid": pid,
            "owner_started_at": fields.pop("owner_started_at", None), "state": "live",
            "created_at": now, "updated_at": now, "seq": 1, "last_transition_at": now,
            "window_started_at": None, "turn": None, "background": [], "wake_pending": None,
            "context": None, "window_turns": [], "background_finished": [],
            "background_lost": [], "compactions": [],
        }
        data.update(fields)
        path = self.pairs / "activity" / self.pair / f"{pid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def task(self, *, owner_pid, status="done", started=None, finished=None, **extra) -> str:
        tid = str(uuid.uuid4())
        # Recent by default: a task that started before its (live) owner
        # process was created reads as PID reuse, which setUpClass's child isn't.
        started = started or (A._av_now() - timedelta(seconds=1))
        data = {"task_id": tid, "pair_name": self.pair, "message": "hi", "status": status,
                "started_at": utc_naive_iso(started), "owner_pid": owner_pid}
        if finished is not None:
            data["finished_at"] = utc_naive_iso(finished)
        data.update(extra)
        d = self.pairs / "async"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.json").write_text(json.dumps(data), encoding="utf-8")
        return tid

    def beacon(self, pid: int, started) -> None:
        d = self.pairs / "activity_owners"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pid}.json").write_text(json.dumps({"version": 1, "pid": pid,
                                                   "owner_started_at": started}), encoding="utf-8")

    def view(self, **kw):
        return A.pair_activity_view(self.pairs, self.pair, **kw)

    def wait_for(self, pred, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.02)
        return pred()


class WriterTests(ActivityTestCase):
    def test_turn_lifecycle_window_and_interim(self):
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        w.background_started("b1", "local_bash", "sleep then write")
        entry = w.turn_closed("t1", preview="x" * 500)
        self.assertEqual((entry["task_id"], entry["kind"], entry["status"]), ("t1", "send", "done"))
        self.assertTrue(entry["interim"])
        self.assertEqual(entry["bg_pending_after"], 1)
        self.assertEqual(len(entry["preview"]), A.PREVIEW_CHARS)
        self.assertTrue(w.flush())
        data = self.read_own()
        self.assertEqual(data["owner_pid"], os.getpid())
        self.assertIsNotNone(data["owner_started_at"])
        self.assertEqual(data["window_task_id"], "t1")
        self.assertTrue(data["background"][0]["outlived_turn"])
        self.assertIsNone(data["turn"])
        # A self-woken turn continues the window; a new send starts a new one.
        w.turn_opened("t2", "self_woken")
        w.turn_closed("t2")
        self.assertEqual([e["task_id"] for e in w.snapshot()["window_turns"]], ["t1", "t2"])
        w.turn_opened("t3", "send")
        snap = w.snapshot()
        self.assertEqual(snap["window_turns"], [])
        self.assertEqual(snap["window_task_id"], "t3")
        self.assertEqual(snap["turn"]["task_id"], "t3")

    def test_turn_closed_is_idempotent_and_identity_checked(self):
        w = A.writer(self.pair)
        self.assertIsNone(w.turn_closed("nope"))
        w.turn_opened("t1", "send")
        self.assertIsNone(w.turn_closed("other"))
        self.assertIsNotNone(w.snapshot()["turn"])
        self.assertIsNotNone(w.turn_closed("t1"))
        self.assertIsNone(w.turn_closed("t1"))

    def test_clear_wake_resolves_a_wake_and_never_a_turn(self):
        w = A.writer(self.pair)
        w.expect_wake("init")
        self.assertIsNone(w.turn_closed(None))  # no turn open: the wake is not its business
        self.assertIsNotNone(w.snapshot()["wake_pending"])
        self.assertTrue(w.clear_wake())
        self.assertIsNone(w.snapshot()["wake_pending"])
        self.assertFalse(w.clear_wake())
        # A phantom result while a real send is open must not publish that
        # send as finished (anvil review, 2026-09-25).
        w.turn_opened("t-send", "send")
        self.assertFalse(w.clear_wake())
        snap = w.snapshot()
        self.assertEqual(snap["turn"]["task_id"], "t-send")
        self.assertEqual(snap["window_turns"], [])

    def test_refresh_wake_extends_only_an_existing_wake(self):
        w = A.writer(self.pair)
        w.refresh_wake()
        self.assertIsNone(w.snapshot()["wake_pending"])
        w.background_finished("b1")
        first = w.snapshot()["wake_pending"]
        time.sleep(0.01)
        w.refresh_wake()
        wake = w.snapshot()["wake_pending"]
        self.assertEqual((wake["since"], wake["task_id"]), (first["since"], "b1"))
        self.assertGreater(A._av_parse(wake["until"]), A._av_parse(first["until"]))

    def test_context_alert_threshold(self):
        self.assertEqual(A.compaction_point(1_000_000), 970_000)
        self.assertEqual(A.context_alert_threshold(1_000_000), 820_000)  # 15% headroom
        self.assertEqual(A.context_alert_threshold(200_000), 130_000)    # 40k floor
        self.assertEqual(A.context_alert_threshold(1_000_000, 50_000), 920_000)
        self.assertIsNone(A.context_alert_threshold(None))
        self.assertIsNone(A.compaction_point(0))

    def test_unclosed_turn_is_superseded_not_lost(self):
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        w.turn_opened("t2", "self_woken")
        turns = w.snapshot()["window_turns"]
        self.assertEqual((turns[-1]["task_id"], turns[-1]["status"]), ("t1", "superseded"))

    def test_wake_pending_only_when_no_turn_is_open(self):
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        w.background_started("fg", "local_agent")
        w.background_finished("fg")  # in-turn completion: delivered inside the turn
        self.assertIsNone(w.snapshot()["wake_pending"])
        w.background_started("b1", "local_bash")
        w.turn_closed("t1")
        w.background_finished("b1", "completed")
        wake = w.snapshot()["wake_pending"]
        self.assertEqual(wake["task_id"], "b1")
        span = A._av_parse(wake["until"]) - A._av_parse(wake["since"])
        self.assertEqual(span, timedelta(seconds=A.WAKE_PENDING_SECONDS))
        done = w.snapshot()["background_finished"][-1]
        self.assertEqual((done["task_id"], done["status"], done["outlived_turn"]), ("b1", "completed", True))
        w.turn_opened("sw", "self_woken")
        self.assertIsNone(w.snapshot()["wake_pending"])
        w.expect_wake()  # no-op while a turn is open
        self.assertIsNone(w.snapshot()["wake_pending"])
        w.turn_closed("sw")
        w.expect_wake("init")
        self.assertEqual(w.snapshot()["wake_pending"]["task_id"], "init")

    def test_lose_background_records_reason_and_lost_wake(self):
        w = A.writer(self.pair)
        w.background_started("b1", "local_bash", "one")
        w.background_started("b2", "local_agent", "two")
        w.background_finished("b0")  # unknown task, no turn open -> wake pending
        lost = w.lose_background("evicted", task_ids=["b2"])
        self.assertEqual([r["task_id"] for r in lost], ["b2"])
        self.assertIsNotNone(w.snapshot()["wake_pending"])  # partial loss keeps the wake
        lost = w.lose_background("runtime tree-killed")
        self.assertEqual([r["task_id"] for r in lost], ["b1", "b0"])
        self.assertEqual(lost[-1]["kind"], "wake-up")
        snap = w.snapshot()
        self.assertEqual(snap["background"], [])
        self.assertIsNone(snap["wake_pending"])
        self.assertEqual([r["reason"] for r in snap["background_lost"]],
                         ["evicted", "runtime tree-killed", "runtime tree-killed"])
        self.assertEqual(w.lose_background("again"), [])

    def test_background_cap_moves_overflow_to_lost(self):
        w = A.writer(self.pair)
        for i in range(A.BACKGROUND_CAP + 3):
            w.background_started(f"b{i}")
        snap = w.snapshot()
        self.assertEqual(len(snap["background"]), A.BACKGROUND_CAP)
        self.assertEqual([r["task_id"] for r in snap["background_lost"]], ["b0", "b1", "b2"])
        self.assertIn("no completion event seen", snap["background_lost"][0]["reason"])

    def test_triage_slot(self):
        w = A.writer(self.pair)
        w.turn_opened("sw", "self_woken")
        entry = w.turn_closed("sw", triage_pending=True)
        self.assertEqual(entry["triage"], {"state": "pending"})
        self.assertTrue(w.set_triage("sw", {"importance": 2, "needs_attention": True, "source": "haiku"}))
        tri = w.snapshot()["window_turns"][-1]["triage"]
        self.assertEqual((tri["state"], tri["importance"], tri["source"]), ("done", 2, "haiku"))
        self.assertFalse(w.set_triage("missing", {"importance": 0}))

    def test_context_compaction_and_reported_window(self):
        w = A.writer(self.pair)
        w.note_context(None, 1000)  # ignored
        self.assertIsNone(w.snapshot()["context"])
        w.note_context(960_000, 1_000_000, window_source="reported")
        ctx = w.snapshot()["context"]
        self.assertEqual((ctx["used"], ctx["pct"], ctx["source"]), (960_000, 96.0, "stream"))
        w.note_compaction("auto", 967_000, 61_000)
        snap = w.snapshot()
        self.assertEqual(snap["compactions"][-1]["trigger"], "auto")
        self.assertEqual((snap["context"]["used"], snap["context"]["source"]), (61_000, "compaction"))
        w.note_reported_window(1_000_000, model_key="opus|default", model="claude-opus-5-5")
        self.assertTrue(w.flush())
        rec = A.reported_window(self.pair, "opus|default")
        self.assertEqual((rec["tokens"], rec["model"]), (1_000_000, "claude-opus-5-5"))
        self.assertIsNone(A.reported_window(self.pair, "haiku|default"))
        self.assertEqual(A.reported_window(self.pair)["tokens"], 1_000_000)
        self.assertEqual(self.view()["reported_window"]["tokens"], 1_000_000)

    def test_writer_is_per_pair_and_home(self):
        self.assertIs(A.writer(self.pair), A.writer(self.pair))
        self.assertIsNot(A.writer(self.pair), A.writer(self.pair + "x"))
        self.assertEqual(A.writer(self.pair, "codex").snapshot()["backend"], "codex")

    def test_shutdown_records_losses_and_marks_exited(self):
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        w.background_started("b1", "local_bash")
        A.announce()
        beacon = self.pairs / "activity_owners" / f"{os.getpid()}.json"
        self.assertTrue(beacon.exists())
        A.shutdown("MCP exited")
        data = self.read_own()
        self.assertEqual((data["state"], data["closed_reason"]), ("exited", "MCP exited"))
        self.assertEqual([r["task_id"] for r in data["background_lost"]], ["b1"])
        self.assertEqual((data["window_turns"][-1]["status"], data["window_turns"][-1]["note"]),
                         ("aborted", "MCP exited"))
        self.assertFalse(beacon.exists())
        view = self.view()
        self.assertEqual(view["verdict"], "idle" if view["quiet_for_s"] >= 5 else "settling")
        self.assertEqual(view["owners"][0]["status"], "exited")
        self.assertEqual(view["background_lost"][0]["task_id"], "b1")

    def test_forget_leaves_a_tombstone_and_never_rewrites_it(self):
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        w.background_started("b1", "local_bash")
        w.note_reported_window(200_000, model_key="haiku|default")
        self.assertTrue(w.flush())
        self.assertTrue((self.pairs / "activity" / self.pair / "pair.json").exists())
        A.forget(self.pair)
        # The tombstone: a running wait.py must still see what the forget
        # killed (anvil review, 2026-09-25).
        data = self.read_own()
        self.assertEqual(data["state"], "forgotten")
        self.assertEqual([r["task_id"] for r in data["background_lost"]], ["b1"])
        self.assertEqual(data["window_turns"][-1]["status"], "aborted")
        self.assertIsNone(data["turn"])
        self.assertFalse((self.pairs / "activity" / self.pair / "pair.json").exists())
        seq = data["seq"]
        w.turn_closed("t1")  # a straggling reader-thread event after forget
        w.background_started("b2")
        time.sleep(0.3)
        self.assertEqual(self.read_own()["seq"], seq)
        view = self.view()
        self.assertNotEqual(view["verdict"], "busy")
        self.assertEqual(view["owners"][0]["status"], "forgotten")
        self.assertEqual([b["task_id"] for b in view["losses"]], ["b1"])
        self.assertIsNot(A.writer(self.pair), w)
        # A pair forgotten with no writer here leaves no directory behind.
        A.forget(self.pair + "-none")
        self.assertFalse((self.pairs / "activity" / (self.pair + "-none")).exists())

    def test_losses_survive_a_new_window(self):
        # pair_compact(force=True): evict (loss) THEN open the compact's window.
        w = A.writer(self.pair)
        w.background_started("b1", "local_bash")
        w.lose_background("pair_compact(force=True) replaced the runtime")
        time.sleep(0.01)
        w.turn_opened("c1", "compact")
        snap = w.snapshot()
        self.assertEqual([r["task_id"] for r in snap["background_lost"]], ["b1"])
        self.assertTrue(w.flush())
        view = self.view()
        self.assertEqual(view["background_lost"], [])  # the window list: since the compact
        self.assertEqual([r["task_id"] for r in view["losses"]], ["b1"])  # unfiltered


class FlushTests(ActivityTestCase):
    def test_mutators_never_wait_for_disk(self):
        """The runtime's reader thread calls the mutators: they must return while
        the flusher is stuck inside a slow write."""
        w = A.writer(self.pair)
        entered, release = threading.Event(), threading.Event()
        real = A._atomic_write

        def slow_write(path, text):
            entered.set()
            release.wait(10)
            real(path, text)

        with patch.object(A, "_atomic_write", slow_write):
            w.turn_opened("t1", "send")
            self.assertTrue(entered.wait(3), "flusher never started a write")
            t0 = time.monotonic()
            for i in range(200):
                w.background_started(f"b{i}")
                w.note_context(1000 + i, 200_000)
                w.background_finished(f"b{i}")
            w.turn_closed("t1")
            w2 = A.writer(self.pair + "-other")  # creating a writer does no I/O either
            w2.turn_opened("x", "send")
            elapsed = time.monotonic() - t0
            release.set()
        self.assertLess(elapsed, 1.0, f"mutators blocked for {elapsed:.2f}s")
        self.assertTrue(self.wait_for(lambda: self.own_file().exists()
                                      and self.read_own()["turn"] is None))

    def test_urgent_changes_flush_promptly_and_context_is_coalesced(self):
        w = A.writer(self.pair)
        writes: list[str] = []
        real = A._atomic_write

        def counting(path, text):
            if path.name == f"{os.getpid()}.json":
                writes.append(text)
            real(path, text)

        with patch.object(A, "_atomic_write", counting):
            w.turn_opened("t1", "send")
            self.assertTrue(self.wait_for(lambda: self.own_file().exists(), 1.0),
                            "urgent change not flushed within 1 s")
            time.sleep(1.2)
            base = len(writes)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.5:
                w.note_context(5000, 200_000)
                time.sleep(0.005)
            time.sleep(1.2)
            lazy_writes = len(writes) - base
        # ~1.5 s of continuous context updates -> about two writes, never hundreds.
        self.assertGreaterEqual(lazy_writes, 1)
        self.assertLessEqual(lazy_writes, 3)
        self.assertEqual(self.read_own()["context"]["used"], 5000)

    def test_failed_write_is_retried(self):
        w = A.writer(self.pair)
        calls = {"n": 0}
        real = A._atomic_write

        def flaky(path, text):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError("disk hiccup")
            real(path, text)

        with patch.object(A, "_atomic_write", flaky):
            w.turn_opened("t1", "send")
            self.assertTrue(self.wait_for(lambda: self.own_file().exists(), 5.0))
        self.assertEqual(self.read_own()["turn"]["task_id"], "t1")

    def test_atomic_write_retries_permission_error_like_save(self):
        target = self.home / "x" / "f.json"
        real_replace = Path.replace
        state = {"n": 0}

        def flaky_replace(self_path, dst):
            state["n"] += 1
            if state["n"] <= 2:
                raise PermissionError("sharing violation")
            return real_replace(self_path, dst)

        with patch.object(Path, "replace", flaky_replace):
            A._atomic_write(target, "{}")
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")
        state["n"] = -10
        with patch.object(Path, "replace", flaky_replace):
            with self.assertRaises(PermissionError):
                A._atomic_write(target, "[]")
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["f.json"])  # no tmp left


class ViewTests(ActivityTestCase):
    def test_two_owners_merge_instead_of_overwriting(self):
        w = A.writer(self.pair)
        w.background_started("mine", "local_bash")
        self.assertTrue(w.flush())
        self.foreign(self.live.pid, owner_started_at=self.live_started,
                     background=[{"task_id": "theirs", "kind": "local_agent", "description": "",
                                  "started_at": A._av_iso(A._av_now())}])
        view = self.view()
        self.assertEqual(view["verdict"], "busy")
        self.assertEqual(sorted(b["task_id"] for b in view["background"]), ["mine", "theirs"])
        self.assertEqual({b["pid"] for b in view["background"]}, {os.getpid(), self.live.pid})
        self.assertEqual(self.read_own()["background"][0]["task_id"], "mine")

    def test_dead_owner_is_reported_lost_not_busy_and_swept(self):
        at = A._av_iso(A._av_now())
        path = self.foreign(self.dead.pid, window_started_at=at, updated_at=at,
                            turn={"task_id": "t9", "kind": "self_woken", "started_at": at},
                            background=[{"task_id": "b9", "kind": "local_bash", "description": "d",
                                         "started_at": at}],
                            wake_pending={"since": at, "until": A._av_iso(A._av_now() + timedelta(minutes=2))})
        view = self.view()
        self.assertEqual(view["owners"][0]["status"], "died")
        self.assertNotEqual(view["verdict"], "busy", view["reasons"])
        self.assertEqual(view["background_lost"][0]["task_id"], "b9")
        self.assertTrue(view["background_lost"][0]["synthesized"])
        self.assertIn(f"(PID {self.dead.pid}) died", view["background_lost"][0]["reason"])
        self.assertEqual(view["window_turns"][-1]["status"], "owner_died")
        self.assertTrue(any("died" in n for n in view["notes"]))
        counts = A.sweep()
        self.assertEqual(counts["marked_dead"], 1)
        swept = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((swept["state"], swept["background"], swept["turn"]), ("dead", [], None))
        after = self.view()
        self.assertEqual(after["owners"][0]["status"], "dead")
        self.assertEqual([r["task_id"] for r in after["background_lost"]], ["b9"])
        self.assertEqual(A.sweep()["marked_dead"], 0)  # idempotent
        self.assertEqual(A.sweep(now=A._av_now() + timedelta(hours=25))["deleted"], 1)
        self.assertFalse(path.exists())

    def test_sweep_never_touches_live_owners_or_self(self):
        live_path = self.foreign(self.live.pid, owner_started_at=self.live_started,
                                 background=[{"task_id": "b", "kind": "k", "description": "",
                                              "started_at": A._av_iso(A._av_now())}])
        w = A.writer(self.pair)
        w.background_started("mine")
        self.assertTrue(w.flush())
        before = live_path.read_text(encoding="utf-8")
        counts = A.sweep(now=A._av_now() + timedelta(days=30))
        self.assertEqual(counts, {"marked_dead": 0, "deleted": 0, "beacons_removed": 0})
        self.assertEqual(live_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.read_own()["background"][0]["task_id"], "mine")

    def test_pid_reuse_guard(self):
        self.foreign(self.live.pid, owner_started_at=self.live_started - 3600,
                     background=[{"task_id": "b", "kind": "k", "description": "",
                                  "started_at": A._av_iso(A._av_now())}])
        view = self.view()
        self.assertEqual(view["owners"][0]["status"], "died")
        self.assertNotEqual(view["verdict"], "busy")

    def test_pre016_owner_makes_the_verdict_unknown(self):
        self.task(owner_pid=self.live.pid, finished=A._av_now())
        view = self.view()
        self.assertEqual(view["verdict"], "unknown")
        self.assertIn(f"activity unknown for PID {self.live.pid}", view["reasons"][0])
        self.assertIn("pre-0.16", view["reasons"][0])
        self.assertIsNotNone(view["quiet_for_s"])

    def test_beacon_without_a_pair_file_is_unknown_not_idle(self):
        # A beacon proves the capability, not this pair's visibility: an
        # owner whose first write here failed must not read as idle while
        # its background may be out (anvil review, 2026-09-25).
        self.task(owner_pid=self.live.pid, finished=A._av_now())
        self.beacon(self.live.pid, self.live_started)
        view = self.view()
        self.assertEqual(view["verdict"], "unknown")
        self.assertIn("no activity file for this pair", view["reasons"][0])
        self.foreign(self.live.pid, owner_started_at=self.live_started)
        self.assertIn(self.view()["verdict"], ("idle", "settling"))

    def test_a_new_writer_writes_its_file_at_once(self):
        A.touch(self.pair)
        self.assertTrue(self.wait_for(lambda: self.own_file().exists()))
        self.assertEqual(self.read_own()["state"], "live")

    def test_old_or_dead_task_owners_are_not_unknown(self):
        old = A._av_now() - timedelta(hours=30)
        self.task(owner_pid=self.live.pid, started=old, finished=old)
        self.task(owner_pid=self.dead.pid, finished=A._av_now())
        # A task that started BEFORE the live process was created can't be its.
        self.task(owner_pid=self.live.pid,
                  started=datetime.fromtimestamp(self.live_started - 600, timezone.utc),
                  finished=A._av_now())
        self.assertIn(self.view()["verdict"], ("idle", "settling"))

    def test_running_task_states(self):
        self.beacon(self.live.pid, self.live_started)
        tid = self.task(owner_pid=self.live.pid, status="running", queued=True)
        view = self.view()
        self.assertEqual(view["verdict"], "busy")
        self.assertEqual(view["running_tasks"][0]["state"], "queued")
        os.remove(self.pairs / "async" / f"{tid}.json")
        tid = self.task(owner_pid=self.dead.pid, status="running")
        view = self.view()
        self.assertNotEqual(view["verdict"], "busy")
        self.assertEqual(view["running_tasks"][0]["state"], "orphaned")
        self.assertIn(f"pair_poll('{tid}')", view["notes"][0])
        os.remove(self.pairs / "async" / f"{tid}.json")
        self.task(owner_pid=self.dead.pid, status="running", child_pid=self.live.pid,
                  started=A._av_now() - timedelta(seconds=1))
        view = self.view()
        self.assertEqual((view["verdict"], view["running_tasks"][0]["state"]), ("busy", "detached"))

    def test_detached_child_is_judged_by_its_own_creation_time(self):
        # A queued Codex task spawns its child long after the task started;
        # the owner rule ("created after the task = PID reuse") must not
        # declare that live child gone (integration fix, 2026-09-25).
        tid = self.task(owner_pid=self.dead.pid, status="running", child_pid=self.live.pid,
                        child_started_at=self.live_started,
                        started=datetime.fromtimestamp(self.live_started - 600, timezone.utc))
        view = self.view()
        self.assertEqual((view["verdict"], view["running_tasks"][0]["state"]), ("busy", "detached"))
        os.remove(self.pairs / "async" / f"{tid}.json")
        # A recorded creation time that doesn't match = the PID was reused.
        self.task(owner_pid=self.dead.pid, status="running", child_pid=self.live.pid,
                  child_started_at=self.live_started - 3600)
        self.assertNotEqual(self.view()["running_tasks"][0]["state"], "detached")

    def test_detach_contract_states(self):
        events = self.home / "run.events.jsonl"
        events.write_text("{}\n", encoding="utf-8")
        self.task(owner_pid=self.dead.pid, status="running", events_path=str(events))
        view = self.view()  # just written: the events file is still "advancing"
        self.assertEqual((view["verdict"], view["running_tasks"][0]["state"]), ("busy", "detached"))
        old = time.time() - 60
        os.utime(events, (old, old))
        view = self.view()
        self.assertEqual(view["running_tasks"][0]["state"], "finished_detached")
        self.assertNotEqual(view["verdict"], "busy")
        self.assertIn("pair_poll(", view["notes"][0])

    def test_settling_then_idle(self):
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        self.assertEqual(A.view(self.pair)["verdict"], "busy")
        w.turn_closed("t1")
        self.assertTrue(w.flush())
        now = A._av_now()
        self.assertEqual(self.view(now=now)["verdict"], "settling")
        later = self.view(now=now + timedelta(seconds=6))
        self.assertEqual(later["verdict"], "idle")
        self.assertGreaterEqual(later["quiet_for_s"], 5.0)

    def test_expired_wake_stays_unknown_until_evidence(self):
        # A deadline limits waiting; it never establishes that the wake won't
        # come (anvil re-review, 2026-09-25): unknown until something resolves it.
        w = A.writer(self.pair)
        w.background_finished("b1")
        self.assertTrue(w.flush())
        now = A._av_now()
        self.assertEqual(self.view(now=now)["verdict"], "busy")
        until = A._av_parse(w.snapshot()["wake_pending"]["until"])
        view = self.view(now=until + timedelta(seconds=1))
        self.assertEqual(view["verdict"], "unknown")
        self.assertIn("never started", view["reasons"][0])
        self.assertEqual(view["quiet_since"], A._av_iso(until))
        self.assertEqual([x["task_id"] for x in view["wake_stale"]], ["b1"])
        self.assertEqual(view["wake_pending"], [])
        self.assertEqual(self.view(now=until + timedelta(days=2))["verdict"], "unknown")
        # Evidence: a sign of life revives it; a clear (or a turn) resolves it.
        with patch.object(A, "_av_now", lambda: until + timedelta(seconds=30)):
            w.refresh_wake()
        self.assertTrue(w.flush())
        self.assertEqual(self.view(now=until + timedelta(seconds=31))["verdict"], "busy")
        self.assertTrue(w.clear_wake())
        self.assertTrue(w.flush())
        self.assertNotEqual(self.view(now=until + timedelta(days=2))["verdict"], "unknown")

    def test_stale_background_is_unknown_not_busy_and_stays_listed(self):
        w = A.writer(self.pair)
        w.background_started("b1", "local_bash", "long job", gen="g1")
        w.background_started("b2", "local_bash", "other runtime", gen="g2")
        flagged = w.mark_background_stale("no completion event for 24 h", gen="g1")
        self.assertEqual([e["task_id"] for e in flagged], ["b1"])
        self.assertEqual(w.mark_background_stale("again", gen="g1"), [])  # idempotent
        self.assertTrue(w.flush())
        view = self.view()
        self.assertEqual(view["verdict"], "busy")  # b2 is fresh
        w.background_finished("b2", gen="g2")
        w.clear_wake()
        self.assertTrue(w.flush())
        view = self.view(now=A._av_now() + timedelta(seconds=10))
        self.assertEqual(view["verdict"], "unknown")
        self.assertIn("no completion event for 24 h", " ".join(view["reasons"]))
        self.assertEqual([(b["task_id"], b["stale"]) for b in view["background"]], [("b1", True)])

    def test_generation_scoped_cleanup_never_touches_another_runtime(self):
        w = A.writer(self.pair)
        w.background_started("old", gen="g-old")
        w.background_started("new", gen="g-new")
        w.background_finished("x", gen="g-new")  # the new runtime's wake
        self.assertEqual(w.lose_background("late EOF of the old runtime", gen="g-old")[0]["task_id"], "old")
        snap = w.snapshot()
        self.assertEqual([b["task_id"] for b in snap["background"]], ["new"])
        self.assertIsNotNone(snap["wake_pending"])  # not the old runtime's to lose
        self.assertFalse(w.clear_wake(gen="g-old"))
        self.assertTrue(w.clear_wake(gen="g-new"))

    def test_publication_watermark(self):
        w = A.writer(self.pair)
        self.assertTrue(w.flush())
        good = w.mutation_seq()
        self.task(owner_pid=os.getpid(), finished=A._av_now() - timedelta(seconds=10), activity_seq=good)
        self.assertIn(self.view()["verdict"], ("idle", "settling"))
        with patch.object(A, "_atomic_write", side_effect=OSError("disk says no")):
            w.background_started("b1", "local_bash")  # never reaches the file
            self.assertFalse(w.flush(timeout=0.2))
            self.task(owner_pid=os.getpid(), finished=A._av_now() - timedelta(seconds=10),
                      activity_seq=A.mutation_seq(self.pair))
            file_view = self.view(now=A._av_now() + timedelta(seconds=10))  # the file's story
            self.assertEqual(file_view["verdict"], "unknown")
            self.assertIn("is behind its task", " ".join(file_view["reasons"]))
            own = A.view(self.pair)  # this process, from memory: its own file lags too
            self.assertIn("is behind its task", " ".join(own["reasons"]))
        self.assertTrue(w.flush())
        self.assertEqual(self.view()["verdict"], "busy")  # published: b1 visible

    def test_recreated_pair_keeps_the_tombstone_history(self):
        w = A.writer(self.pair)
        w.background_started("b1", "local_bash")
        self.assertTrue(w.flush())
        A.forget(self.pair)
        seq = self.read_own()["mut_seq"]
        w2 = A.writer(self.pair)  # recreated in the same process: same file name
        self.assertTrue(w2.flush())
        data = self.read_own()
        self.assertEqual(data["state"], "live")
        self.assertEqual([b["task_id"] for b in data["background_lost"]], ["b1"])
        self.assertGreater(data["mut_seq"], seq)

    def test_unreadable_or_newer_schema_is_unknown(self):
        path = self.pairs / "activity" / self.pair / f"{self.live.pid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        view = self.view()
        self.assertEqual((view["verdict"], view["owners"][0]["status"]), ("unknown", "unreadable"))
        self.foreign(self.live.pid, version=2, owner_started_at=self.live_started)
        self.assertEqual(self.view()["verdict"], "unknown")

    def test_window_lists_follow_the_latest_solicited_send(self):
        old = A._av_now() - timedelta(minutes=10)
        iso = A._av_iso(old)
        self.foreign(self.dead.pid, state="exited", window_started_at=iso, updated_at=iso,
                     window_turns=[{"task_id": "old", "kind": "send", "started_at": iso}],
                     compactions=[{"at": A._av_iso(old + timedelta(minutes=1)), "trigger": "auto"}])
        view = self.view()
        self.assertEqual([t["task_id"] for t in view["window_turns"]], ["old"])
        self.assertEqual(len(view["compactions"]), 1)
        w = A.writer(self.pair)
        w.turn_opened("new", "send")
        w.turn_closed("new")
        self.assertTrue(w.flush())
        view = self.view()
        self.assertEqual(view["window_started_at"], w.snapshot()["window_started_at"])
        self.assertEqual([t["task_id"] for t in view["window_turns"]], ["new"])
        self.assertEqual(view["compactions"], [])

    def test_latest_context_wins_across_owners(self):
        older = A._av_iso(A._av_now() - timedelta(minutes=1))
        self.foreign(self.live.pid, owner_started_at=self.live_started,
                     context={"used": 1, "window": 10, "pct": 10.0, "at": older})
        w = A.writer(self.pair)
        w.note_context(7, 10, window_source="reported")
        self.assertEqual(A.view(self.pair)["context"]["used"], 7)  # live state, before any flush

    def test_package_view_uses_unflushed_state_and_counts_self_as_new(self):
        release = threading.Event()
        real = A._atomic_write

        def blocked(path, text):
            release.wait(10)
            real(path, text)

        self.task(owner_pid=os.getpid(), finished=A._av_now())
        with patch.object(A, "_atomic_write", blocked):
            w = A.writer(self.pair)
            w.turn_opened("t1", "send")
            view = A.view(self.pair)
            release.set()
        self.assertEqual(view["verdict"], "busy")
        self.assertEqual(view["turns"][0]["task_id"], "t1")
        self.assertFalse(any("unknown" in r for r in view["reasons"]))

    def test_task_cache_rereads_only_changed_files(self):
        cache: dict = {}
        tid = self.task(owner_pid=self.dead.pid, status="running")
        A.pair_activity_view(self.pairs, self.pair, task_cache=cache)
        self.assertEqual(len(cache), 1)
        reads = []
        real = A._av_read_json

        def counting(path, attempts=3):
            reads.append(str(path))
            return real(path, attempts)

        with patch.object(A, "_av_read_json", counting):
            A.pair_activity_view(self.pairs, self.pair, task_cache=cache)
        self.assertFalse(any(tid in r for r in reads))
        os.remove(self.pairs / "async" / f"{tid}.json")
        A.pair_activity_view(self.pairs, self.pair, task_cache=cache)
        self.assertEqual(cache, {})


class ProcessTests(ActivityTestCase):
    def test_process_liveness_and_start_time(self):
        alive, started = A._av_proc(os.getpid())
        self.assertTrue(alive)
        self.assertLessEqual(started, time.time())
        self.assertEqual(A._av_proc(os.getpid())[1], started)
        alive, child_started = A._av_proc(self.live.pid)
        self.assertTrue(alive)
        self.assertGreater(child_started, started - 1)
        # Exited but its handle is still open in this process: must read dead.
        self.assertIs(A._av_proc(self.dead.pid)[0], False)
        self.assertEqual(A._av_proc(0), (False, None))
        self.assertEqual(A._av_proc(True), (False, None))


class StdlibBlockTests(ActivityTestCase):
    def test_block_is_stdlib_only_and_py37_syntax(self):
        block = stdlib_block()
        tree = ast.parse(block, feature_version=(3, 7))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                mods.add(node.module.split(".")[0])
        self.assertTrue(mods)
        self.assertTrue(mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names))
        self.assertNotIn("__future__", mods)  # wait.py already has it on line 1
        self.assertNotIn("\\", block)  # embedded in a non-raw ''' string in wait.py

    def test_block_references_only_its_own_names(self):
        tree = ast.parse(stdlib_block())
        bound = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
        loads = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.assertEqual(loads - bound, set())

    def test_block_standalone_matches_package(self):
        """Exec the block alone (as wait.py will) and compare its view with the
        package's on a fixture covering every branch."""
        ns = {"__name__": "embedded_wait"}
        code = compile(stdlib_block(), "<wait.py block>", "exec",
                       flags=__future__.annotations.compiler_flag, dont_inherit=True)
        exec(code, ns)
        at = A._av_iso(A._av_now())
        w = A.writer(self.pair)
        w.turn_opened("t1", "send")
        w.background_started("b1", "local_bash")
        w.turn_closed("t1", preview="p")
        w.note_context(10, 100, window_source="reported")
        self.assertTrue(w.flush())
        self.foreign(self.dead.pid, window_started_at=at, background=[{"task_id": "bd", "kind": "k",
                     "description": "", "started_at": at}])
        self.task(owner_pid=self.live.pid, finished=A._av_now())
        self.task(owner_pid=self.dead.pid, status="running")
        now = A._av_now()
        mine = A.pair_activity_view(self.pairs, self.pair, now=now)
        theirs = ns["pair_activity_view"](self.pairs, self.pair, now=now)
        self.assertEqual(json.dumps(theirs, sort_keys=True, default=str),
                         json.dumps(mine, sort_keys=True, default=str))
        self.assertEqual(mine["verdict"], "busy")

    def test_wait_script_embeds_the_block_verbatim(self):
        if BEGIN not in W.WAIT_SCRIPT_SOURCE:
            self.skipTest("wait.py embeds the activity view in phase 2")
        self.assertIn(stdlib_block(), W.WAIT_SCRIPT_SOURCE)


if __name__ == "__main__":
    unittest.main()
