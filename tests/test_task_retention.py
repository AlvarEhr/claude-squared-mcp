"""Offline task-retention safety in disposable worktree homes; no model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".test-homes"
SCRATCH.mkdir(exist_ok=True)
HOME = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="retention-", ignore_cleanup_errors=True)
os.environ["CLAUDE_HOME"] = HOME.name
os.environ["CODEX_HOME"] = str(Path(HOME.name) / "codex")
sys.path.insert(0, str(ROOT / "src"))
from filelock import FileLock
from claude_squared import async_tasks as A, activity, task_retention as T, server as S


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=HOME.name, ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "pairs"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"CLAUDE_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = time.time()
        self.old = self.now - T.RETENTION_SECONDS - 100

    def task(self, name, **changes):
        data = {"task_id": name, "pair_name": "fixture", "status": "done", "message": "fixture",
                "finished_at": datetime.fromtimestamp(self.old, timezone.utc).replace(tzinfo=None).isoformat()}
        data.update(changes)
        path = self.root / "async" / (name + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        os.utime(path, (self.old, self.old))
        return path

    def reference(self, name):
        path = self.root / "activity/fixture/123.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "window_turns": [{"task_id": name}]}), encoding="utf-8")
        return path

    def test_prunes_only_old_terminal_unreferenced_tasks_and_logs_counts(self):
        deleted = [self.task(status, status=status) for status in ("done", "failed", "stopped")]
        protected = [self.task("running", status="running", owner_pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat()),
                     self.task("queued", queued=True), self.task("detached", detached=True),
                     self.task("detached-unknown", events_path="fixture.events.jsonl"),
                     self.task("detached-running", status="running", events_path="fixture.events.jsonl",
                               owner_pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat()),
                     self.task("referenced"), self.task("missing-date", finished_at=None),
                     self.task("recent", finished_at=datetime.now(timezone.utc).isoformat())]
        self.reference("referenced")
        with self.assertLogs(T.logger, "INFO") as logs:
            result = T.prune_terminal_tasks(self.root, now=self.now)
        self.assertEqual(result["deleted"], 3)
        self.assertTrue(all(not path.exists() for path in deleted))
        self.assertTrue(all(path.exists() for path in protected))
        self.assertTrue(any("deleted=3" in line for line in logs.output))

    def test_daily_gate_persists_and_recent_mtime_protects_old_payload(self):
        first = self.task("first")
        with FileLock(str(self.root / "async/.retention.lock"), timeout=0):
            self.assertFalse(T.prune_terminal_tasks(self.root, now=self.now)["ran"])
            self.assertTrue(first.exists())
        self.assertEqual(T.prune_terminal_tasks(self.root, now=self.now)["deleted"], 1)
        later = self.task("later")
        self.assertFalse(T.prune_terminal_tasks(self.root, now=self.now + 100)["ran"])
        self.assertTrue(later.exists())
        os.utime(later, (self.now, self.now))
        result = T.prune_terminal_tasks(self.root, now=self.now + T.CHECK_INTERVAL_SECONDS)
        self.assertTrue(result["ran"])
        self.assertTrue(later.exists())

    def test_rechecks_new_activity_reference_and_task_update_before_deleting(self):
        for change in ("reference", "task"):
            with self.subTest(change=change):
                path = self.task("late-" + change)
                original = T._activity_references
                def changed(root, ids, pair=None):
                    result = original(root, ids, pair)
                    if pair is None:
                        if change == "reference":
                            self.reference(path.stem)
                        else:
                            os.utime(path, (self.now, self.now))
                    return result
                with patch.object(T, "_activity_references", changed):
                    T.prune_terminal_tasks(self.root, now=self.now)
                self.assertTrue(path.exists())
                (self.root / "async/.retention").unlink()

    def test_unreadable_activity_aborts_without_pruning(self):
        task = self.task("protected")
        record = self.reference(task.stem)
        for text in ("broken", '{"version":999,"opaque":"future schema"}'):
            record.write_text(text, encoding="utf-8")
            with self.assertLogs(T.logger, "WARNING"):
                result = T.prune_terminal_tasks(self.root, now=self.now)
            self.assertTrue(task.exists())
            self.assertEqual(result["deleted"], 0)
            self.assertGreater(result["errors"], 0)
            (self.root / "async/.retention").unlink()

    def test_live_or_unverifiable_children_and_local_pending_states_are_kept(self):
        for alive in (True, None):
            path = self.task("child", child_pid=123)
            with patch.object(activity, "_av_proc", return_value=(alive, None)):
                T.prune_terminal_tasks(self.root, now=self.now)
            self.assertTrue(path.exists())
            (self.root / "async/.retention").unlink()
        local = self.task("unsaved")
        memory = self.task("memory-reference")
        writer = SimpleNamespace(_home=self.root, pair="fixture", snapshot=lambda: {"turn": {"task_id": memory.stem}})
        with patch.dict(A._unsaved_results, {local.stem: SimpleNamespace()}), \
                patch.object(activity, "_writers_snapshot", return_value=[writer]), \
                patch.object(activity, "_av_proc", return_value=(None, None)):
            T.prune_terminal_tasks(self.root, now=self.now)
        self.assertTrue(local.exists())
        self.assertTrue(memory.exists())

    def test_pair_lock_and_bad_task_are_skipped_without_blocking(self):
        path = self.task("busy-pair")
        bad = self.task("bad")
        bad.write_text("broken", encoding="utf-8")
        os.utime(bad, (self.old, self.old))
        (self.root / "locks").mkdir()
        with FileLock(str(self.root / "locks/fixture.lock"), timeout=0):
            result = T.prune_terminal_tasks(self.root, now=self.now)
        self.assertTrue(path.exists())
        self.assertTrue(bad.exists())
        self.assertEqual(result["deleted"], 0)

    def test_server_starts_housekeeping_and_worker_runs_once_per_home(self):
        with patch.object(S.activity_mod, "announce"), patch.object(S.activity_mod, "sweep"), \
                patch.object(S.runtime_mod, "install_exit_handler"), patch.object(S.mcp, "run"), \
                patch.object(T, "start_housekeeping") as start:
            S.run()
            start.assert_called_once_with()
            start.side_effect = OSError("fixture start failure")
            with self.assertLogs(T.logger, "WARNING"):
                S.run()  # housekeeping is best-effort, never a server startup gate
        called = threading.Event()
        with patch.object(T, "prune_terminal_tasks", side_effect=lambda root: called.set()) as prune:
            stop = T.start_housekeeping()
            try:
                self.assertTrue(called.wait(3))
                self.assertIs(T.start_housekeeping(), stop)
                prune.assert_called_once_with(self.root.resolve())
            finally:
                stop.set()
                worker, _ = T._workers.pop(self.root.resolve())
                worker.join(3)
                self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
