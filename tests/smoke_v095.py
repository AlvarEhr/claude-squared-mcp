"""v0.9.5 smoke: orphan-task detection (owner MCP server died mid-turn).

Pure unit tests — no live `claude` CLI subprocess. Run:
    python -u tests/smoke_v095.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v095_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_squared import async_tasks
from claude_squared.async_tasks import ORPHAN_ERROR_PREFIX, _ORPHAN_MESSAGE, reap_orphan
from claude_squared.models import AsyncTaskState
from claude_squared.registry import async_dir


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


_DEAD_PID = 999_999_998  # almost certainly not a live process
_LIVE_PID = os.getpid()  # definitely alive (this test process)


def _write_task(task_id, status, owner_pid):
    async_dir().mkdir(parents=True, exist_ok=True)
    st = AsyncTaskState(
        task_id=task_id, pair_name="t", message="m", status=status,
        started_at=__import__("datetime").datetime.utcnow(), owner_pid=owner_pid,
    )
    async_tasks._save(st)


def test_reap_dead_owner():
    print("=== reap_orphan: running task with dead owner → orphaned ===")
    _write_task("orph1", "running", _DEAD_PID)
    st = reap_orphan("orph1")
    assert_eq(st.status, "failed", "dead-owner running task flipped to failed")
    assert_true(st.error.startswith(ORPHAN_ERROR_PREFIX), "error carries ORPHAN prefix")
    assert_true(st.finished_at is not None, "finished_at set")
    # Persisted to disk
    reloaded = async_tasks.load_task("orph1")
    assert_eq(reloaded.status, "failed", "orphan state persisted")


def test_live_owner_not_reaped():
    print("\n=== reap_orphan: running task with LIVE owner → untouched ===")
    _write_task("live1", "running", _LIVE_PID)
    st = reap_orphan("live1")
    assert_eq(st.status, "running", "live-owner task left running")


def test_done_task_not_reaped():
    print("\n=== reap_orphan: already-terminal task → untouched ===")
    _write_task("done1", "done", _DEAD_PID)
    st = reap_orphan("done1")
    assert_eq(st.status, "done", "done task not flipped even with dead owner")


def test_missing_owner_not_reaped_at_runtime():
    print("\n=== reap_orphan: running task with no owner_pid → left for startup sweep ===")
    _write_task("noowner", "running", None)
    st = reap_orphan("noowner")
    assert_eq(st.status, "running", "no-owner task not reaped at runtime")


def test_unknown_task():
    print("\n=== reap_orphan: unknown task_id → None ===")
    assert_eq(reap_orphan("does-not-exist"), None, "unknown task returns None")


def test_poll_renders_orphan_distinctly():
    print("\n=== pair_poll renders orphan as supervision event, not 'failed:' ===")
    from claude_squared.server import pair_poll
    _write_task("orph2", "running", _DEAD_PID)
    # pair_poll resolves, reaps, and renders
    fn = pair_poll.fn if hasattr(pair_poll, "fn") else pair_poll
    out = fn(task_id="orph2")
    assert_true("ORPHANED" in out, "poll output flags ORPHANED")
    assert_true("NOT a work failure" in out, "poll explains it's not a work failure")
    assert_true(not out.startswith("failed:"), "poll does NOT lead with 'failed:'")


def test_wait_script_orphan_exit():
    print("\n=== standalone wait.py: dead owner → exit 4 (orphaned) ===")
    from claude_squared._wait_script import install_wait_script
    wp = install_wait_script()
    _write_task("orph3", "running", _DEAD_PID)
    # Run the real installed wait.py against the dead-owner task in OUR CLAUDE_HOME
    env = dict(os.environ)
    env["CLAUDE_HOME"] = _TMPDIR
    r = subprocess.run([sys.executable, str(wp), "orph3", "--timeout", "10", "--poll", "1"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert_eq(r.returncode, 4, "wait.py exits 4 for orphaned (dead owner)")
    assert_true("orphaned" in (r.stderr or "").lower(), "wait.py stderr says orphaned")

    print("\n=== standalone wait.py: live owner + done → exit 0 ===")
    _write_task("done2", "running", _LIVE_PID)
    # Flip to done after a beat by writing done state
    _write_task("done2", "done", _LIVE_PID)
    r2 = subprocess.run([sys.executable, str(wp), "done2", "--timeout", "10", "--poll", "1"],
                        capture_output=True, text=True, env=env, timeout=30)
    assert_eq(r2.returncode, 0, "wait.py exits 0 for done")


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    test_reap_dead_owner()
    test_live_owner_not_reaped()
    test_done_task_not_reaped()
    test_missing_owner_not_reaped_at_runtime()
    test_unknown_task()
    test_poll_renders_orphan_distinctly()
    test_wait_script_orphan_exit()
    print("\n" + "=" * 60)
    print("PASS: all v0.9.5 smoke checks passed")


if __name__ == "__main__":
    main()
