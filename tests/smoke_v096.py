"""v0.9.6 smoke: pair_poll accepts a pair name (resolves to latest task).

Pure unit tests — no live `claude` CLI subprocess. Run:
    python -u tests/smoke_v096.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v096_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_squared import async_tasks
from claude_squared import registry as reg_mod
from claude_squared.async_tasks import latest_task_id_for_pair
from claude_squared.models import AsyncTaskState, PairSpec
from claude_squared.registry import async_dir
from claude_squared.server import pair_poll

_poll = pair_poll.fn if hasattr(pair_poll, "fn") else pair_poll


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


def _write_task(task_id, pair_name, status, started_at, owner_pid=None):
    async_dir().mkdir(parents=True, exist_ok=True)
    st = AsyncTaskState(
        task_id=task_id, pair_name=pair_name, message="m", status=status,
        started_at=started_at, owner_pid=owner_pid or os.getpid(),
    )
    async_tasks._save(st)


def test_latest_task_resolver():
    print("=== latest_task_id_for_pair picks max started_at ===")
    _write_task("taskold", "pr", "done", datetime(2026, 1, 1, 10, 0, 0))
    _write_task("tasknew", "pr", "running", datetime(2026, 1, 1, 12, 0, 0))
    _write_task("taskother", "otherpair", "running", datetime(2026, 1, 1, 13, 0, 0))
    assert_eq(latest_task_id_for_pair("pr"), "tasknew", "latest task for 'pr' is t-new")
    assert_eq(latest_task_id_for_pair("otherpair"), "taskother", "other pair unaffected")
    assert_eq(latest_task_id_for_pair("nonexistent"), None, "no tasks → None")


def test_poll_by_pair_name():
    print("\n=== pair_poll('<pairname>') resolves to latest task ===")
    reg_mod.add_pair(PairSpec(name="pr", session_id="s", model="opus"))
    try:
        out = _poll(task_id="pr")
        # t-new is "running" → headline mentions running on pair 'pr'
        assert_true("running" in out and "pr" in out, "polls the latest (running) task")
        assert_true("resolved pair 'pr'" in out, "surfaces the resolution note")
        assert_true("tasknew" in out, "names the concrete task it picked")
    finally:
        reg_mod.remove_pair("pr")


def test_poll_pair_no_tasks():
    print("\n=== pair_poll('<pairname>') with no tasks → clear error ===")
    reg_mod.add_pair(PairSpec(name="freshpair", session_id="s2", model="opus"))
    try:
        try:
            _poll(task_id="freshpair")
            print("  [FAIL] expected PairError for pair with no tasks")
            sys.exit(1)
        except Exception as e:
            assert_true("no async tasks yet" in str(e), "clear 'no tasks yet' error")
    finally:
        reg_mod.remove_pair("freshpair")


def test_exact_task_id_still_works():
    print("\n=== exact task_id still resolves directly (no regression) ===")
    _write_task("exacttid123", "pr", "done", datetime(2026, 1, 1, 9, 0, 0))
    # done with no result → "done (no result captured)"
    out = _poll(task_id="exacttid123")
    assert_true("done" in out, "exact task_id polled directly")
    assert_true("resolved pair" not in out, "no spurious resolution note for exact id")


def test_unknown_ref_error():
    print("\n=== unknown ref → error mentions both task and pair ===")
    try:
        _poll(task_id="totally-unknown-xyz")
        print("  [FAIL] expected PairError")
        sys.exit(1)
    except Exception as e:
        assert_true("No async task or pair named" in str(e), "error names both options")


def test_async_handle_uses_pair_name():
    print("\n=== _format_async_handle shows pair name in poll hints ===")
    from claude_squared.server import _format_async_handle
    out = _format_async_handle("abc-123-task", "Started.", pair_name="reviewer")
    assert_true("pair_poll('reviewer')" in out, "poll hint uses pair name")
    assert_true("abc-123-task" in out, "exact task id still shown (for the Bash watcher)")
    # Without pair_name, falls back to task id (backward compat)
    out2 = _format_async_handle("abc-123-task", "Started.")
    assert_true("pair_poll('abc-123-task')" in out2, "fallback to task id when no name")


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    test_latest_task_resolver()
    test_poll_by_pair_name()
    test_poll_pair_no_tasks()
    test_exact_task_id_still_works()
    test_unknown_ref_error()
    test_async_handle_uses_pair_name()
    print("\n" + "=" * 60)
    print("PASS: all v0.9.6 smoke checks passed")


if __name__ == "__main__":
    main()
