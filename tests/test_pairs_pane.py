"""Offline read-only pane snapshots from isolated on-disk pair fixtures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".test-homes"
SCRATCH.mkdir(exist_ok=True)
HOME = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="pane-tests-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = str(Path(HOME.name) / "claude")
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
sys.path.insert(0, str(ROOT / "src"))
from claude_squared import _wait_script as W

spec = importlib.util.spec_from_file_location("pane_reader", ROOT / "mods/pairs-pane/read_pairs.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="pane-case-", ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.pairs = self.home / "pairs"
        W.install_wait_script(self.pairs / "wait.py")
        self.now = datetime.now(timezone.utc)
        self.started = (self.now - timedelta(seconds=120)).isoformat()
        self.write("registry.json", {"pairs": {
            "scout": {"backend": "codex", "model": "luna"}}})

    def write(self, relative, data):
        path = self.pairs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def activity(self, **changes):
        data = {"version": 1, "pair": "scout", "owner_pid": os.getpid(), "state": "live",
                "backend": "codex", "window_started_at": self.started,
                "updated_at": self.now.isoformat(), "published_seq": 1,
                "turn": {"task_id": "task", "kind": "send", "started_at": self.started},
                "context": {"used": 50000, "window": 200000, "at": self.now.isoformat()}}
        data.update(changes)
        return self.write(f"activity/scout/{os.getpid()}.json", data)

    def watch(self, name="one", **changes):
        data = {"version": 1, "pair": "scout", "session_id": "manager", "cwd": "/project",
                "hook_pid": os.getpid(), "started_at": self.now.isoformat()}
        data.update(changes)
        return self.write(f"watches/{name}.json", data)

    def test_open_turn_context_log_and_read_only(self):
        self.activity()
        self.watch()
        log = self.pairs / "logs/scout/main.log"
        log.parent.mkdir(parents=True)
        log.write_text("old\n" * 10000 + "\x1b[31m[T-9] Running tests ✓\x1b[0m\n", encoding="utf-8")
        before = {str(p): p.read_bytes() for p in self.pairs.rglob("*") if p.is_file()}
        data = P.snapshot(self.home, expanded=["scout"])
        self.assertEqual(len(data["rows"]), 1)
        row = data["rows"][0]
        self.assertEqual((row["name"], row["backend"], row["model"], row["status"]), ("scout", "codex", "luna", "busy"))
        self.assertGreaterEqual(row["elapsed_s"], 120)
        self.assertEqual(row["context"]["percent"], 25)
        self.assertEqual(row["last_line"], "[T-9] Running tests ✓")
        self.assertEqual(row["watches"][0]["session_id"], "manager")
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.pairs.rglob("*") if p.is_file()})

    def test_queued_task_without_activity_is_visible(self):
        self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": "running",
            "owner_pid": os.getpid(), "queued": True, "started_at": self.now.isoformat()})
        row = P.snapshot(self.home)["rows"][0]
        self.assertEqual(row["status"], "busy")
        self.assertEqual(row["watches"], [])

    def test_background_and_pending_wake_are_not_idle(self):
        self.activity(turn=None, background=[{"task_id": "bg", "kind": "Bash", "started_at": self.started}])
        self.assertEqual(P.snapshot(self.home)["rows"][0]["status"], "busy")
        self.activity(turn=None, wake_pending={"since": self.started,
            "until": (self.now + timedelta(seconds=60)).isoformat()})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["status"], "busy")
        self.activity(turn=None, wake_pending={"since": self.started, "until": self.started})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["status"], "unknown")

    def test_dead_and_reused_watcher_pids_are_hidden(self):
        self.watch("dead", hook_pid=999999999)
        self.watch("reused", started_at="2000-01-01T00:00:00+00:00")
        self.assertEqual(P.snapshot(self.home)["rows"][0]["watches"], [])
        self.assertEqual(len(list((self.pairs / "watches").glob("*.json"))), 2)
        self.watch("alive")
        self.watch("another-session", session_id="another-manager")
        row = P.snapshot(self.home)["rows"][0]
        self.assertEqual(len(row["watches"]), 2)
        self.assertEqual(row["status"], "idle")  # the watcher is still reporting/settling

    def test_orphaned_task_is_attention_not_a_live_turn(self):
        self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": "running",
            "owner_pid": 999999999, "started_at": self.started})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["status"], "needs attention")

    def test_invalid_records_and_registry_do_not_claim_idle(self):
        self.write("watches/invalid.json", {"version": 1, "pair": "../escape"})
        (self.pairs / "registry.json").write_text("broken", encoding="utf-8")
        self.activity()
        data = P.snapshot(self.home)
        self.assertIn("registry unavailable", data["error"])
        self.assertEqual(data["rows"][0]["status"], "busy")

    def test_standalone_without_package_and_missing_install(self):
        result = subprocess.run([sys.executable, "-S", "-B", str(ROOT / "mods/pairs-pane/read_pairs.py"),
                                 "--home", str(self.home)], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)["rows"][0]
        self.assertEqual((row["name"], row["status"]), ("scout", "idle"))
        self.assertIsNone(row["context"]["percent"])
        (self.pairs / "wait.py").unlink()
        self.assertIn("install wait.py", P.snapshot(self.home)["error"])

    def test_context_alert_matches_claude_and_codex_thresholds(self):
        cases = (
            ("claude", 1_000_000, 819_999, None, None, None, 820_000, False),
            ("claude", 1_000_000, 820_000, None, None, None, 820_000, True),
            ("claude", 1_000_000, 350_000, None, None, 350_000, 350_000, True),
            ("claude", 1_000_000, 770_000, None, 200_000, None, 770_000, True),
            ("claude", 1_000_000, 400_000, None, 2_000_000, 350_000, 350_000, True),
            ("codex", 200_000, 110_000, 150_000, None, None, 110_000, True),
            ("codex", 200_000, 139_999, None, None, 0, 140_000, False),
            ("codex", 200_000, 50_000, 150_000, None, 50_000, 50_000, True),
        )
        for backend, window, used, limit, headroom, override, expected, above in cases:
            with self.subTest(backend=backend, used=used, headroom=headroom, override=override):
                self.write("registry.json", {"pairs": {"scout": {"backend": backend,
                    "model": "test", "context_alert_at": override}}})
                self.write("defaults.json", {"context_alert_headroom": headroom})
                self.activity(context={"backend": backend, "used": used, "window": window,
                    "auto_compact_token_limit": limit, "at": self.now.isoformat()})
                fill = P.snapshot(self.home)["rows"][0]["context"]
                self.assertEqual(fill["alert_at"], expected)
                self.assertEqual(fill["above_alert"], above)
                self.assertEqual(fill["used"], used)
                self.assertEqual(fill["window"], window)

    def test_reported_window_and_codex_limit_supply_missing_context_fields(self):
        self.activity(context={"backend": "codex", "used": 110_000, "at": self.now.isoformat()})
        self.write("activity/scout/pair.json", {"reported_window": {
            "backend": "codex", "tokens": 200_000, "auto_compact_token_limit": 150_000}})
        fill = P.snapshot(self.home)["rows"][0]["context"]
        self.assertEqual(fill["window"], 200_000)
        self.assertEqual(fill["alert_at"], 110_000)
        self.assertTrue(fill["above_alert"])

    def test_idle_missing_context_and_invalid_fill_are_unknown_not_zero(self):
        row = P.snapshot(self.home)["rows"][0]
        self.assertEqual(row["status"], "idle")
        self.assertIsNone(row["context"]["used"])
        self.assertIsNone(row["context"]["percent"])
        self.assertFalse(row["context"]["above_alert"])
        for used in (None, True, -1, 300_000):
            self.activity(turn=None, context={"used": used, "window": 200_000, "at": self.now.isoformat()})
            fill = P.snapshot(self.home)["rows"][0]["context"]
            self.assertIsNone(fill["used"])
            self.assertEqual(fill["window"], 200_000)
            self.assertIsNone(fill["percent"])
            self.assertFalse(fill["above_alert"])
        self.activity(turn=None, context={"used": 0, "window": 200_000, "at": self.now.isoformat()})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["context"]["percent"], 0)

    def test_large_history_is_skipped_before_opening_and_recent_files_are_capped(self):
        folder = self.pairs / "async"
        folder.mkdir()
        large = json.dumps({"pair_name": "scout", "status": "done", "result": {"response": "x" * (8 * 1024 * 1024)}})
        for i in range(10):
            path = folder / f"old-{i}.json"
            path.write_text(large, encoding="utf-8")
            os.utime(path, (1, 1))
        (folder / "recent-large.json").write_text(large, encoding="utf-8")
        original = Path.open
        opened = []
        def track(path, *args, **kwargs):
            opened.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", track):
            data = P.snapshot(self.home)
        self.assertFalse(any(path.parent == folder for path in opened))
        self.assertLess(data["io"]["bytes"], 10000)
        self.assertIn("Partial snapshot", data["notice"])
        self.assertEqual(data["rows"][0]["status"], "unknown")

    def test_task_count_budget_and_warm_summary_cache(self):
        for i in range(P.TASK_BUDGET + 20):
            self.write(f"async/task-{i}.json", {"task_id": f"task-{i}", "pair_name": "scout",
                "status": "running", "owner_pid": os.getpid(), "started_at": self.now.isoformat()})
        cold = P.snapshot(self.home)
        self.assertLessEqual(cold["io"]["bytes"], P.BYTE_BUDGET)
        self.assertLessEqual(cold["io"]["files"], P.FILE_BUDGET)
        self.assertLessEqual(len(cold["cache"]["tasks"]), P.TASK_BUDGET)
        self.assertTrue(all(isinstance(item["mtime"], str) for item in cold["cache"]["tasks"].values()))
        warm = P.snapshot(self.home, cache=cold["cache"])
        self.assertLess(warm["io"]["bytes"], cold["io"]["bytes"])
        self.assertIn("Partial snapshot", warm["notice"])

    def test_filter_precedes_activity_reads_and_detail_requires_expansion(self):
        self.activity()
        self.watch()
        self.write("registry.json", {"pairs": {"scout": {}, "hidden": {}}})
        self.write(f"activity/hidden/{os.getpid()}.json", {"version": 1})
        original = P.Reads.json
        read_paths = []
        def track(reader, path, *args, **kwargs):
            read_paths.append(Path(path))
            return original(reader, path, *args, **kwargs)
        with patch.object(P.Reads, "json", track), patch.object(P, "tail", return_value="detail") as detail:
            collapsed = P.snapshot(self.home, session="manager", show_all=False)
            detail.assert_not_called()
            self.assertEqual([r["name"] for r in collapsed["rows"]], ["scout"])
            self.assertEqual(collapsed["rows"][0]["last_line"], "")
            self.assertFalse(any("hidden" in path.parts for path in read_paths))
            P.snapshot(self.home, session="manager", show_all=False, expanded=["hidden", "scout"])
            detail.assert_called_once_with(self.pairs / "logs/scout/main.log")

    def test_activity_reference_keeps_an_old_running_task_eligible(self):
        self.activity()
        path = self.write("async/task.json", {"task_id": "task", "pair_name": "scout",
            "status": "running", "owner_pid": os.getpid(), "started_at": self.now.isoformat()})
        os.utime(path, (1, 1))
        data = P.snapshot(self.home)
        self.assertEqual(data["cache"]["tasks"]["task.json"]["summary"]["status"], "running")
        self.assertEqual(data["rows"][0]["status"], "busy")

    def test_completed_receipt_hides_watch_even_while_pid_is_alive(self):
        path = self.watch()
        record = json.loads(path.read_text(encoding="utf-8"))
        path.with_suffix(".done").write_text(json.dumps({"state": "done", "hook_pid": record["hook_pid"],
            "started_at": record["started_at"]}), encoding="utf-8")
        self.assertEqual(P.snapshot(self.home, session="manager", show_all=False)["rows"], [])

    def test_recent_completion_and_last_claude_result_context(self):
        self.write("registry.json", {"pairs": {"scout": {"backend": "claude", "model": "claude-opus-5-5"}}})
        finished = (self.now - timedelta(minutes=90)).isoformat()
        path = self.write("async/result.json", {"task_id": "result", "pair_name": "scout", "status": "done",
            "finished_at": finished, "result": {"context": {"tokens_used": 123000, "tokens_max": 1000000},
                                                "response": "not retained"}})
        os.utime(path, (self.now.timestamp() - 5400,) * 2)
        data = P.snapshot(self.home)
        row = data["rows"][0]
        self.assertEqual(row["finished_at"], finished)
        self.assertAlmostEqual(row["finished_ago_s"], 5400, delta=5)
        self.assertEqual(row["context"]["used"], 123000)
        self.assertNotIn("not retained", json.dumps(data["cache"]))
        self.assertEqual(P.snapshot(self.home, cache=data["cache"])["rows"][0]["context"], row["context"])
        self.activity(backend="claude", turn=None, context=None, compactions=[{"at": self.now.isoformat()}])
        self.assertIsNone(P.snapshot(self.home)["rows"][0]["context"]["used"])

    def test_idle_owner_context_and_failed_status_come_from_activity(self):
        self.activity(turn=None, window_turns=[{"started_at": self.started, "ended_at": self.now.isoformat(),
                                               "status": "failed"}])
        row = P.snapshot(self.home)["rows"][0]
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["context"]["used"], 50000)
        self.assertAlmostEqual(row["finished_ago_s"], 0, delta=5)

    def test_idle_claude_keeps_last_known_owner_context(self):
        self.write("registry.json", {"pairs": {"scout": {"backend": "claude"}}})
        self.activity(backend="claude", turn=None,
                      context={"used": None, "window": 1000000, "at": self.now.isoformat()})
        self.write("activity/scout/999999999.json", {"version": 1, "state": "closed", "backend": "claude",
            "context": {"used": 75000, "window": 200000, "window_source": "inferred", "at": self.started}})
        self.write("activity/scout/pair.json", {"reported_window": {"tokens": 1000000, "backend": "claude"}})
        fill = P.snapshot(self.home)["rows"][0]["context"]
        self.assertEqual((fill["used"], fill["window"], fill["percent"]), (75000, 1000000, 7.5))

    def test_detail_skips_turn_bookkeeping(self):
        self.activity()
        log = self.pairs / "logs/scout/main.log"
        log.parent.mkdir(parents=True)
        log.write_text("[19:44:21] [T-9] Tests passed\n"
                       "[19:44:22] === TURN COMPLETED (in 36,632,455 / cached 36,319,616) ===\n", encoding="utf-8")
        self.assertEqual(P.snapshot(self.home, expanded=["scout"])["rows"][0]["last_line"],
                         "[19:44:21] [T-9] Tests passed")

    def test_row_aging_into_idle_group_stops_detail_reads(self):
        self.activity(turn=None, window_turns=[{"started_at": self.started,
            "ended_at": (self.now - timedelta(hours=3)).isoformat(), "status": "done"}])
        with patch.object(P, "tail") as detail:
            self.assertEqual(P.snapshot(self.home, expanded=["scout"])["rows"][0]["last_line"], "")
            detail.assert_not_called()

    def test_owner_pid_alone_does_not_guess_manager_session(self):
        self.activity()
        self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": "running",
            "owner_pid": os.getpid(), "started_at": self.now.isoformat()})
        self.assertEqual(P.snapshot(self.home, session="manager", show_all=False)["rows"], [])
        self.assertEqual(P.snapshot(self.home)["rows"][0]["status"], "busy")


if __name__ == "__main__":
    unittest.main()
