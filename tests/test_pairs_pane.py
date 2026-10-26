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
            "scout": {"backend": "codex", "model": "luna"},
            "idle": {"backend": "claude", "model": "haiku"}}})

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
        data = P.snapshot(self.home)
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
        self.assertEqual(P.snapshot(self.home)["rows"], [])
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
        self.assertEqual(json.loads(result.stdout)["rows"], [])
        (self.pairs / "wait.py").unlink()
        self.assertIn("install wait.py", P.snapshot(self.home)["error"])


if __name__ == "__main__":
    unittest.main()
