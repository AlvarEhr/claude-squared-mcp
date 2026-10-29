"""Offline read-only pane snapshots from isolated on-disk pair fixtures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sqlite3
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
        self.assertEqual(row["detail"]["logs"][-1], "[T-9] Running tests ✓")
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
        self.assertEqual(P.snapshot(self.home)["rows"][0]["status"], "orphaned")

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
        self.assertIn("budget", data["notice"])
        self.assertEqual(data["rows"][0]["status"], "unknown")
        self.assertTrue(data["rows"][0]["details_skipped"])

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
        self.assertIn("budget", warm["notice"])

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
            self.assertIsNone(collapsed["rows"][0]["detail"])
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
        self.assertEqual(P.snapshot(self.home, expanded=["scout"])["rows"][0]["detail"]["logs"],
                         ["[19:44:21] [T-9] Tests passed"])

    def test_row_aging_into_idle_group_stops_detail_reads(self):
        self.activity(turn=None, window_turns=[{"started_at": self.started,
            "ended_at": (self.now - timedelta(hours=3)).isoformat(), "status": "done"}])
        with patch.object(P, "tail") as detail:
            self.assertIsNone(P.snapshot(self.home, expanded=["scout"])["rows"][0]["detail"])
            detail.assert_not_called()

    def test_owner_pid_alone_does_not_guess_manager_session(self):
        self.activity()
        self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": "running",
            "owner_pid": os.getpid(), "started_at": self.now.isoformat()})
        self.assertEqual(P.snapshot(self.home, session="manager", show_all=False)["rows"], [])
        self.assertEqual(P.snapshot(self.home)["rows"][0]["status"], "busy")

    def test_floating_model_uses_latest_matching_report_without_extra_reads(self):
        self.write("registry.json", {"pairs": {"scout": {"backend": "codex", "model": "sol"}}})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["model"], "sol")
        self.write("activity/scout/pair.json", {"reported_window": {"tokens": 200000,
            "model_key": "sol|default", "model": "gpt-6-sol", "at": self.started}})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["model"], "gpt-6-sol")
        self.write("async/result.json", {"task_id": "result", "pair_name": "scout", "status": "done",
            "finished_at": self.now.isoformat(), "result": {"model_used": "gpt-6.1-sol"}})
        data = P.snapshot(self.home)
        self.assertEqual(data["rows"][0]["model"], "gpt-6.1-sol")
        self.assertEqual(P.snapshot(self.home, cache=data["cache"])["rows"][0]["model"], "gpt-6.1-sol")
        self.write("registry.json", {"pairs": {"scout": {"backend": "codex", "model": "astra"}}})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["model"], "astra")
        self.write("registry.json", {"pairs": {"scout": {"backend": "codex", "model": "gpt-6-sol"}}})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["model"], "gpt-6-sol")

    def test_claude_alias_uses_resolved_model_metadata(self):
        self.write("registry.json", {"pairs": {"scout": {"backend": "claude", "model": "fable"}}})
        self.write("activity/scout/pair.json", {"reported_window": {"tokens": 1000000,
            "model_key": "fable|default", "model": "claude-fable-5-1", "at": self.now.isoformat()}})
        self.assertEqual(P.snapshot(self.home)["rows"][0]["model"], "claude-fable-5-1")

    def test_expanded_detail_keeps_reply_and_log_lines_whole_without_caching_bodies(self):
        reply = "A complete reply " * 100 + "\nlast line"
        self.activity(window_turns=[{"task_id": "previous", "started_at": self.started,
                                    "ended_at": self.started, "status": "done"}])
        previous = self.write("async/previous.json", {"task_id": "previous", "pair_name": "scout", "status": "done",
            "finished_at": self.started, "result": {"response": reply}})
        os.utime(previous, (1, 1))  # activity reference makes an older reply eligible
        self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": "running",
            "owner_pid": os.getpid(), "started_at": self.now.isoformat(), "message": "Investigate the failure\nMore instructions"})
        log = self.pairs / "logs/scout/main.log"
        log.parent.mkdir(parents=True)
        long_line = "[21:30:00] " + "whole word " * 60
        log.write_text("\n".join([f"[21:29:00] line {i}" for i in range(20)] + [long_line]), encoding="utf-8")
        snapshot = P.snapshot(self.home, expanded=["scout"])
        detail = snapshot["rows"][0]["detail"]
        self.assertEqual(detail["reply"], reply)
        self.assertEqual(detail["working_on"], "Investigate the failure")
        self.assertEqual(len(detail["logs"]), 21)
        self.assertEqual(detail["logs"][-1], long_line.strip())
        self.assertNotIn(reply, json.dumps(snapshot["cache"]))
        self.assertEqual(P.snapshot(self.home, expanded=["scout"], cache=snapshot["cache"])["rows"][0]["detail"], detail)
        self.assertIsNone(P.snapshot(self.home, cache=snapshot["cache"])["rows"][0]["detail"])

    def test_finished_status_and_notice_ignore_unrelated_old_history(self):
        self.activity(turn=None, window_turns=[{"started_at": self.started, "ended_at": self.now.isoformat(), "status": "done"}])
        old = self.write("async/old.json", {"status": "done", "pair_name": "other"})
        os.utime(old, (1, 1))
        data = P.snapshot(self.home)
        self.assertEqual(data["rows"][0]["status"], "idle")
        self.assertIsNone(data["notice"])
        self.activity()
        self.write("async/task.json", {"message": "x" * P.TASK_LIMIT})
        data = P.snapshot(self.home)
        self.assertEqual(data["rows"][0]["status"], "busy")
        self.assertTrue(data["rows"][0]["partial"])
        self.assertIn("budget", data["notice"])

    def test_transcript_is_explicit_bounded_and_paged(self):
        self.write("registry.json", {"pairs": {"scout": {"backend": "claude", "cwd": "/fixture", "session_id": "thread"}}})
        path = self.home / "projects/-fixture/thread.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps({"timestamp": self.now.isoformat(), "message": {
            "role": "assistant", "content": [{"type": "text", "text": f"message {i} " + "words " * 200}]}})
            for i in range(100)) + "\n", encoding="utf-8")
        before = path.read_bytes()
        with patch.object(P, "transcript_page", side_effect=AssertionError("implicit transcript read")):
            P.snapshot(self.home)
        page = P.transcript_page(self.home, "scout")
        self.assertLessEqual(page["bytes"], P.TASK_LIMIT)
        self.assertTrue(page["lines"][0]["text"].startswith("message 0 "))
        self.assertIsNotNone(page["next"])
        second = P.transcript_page(self.home, "scout", page["next"])
        self.assertNotEqual(second["lines"][0]["text"], page["lines"][-1]["text"])
        self.assertEqual(path.read_bytes(), before)

    def test_codex_transcript_lookup_is_read_only_and_does_not_restore_compressed_files(self):
        codex = self.home / "codex"
        codex.mkdir()
        path = codex / "rollout-thread.jsonl"
        path.write_text(json.dumps({"payload": {"role": "assistant", "content": [{"type": "output_text", "text": "Reply"}]}}) + "\n", encoding="utf-8")
        database = codex / "state_5.sqlite"
        con = sqlite3.connect(database)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
        con.execute("INSERT INTO threads VALUES (?, ?)", ("thread", str(path)))
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        self.write("registry.json", {"pairs": {"scout": {"backend": "codex", "session_id": "thread"}}})
        before = database.read_bytes()
        files_before = {file.name for file in codex.iterdir()}
        with patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
            self.assertEqual(P.transcript_page(self.home, "scout")["lines"][0]["text"], "Reply")
            self.assertEqual({file.name for file in codex.iterdir()}, files_before)
            path.rename(path.with_suffix(".jsonl.zst"))
            self.assertIn("compressed", P.transcript_page(self.home, "scout")["note"])
            self.assertFalse(path.exists())
        self.assertEqual(database.read_bytes(), before)

    def test_sixty_pairs_busy_last_is_prioritized_and_idle_uses_only_registry(self):
        names = [f"idle-{i:02}" for i in range(59)] + ["zz-busy"]
        self.write("registry.json", {"pairs": {name: {"backend": "claude", "model": "opus"} for name in names}})
        for name in names[:-1]:
            for owner in range(3):
                path = self.write(f"activity/{name}/{1000 + owner}.json", {"version": 1, "state": "closed"})
                os.utime(path, (1, 1))
        self.write(f"activity/zz-busy/{os.getpid()}.json", {"version": 1, "state": "live", "owner_pid": os.getpid(),
            "turn": {"task_id": "busy", "started_at": self.started},
            "context": {"used": 50000, "window": 1000000, "at": self.now.isoformat()}})
        self.write("async/busy.json", {"task_id": "busy", "pair_name": "zz-busy", "status": "running",
            "owner_pid": os.getpid(), "started_at": self.now.isoformat(), "message": "Work on this"})
        self.write("async/previous.json", {"task_id": "previous", "pair_name": "zz-busy", "status": "done",
            "finished_at": self.started, "result": {"response": "Previous reply"}})
        original = Path.open
        opened = []
        def track(path, *args, **kwargs):
            opened.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", track):
            data = P.snapshot(self.home, expanded=["zz-busy"])
        self.assertEqual(len(data["rows"]), 60)
        self.assertEqual(data["rows"][0]["name"], "zz-busy")
        row = data["rows"][0]
        self.assertEqual(row["status"], "busy")
        self.assertEqual(row["context"]["used"], 50000)
        self.assertEqual(row["detail"]["reply"], "Previous reply")
        self.assertEqual(row["detail"]["working_on"], "Work on this")
        self.assertFalse(any("activity" in path.parts and any(part.startswith("idle-") for part in path.parts) for path in opened))
        self.assertTrue(all(row["compact"] for row in data["rows"][1:]))

    def test_exhausted_detail_budget_keeps_every_registry_pair_visible(self):
        names = [f"recent-{i:02}" for i in range(70)]
        self.write("registry.json", {"pairs": {name: {} for name in names}})
        for name in names:
            for owner in range(3):
                self.write(f"activity/{name}/{1000 + owner}.json", {"version": 1, "state": "closed"})
        data = P.snapshot(self.home)
        self.assertEqual({row["name"] for row in data["rows"]}, set(names))
        self.assertLessEqual(data["io"]["files"], P.FILE_BUDGET)
        self.assertTrue(any(row.get("details_skipped") for row in data["rows"]))
        self.assertTrue(data["budget_limited"])
        self.assertIn("budget", data["notice"])

    def test_elapsed_scan_budget_marks_unconfirmed_idle_rows(self):
        for name in ("old-a", "old-b"):
            path = self.write(f"async/{name}.json", {"status": "done"})
            os.utime(path, (1, 1))
        with patch.object(P, "ASYNC_SCAN_SECONDS", 0):
            data = P.snapshot(self.home)
        self.assertEqual([row["name"] for row in data["rows"]], ["scout"])
        self.assertTrue(data["rows"][0]["details_skipped"])
        self.assertTrue(data["budget_limited"])

    def test_expired_watch_records_are_skipped_by_mtime_without_reading(self):
        old = self.watch("expired")
        old.write_text("not JSON" * 100000, encoding="utf-8")
        os.utime(old, (self.now.timestamp() - P.WATCH_MAX_AGE - 1,) * 2)
        self.watch("current")
        original = Path.open
        def checked(path, *args, **kwargs):
            self.assertNotEqual(path, old)
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", checked):
            data = P.snapshot(self.home)
        self.assertEqual(len(data["rows"][0]["watches"]), 1)
        self.assertFalse(data["budget_limited"])
        self.assertTrue(old.exists())  # the pane stays read-only

    def test_registry_only_idle_rows_survive_unrelated_history_limits_without_unknowns(self):
        registered = [f"registered-{i:02}" for i in range(52)]
        forgotten = [f"forgotten-{i:02}" for i in range(37)]
        self.write("registry.json", {"pairs": {name: {"backend": "claude", "model": "opus"} for name in registered}})
        for name in registered + forgotten:
            # A PID no OS assigns (macOS caps at 99,999; Linux at 4,194,304): PID 123
            # was a live system process on macOS runners, so its file was read.
            path = self.write(f"activity/{name}/99999999.json", {"version": 1, "state": "closed"})
            os.utime(path, (1, 1))
        self.write("async/oversized.json", {"result": "x" * P.TASK_LIMIT})
        original = Path.open
        def checked(path, *args, **kwargs):
            self.assertNotIn("activity", path.parts, "idle rows must not load activity JSON")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", checked):
            data = P.snapshot(self.home)
        self.assertEqual({row["name"] for row in data["rows"]}, set(registered))
        self.assertTrue(all(row["status"] == "idle" and row["compact"] for row in data["rows"]))
        self.assertTrue(data["budget_limited"])

    def test_nonregistry_rows_require_busy_or_recent_completion_evidence(self):
        for name in ("forgotten-old", "forgotten-touched"):
            path = self.write(f"activity/{name}/123.json", {"version": 1, "state": "closed"})
            if name == "forgotten-old":
                os.utime(path, (1, 1))
        self.write("async/running.json", {"task_id": "running", "pair_name": "forgotten-busy", "status": "running",
            "owner_pid": os.getpid(), "started_at": self.now.isoformat()})
        self.write("async/finished.json", {"task_id": "finished", "pair_name": "forgotten-recent", "status": "done",
            "finished_at": self.started, "result": {"response": "Done"}})
        data = P.snapshot(self.home)
        self.assertEqual({row["name"] for row in data["rows"]}, {"scout", "forgotten-busy", "forgotten-recent"})
        self.assertEqual(next(row for row in data["rows"] if row["name"] == "forgotten-busy")["status"], "busy")

    def test_skipped_compact_details_do_not_change_an_established_idle_status(self):
        completed = self.write("async/finished.json", {"task_id": "finished", "pair_name": "scout", "status": "done",
            "finished_at": self.started})
        with patch.object(P, "ROW_BUDGET", 0):
            row = P.snapshot(self.home)["rows"][0]
        self.assertTrue(row["details_skipped"])
        self.assertEqual(row["status"], "idle")
        completed.unlink()
        self.activity()  # the primary activity pass establishes busy before detail reads
        with patch.object(P, "ROW_BUDGET", 0):
            row = P.snapshot(self.home)["rows"][0]
        self.assertTrue(row["details_skipped"])
        self.assertEqual(row["status"], "busy")

    def test_large_old_task_directory_does_not_hide_late_pair_or_make_others_unknown(self):
        names = [f"idle-{i:02}" for i in range(51)] + ["zz-working"]
        self.write("registry.json", {"pairs": {name: {} for name in names}})
        folder = self.pairs / "async"
        folder.mkdir()
        for i in range(10000):
            path = folder / f"old-{i:05}.json"
            path.write_text('{"status":"done"}', encoding="utf-8")
            os.utime(path, (1, 1))
        self.write(f"activity/zz-working/{os.getpid()}.json", {"version": 1, "state": "live",
            "owner_pid": os.getpid(), "turn": {"task_id": "zzz-running", "started_at": self.now.isoformat()}})
        self.write("async/zzz-running.json", {"task_id": "zzz-running", "pair_name": "zz-working", "status": "running",
            "owner_pid": os.getpid(), "started_at": self.now.isoformat()})
        original = Path.open
        def checked(path, *args, **kwargs):
            self.assertFalse(path.name.startswith("old-"), "old history must be filtered before opening")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", checked):
            data = P.snapshot(self.home)
        self.assertEqual(len(data["rows"]), 52)
        self.assertEqual(data["rows"][0]["name"], "zz-working")
        self.assertEqual(data["rows"][0]["status"], "busy")
        self.assertTrue(all(row["status"] == "idle" for row in data["rows"][1:]), data["notice"])
        self.assertFalse(data["budget_limited"], data["io"])
        (self.pairs / "activity/zz-working" / f"{os.getpid()}.json").unlink()
        with patch.object(Path, "open", checked):
            cold = P.snapshot(self.home)  # no activity reference or previous cache
        self.assertEqual(cold["rows"][0]["status"], "busy")
        self.assertTrue(all(row["status"] == "idle" for row in cold["rows"][1:]), cold["notice"])

    def test_activity_primary_finds_an_old_task_even_when_supplement_scan_times_out(self):
        self.activity()
        task = self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": "running",
            "owner_pid": os.getpid(), "started_at": self.now.isoformat()})
        os.utime(task, (1, 1))
        with patch.object(P, "ASYNC_SCAN_SECONDS", 0):
            data = P.snapshot(self.home)
        self.assertEqual(data["rows"][0]["status"], "busy")
        self.assertIn("task.json", data["cache"]["tasks"])

    def test_interrupted_turns_are_not_replies_and_naive_utc_is_normalized(self):
        for status, error, expected in [("failed", "ORPHANED: owner exited", "orphaned"),
                ("failed", "CRASHED: runtime exited", "crashed"), ("failed", "CLIError: failure", "failed"),
                ("stopped", "user stopped the turn", "stopped")]:
            at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": status,
                "finished_at": at, "error": error})
            data = P.snapshot(self.home, expanded=["scout"])
            with self.subTest(expected=expected):
                row = data["rows"][0]
                self.assertEqual(row["status"], expected)
                self.assertEqual(row["outcome"], expected)
                self.assertEqual(row["detail"]["outcome"], expected)
                self.assertIsNone(row["detail"]["reply"])
                self.assertEqual(row["detail"]["reply_at"], row["finished_at"])
                self.assertTrue(row["finished_at"].endswith("+00:00"))
                self.assertNotIn(error, json.dumps(row))

    def test_latest_physical_log_tail_crosses_days_and_refreshes_with_warm_task_cache(self):
        self.activity()
        self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": "running",
            "owner_pid": os.getpid(), "started_at": self.started})
        log = self.pairs / "logs/scout/main.log"
        log.parent.mkdir(parents=True)
        log.write_text("[21:53:00] [T-561] older day\n[21:53:01] [T-562] older day\n", encoding="utf-8")
        old = P.snapshot(self.home, expanded=["scout"])
        with log.open("a", encoding="utf-8") as stream:
            stream.write("[14:10:00] [T-566] latest day\n[14:10:01] [T-567] latest day\n")
        new = P.snapshot(self.home, expanded=["scout"], cache=old["cache"])
        self.assertEqual(new["rows"][0]["detail"]["logs"][-2:],
            ["[14:10:00] [T-566] latest day", "[14:10:01] [T-567] latest day"])

    def test_successful_reply_quoting_orphan_error_is_not_an_orphan(self):
        self.write("async/task.json", {"task_id": "task", "pair_name": "scout", "status": "done",
            "finished_at": self.started, "result": {"response": "ORPHANED: example error text"}})
        row = P.snapshot(self.home, expanded=["scout"])["rows"][0]
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["detail"]["reply"], "ORPHANED: example error text")

    def test_tool_detail_reads_codex_items_and_claude_transcripts_on_request(self):
        self.write("logs/scout/codex-tool-items/T-7.json", {"tag": "T-7", "tool_name": "command_execution",
            "completed": {"type": "command_execution", "command": "pytest -q", "aggregated_output": "1 failed",
                          "exit_code": 1}})
        detail = P.tool_detail(self.home, "scout", "T-7")
        self.assertEqual((detail["input"], detail["output"], detail["status"]), ("pytest -q", "1 failed", "exit 1"))
        self.write("registry.json", {"pairs": {"helper": {"backend": "claude", "session_id": "s1", "cwd": "C:/w"}}})
        self.write("logs/helper/main.idx.json", {"T-3": {"tool_use_id": "toolu_9"}})
        transcript = self.home / "projects" / "C--w" / "s1.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("\n".join(json.dumps(line) for line in [
            {"message": {"content": [{"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "ls"}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "a.py"}]}},
        ]), encoding="utf-8")
        detail = P.tool_detail(self.home, "helper", "T-3")
        self.assertEqual((detail["input"], detail["language"], detail["output"]), ("ls", "bash", "a.py"))
        self.write("logs/helper/main.idx.json", {"T-3": {"tool_use_id": "toolu_9"}, "T-5": {"tool_use_id": "toolu_sub"}})
        sub = transcript.parent / "s1" / "subagents" / "agent-1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps({"message": {"content": [{"type": "tool_use", "id": "toolu_sub", "name": "Read",
                                                           "input": {"file_path": "x.py"}}]}}), encoding="utf-8")
        self.assertIn("x.py", P.tool_detail(self.home, "helper", "T-5")["input"])
        self.assertIn("note", P.tool_detail(self.home, "helper", "T-4"))
        self.assertIn("note", P.tool_detail(self.home, "../x", "T-3"))


if __name__ == "__main__":
    unittest.main()
