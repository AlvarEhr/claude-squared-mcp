"""v0.9.7 smoke: background watcher (wait.py) accepts a pair name too.

Pure unit tests + a real subprocess run of the installed wait.py. Run:
    python -u tests/smoke_v097.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v097_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_squared import async_tasks
from claude_squared._wait_script import install_wait_script
from claude_squared.models import AsyncTaskState
from claude_squared.registry import async_dir
from claude_squared.server import _format_async_handle


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"  [FAIL] {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def assert_true(cond, label):
    if not cond:
        print(f"  [FAIL] {label}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _write_task(task_id, pair_name, status, started_at):
    async_dir().mkdir(parents=True, exist_ok=True)
    async_tasks._save(AsyncTaskState(
        task_id=task_id, pair_name=pair_name, message="m", status=status,
        started_at=started_at, owner_pid=os.getpid(),
    ))


def _run_wait(arg):
    wp = install_wait_script()
    env = dict(os.environ)
    env["CLAUDE_HOME"] = _TMPDIR
    return subprocess.run(
        [sys.executable, str(wp), arg, "--timeout", "10", "--poll", "1"],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_wait_by_pair_name_done():
    print("=== wait.py <pair_name> resolves to latest task → exit 0 on done ===")
    _write_task("wt-old", "wpair", "done", datetime(2026, 1, 1, 10, 0, 0))
    _write_task("wt-new", "wpair", "done", datetime(2026, 1, 1, 12, 0, 0))
    r = _run_wait("wpair")
    assert_eq(r.returncode, 0, "exit 0 (latest task is done)")
    assert_true("resolved pair 'wpair'" in (r.stderr or ""), "stderr shows name resolution")
    assert_true("wt-new" in (r.stderr or ""), "resolved to the LATEST task (wt-new)")


def test_wait_by_pair_name_failed():
    print("\n=== wait.py <pair_name> → exit 1 when latest task failed ===")
    _write_task("wf1", "failpair", "failed", datetime(2026, 1, 1, 9, 0, 0))
    r = _run_wait("failpair")
    assert_eq(r.returncode, 1, "exit 1 (work failure)")


def test_wait_unknown_name():
    print("\n=== wait.py <unknown> → exit 2 not-found ===")
    r = _run_wait("no-such-pair-or-task")
    assert_eq(r.returncode, 2, "exit 2 not-found")
    assert_true("no task id or pair named" in (r.stderr or ""), "clear not-found message")


def test_exact_task_id_still_works():
    print("\n=== wait.py <exact task id> still works (no regression) ===")
    _write_task("exactid97", "wpair", "done", datetime(2026, 1, 1, 8, 0, 0))
    r = _run_wait("exactid97")
    assert_eq(r.returncode, 0, "exact id exits 0")
    assert_true("resolved pair" not in (r.stderr or ""), "no spurious resolution for exact id")


def test_handle_watcher_uses_name():
    print("\n=== async-handle Bash watcher command uses the pair name ===")
    out = _format_async_handle("abc-uuid-task", "Started.", pair_name="scout")
    # The Bash watcher line should now reference 'scout', not the raw uuid
    watcher_line = [ln for ln in out.splitlines() if "wait.py" in ln or "claude_squared wait" in ln]
    assert_true(len(watcher_line) == 1, "exactly one watcher line")
    assert_true("scout" in watcher_line[0], "watcher command uses pair name")
    # The exact task id is still printed (on the Async task: line) for targeting older tasks
    assert_true("abc-uuid-task" in out, "exact task id still shown in handle")


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    test_wait_by_pair_name_done()
    test_wait_by_pair_name_failed()
    test_wait_unknown_name()
    test_exact_task_id_still_works()
    test_handle_watcher_uses_name()
    print("\n" + "=" * 60)
    print("PASS: all v0.9.7 smoke checks passed")


if __name__ == "__main__":
    main()
