"""v0.9.9 smoke: wait.py + python -m claude_squared wait accept task-id prefixes.

Also brings ``_cmd_wait`` up to v0.9.8 exit-code parity (stopped=5, crashed=6,
orphaned=4) which was missed when we updated only the standalone wait.py.

Run:
    python -u tests/smoke_v099.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v099_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_squared import async_tasks
from claude_squared._wait_script import install_wait_script
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


def _write_task(task_id, pair_name, status, error=None):
    async_dir().mkdir(parents=True, exist_ok=True)
    async_tasks._save(AsyncTaskState(
        task_id=task_id, pair_name=pair_name, message="m",
        status=status, started_at=datetime.utcnow(), error=error,
        owner_pid=os.getpid(),
    ))


def _run_waitpy(arg, timeout=10):
    """Run the standalone wait.py script."""
    wp = install_wait_script()
    env = dict(os.environ)
    env["CLAUDE_HOME"] = _TMPDIR
    return subprocess.run(
        [sys.executable, str(wp), arg, "--timeout", "5", "--poll", "0.5"],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def _run_cmdwait(arg, timeout=10):
    """Run python -m claude_squared wait <arg>."""
    env = dict(os.environ)
    env["CLAUDE_HOME"] = _TMPDIR
    env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
    return subprocess.run(
        [sys.executable, "-m", "claude_squared", "wait", arg,
         "--timeout", "5", "--poll", "0.5"],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


# ---------- standalone wait.py prefix resolution ----------

def test_waitpy_resolves_unique_prefix():
    print("=== 1.1: wait.py accepts a unique task-id prefix ===")
    _write_task("a1b2c3d4-5678-uniqueprefix-task", "p1", "done")
    r = _run_waitpy("a1b2c3d4")  # 8-char prefix
    assert_eq(r.returncode, 0, "exit 0 (prefix resolved to a done task)")
    assert_true("resolved prefix" in (r.stderr or ""), "stderr names the resolution")
    assert_true("a1b2c3d4" in (r.stderr or ""), "stderr includes original prefix")


def test_waitpy_ambiguous_prefix_exits_2():
    print("\n=== 1.2: wait.py ambiguous prefix → exit 2 with clear message ===")
    _write_task("ambig-aaaa-1111-22220000-task", "p2", "done")
    _write_task("ambig-bbbb-2222-33330000-task", "p2", "done")
    r = _run_waitpy("ambig-")
    assert_eq(r.returncode, 2, "exit 2 on ambiguous prefix (same as not-found)")
    assert_true("ambiguous" in (r.stderr or "").lower(), "stderr says 'ambiguous'")
    assert_true("2 tasks" in (r.stderr or ""), "stderr shows match count")


def test_waitpy_exact_id_still_works():
    print("\n=== 1.3: wait.py exact task id still works (no regression) ===")
    _write_task("exactid99-fffffffffffffffffffffff", "p3", "done")
    r = _run_waitpy("exactid99-fffffffffffffffffffffff")
    assert_eq(r.returncode, 0, "exact id exits 0")
    assert_true(
        "resolved prefix" not in (r.stderr or ""),
        "no spurious prefix-resolution message for an exact id",
    )


def test_waitpy_pair_name_still_works():
    print("\n=== 1.4: wait.py pair name still works (no regression) ===")
    _write_task("byname-task-id-99", "by_name_pair", "done")
    r = _run_waitpy("by_name_pair")
    assert_eq(r.returncode, 0, "pair name resolves to latest task")
    assert_true("resolved pair" in (r.stderr or ""), "stderr says 'resolved pair'")


def test_waitpy_unknown_arg_clear_message():
    print("\n=== 1.5: wait.py unknown arg → exit 2 with new clearer message ===")
    r = _run_waitpy("absolutely-not-a-thing")
    assert_eq(r.returncode, 2, "exit 2 not-found")
    msg = r.stderr or ""
    # New message names all three resolution paths.
    assert_true("task id" in msg or "prefix" in msg or "pair name" in msg,
                "stderr names the resolution paths")


# ---------- python -m claude_squared wait (in-package) ----------

def test_cmdwait_resolves_unique_prefix():
    print("\n=== 2.1: `python -m claude_squared wait` accepts unique prefix ===")
    _write_task("cmdpref-aaa-bbb-ccc-task-id", "cp1", "done")
    r = _run_cmdwait("cmdpref-")
    assert_eq(r.returncode, 0, "exit 0 (prefix resolved)")
    assert_true("resolved prefix" in (r.stderr or ""), "stderr names the resolution")


def test_cmdwait_stopped_exits_5():
    print("\n=== 2.2: _cmd_wait status=stopped → exit 5 (v0.9.8 parity) ===")
    _write_task("cmdstop-task-id-1", "cp2", "stopped", error="stopped by pair_stop")
    r = _run_cmdwait("cmdstop-task-id-1")
    assert_eq(r.returncode, 5, "exit 5 (deliberate stop, not work error)")


def test_cmdwait_crashed_exits_6():
    print("\n=== 2.3: _cmd_wait CRASHED prefix → exit 6 (v0.9.8 parity) ===")
    _write_task(
        "cmdcrash-task-id-1", "cp3", "failed",
        error="CRASHED: pair runtime exited mid-turn (exit 3221225477)",
    )
    r = _run_cmdwait("cmdcrash-task-id-1")
    assert_eq(r.returncode, 6, "exit 6 (claude.exe died, NOT a work error)")


def test_cmdwait_orphaned_exits_4():
    print("\n=== 2.4: _cmd_wait ORPHANED prefix → exit 4 (v0.9.8 parity) ===")
    _write_task(
        "cmdorph-task-id-1", "cp4", "failed",
        error="ORPHANED: server died",
    )
    r = _run_cmdwait("cmdorph-task-id-1")
    assert_eq(r.returncode, 4, "exit 4 (supervision event, not work error)")


def test_cmdwait_generic_failed_still_exits_1():
    print("\n=== 2.5: _cmd_wait generic failed → exit 1 (no regression) ===")
    _write_task(
        "cmdgen-task-id-1", "cp5", "failed",
        error="ValueError: boom",
    )
    r = _run_cmdwait("cmdgen-task-id-1")
    assert_eq(r.returncode, 1, "exit 1 (generic work error)")


def test_cmdwait_ambiguous_prefix_exits_2():
    print("\n=== 2.6: _cmd_wait ambiguous prefix → exit 2 with clear message ===")
    _write_task("amb2-aaa-task-id-1", "cp6", "done")
    _write_task("amb2-bbb-task-id-2", "cp6", "done")
    r = _run_cmdwait("amb2-")
    assert_eq(r.returncode, 2, "exit 2 on ambiguous prefix")
    assert_true("ambiguous" in (r.stderr or "").lower(), "stderr says 'ambiguous'")


# ---------- helper tests ----------

def test_find_tasks_by_prefix_helper():
    print("\n=== 3.1: _find_tasks_by_prefix returns all matches (used internally) ===")
    # Note: this exercises the stdlib mirror inside _wait_script.py source. We
    # can't import the inner function directly (it's a string template), so we
    # check by behavior via the wait.py subprocess above. This test just
    # exercises async_tasks.find_task_by_prefix to ensure the in-package
    # equivalent works for _cmd_wait.
    _write_task("multi-aa-1", "mp", "done")
    _write_task("multi-aa-2", "mp", "done")
    _write_task("multi-bb-1", "mp", "done")
    out = async_tasks.find_task_by_prefix("multi-aa")
    assert_eq(sorted(out), ["multi-aa-1", "multi-aa-2"], "prefix matches both aa-* tasks")
    out_bb = async_tasks.find_task_by_prefix("multi-bb")
    assert_eq(out_bb, ["multi-bb-1"], "prefix matches single bb-* task")
    out_empty = async_tasks.find_task_by_prefix("")
    assert_eq(out_empty, [], "empty prefix returns [] (no accidental match-all)")


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    # Standalone wait.py
    test_waitpy_resolves_unique_prefix()
    test_waitpy_ambiguous_prefix_exits_2()
    test_waitpy_exact_id_still_works()
    test_waitpy_pair_name_still_works()
    test_waitpy_unknown_arg_clear_message()
    # In-package python -m claude_squared wait
    test_cmdwait_resolves_unique_prefix()
    test_cmdwait_stopped_exits_5()
    test_cmdwait_crashed_exits_6()
    test_cmdwait_orphaned_exits_4()
    test_cmdwait_generic_failed_still_exits_1()
    test_cmdwait_ambiguous_prefix_exits_2()
    # Helper
    test_find_tasks_by_prefix_helper()

    print("\n" + "=" * 60)
    print("PASS: all v0.9.9 smoke checks passed")


if __name__ == "__main__":
    main()
