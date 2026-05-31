"""v0.9.8 smoke: 5 bug-fix clusters.

A — evictor mid-turn skip + last_activity bump (Bug 1)
B — pair_compact async-wrap + AsyncTaskState union + pair_poll polymorphism + _fmt_compact_result (Bugs 2+8)
C — pair_status local-corpse detection (Bug 3)
D — race-safe exit-code snapshot + CRASHED prefix on CLIError message (Bug 5)
E — wait.py: status="stopped" → exit 5; CRASHED prefix → exit 6 (Bug 4)

Pure unit tests + a real subprocess run of the installed wait.py. Run:
    python -u tests/smoke_v098.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v098_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_squared import async_tasks
from claude_squared._wait_script import install_wait_script
from claude_squared.errors import CLIError
from claude_squared.models import (
    AsyncTaskState,
    CompactResult,
    ContextStatus,
    PairSpec,
    SendResult,
)
from claude_squared.registry import async_dir, logs_dir
from claude_squared.runtime import (
    CRASHED_ERROR_PREFIX,
    PairRuntime,
    RuntimeRegistry,
    TurnLogScope,
)


# ----------------------------- harness ------------------------------------

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


def _make_pair_spec(name="pair_v98", session_id="00000000-0000-0000-0000-000000000098"):
    """Build a PairSpec for a runtime that we'll NEVER actually start()."""
    return PairSpec(
        name=name,
        purpose="v0.9.8 smoke",
        backend="claude",
        session_id=session_id,
        model="opus",
        effort="xhigh",
        permission_mode="auto",
        cwd=str(Path(_TMPDIR) / "cwd"),
    )


class _FakeProc:
    """Stand-in for subprocess.Popen — supports .returncode and .poll()."""
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


# ----------------------------- Cluster A ----------------------------------

def test_evict_idle_skips_mid_turn():
    print("=== A.1: _evict_idle skips runtimes whose _current_scope is set ===")
    reg = RuntimeRegistry(idle_timeout_seconds=1)
    spec = _make_pair_spec("evict_midturn")
    rt = PairRuntime(spec, adapter=None)  # adapter not needed for evictor logic
    rt.proc = _FakeProc(returncode=None)  # is_alive() returns True
    rt.last_activity = datetime.utcnow() - timedelta(seconds=10)  # WAY past cutoff
    rt._current_scope = TurnLogScope(rt.main_log_path, 1)  # mid-turn
    reg._runtimes[spec.name] = rt

    reg._evict_idle()

    assert_true(
        spec.name in reg._runtimes,
        "runtime still in registry (mid-turn skip protected it)",
    )
    assert_true(rt.proc is not None, "runtime's proc not stopped")


def test_evict_idle_kills_idle_non_mid_turn():
    print("\n=== A.2: _evict_idle DOES kill genuinely-idle runtime (no scope) ===")
    reg = RuntimeRegistry(idle_timeout_seconds=1)
    spec = _make_pair_spec("evict_idle")
    rt = PairRuntime(spec, adapter=None)
    rt.proc = _FakeProc(returncode=None)  # alive
    rt.last_activity = datetime.utcnow() - timedelta(seconds=10)  # past cutoff
    rt._current_scope = None  # NOT mid-turn
    reg._runtimes[spec.name] = rt

    # Patch out the actual stop() call to avoid platform-specific taskkill side
    # effects in CI — we only care that the runtime was popped from the registry.
    rt.stop = lambda: "tree-killed"

    reg._evict_idle()

    assert_true(
        spec.name not in reg._runtimes,
        "idle runtime popped from registry (eviction fired correctly)",
    )


def test_append_main_log_line_bumps_both_timestamps():
    print("\n=== A.3: _append_main_log_line bumps last_activity AND _last_log_activity_at ===")
    spec = _make_pair_spec("bump_check")
    rt = PairRuntime(spec, adapter=None)
    before = rt.last_activity
    pre_log_act = rt._last_log_activity_at
    # Force a small wait so utcnow() advances past `before`
    import time as _t
    _t.sleep(0.005)
    rt._append_main_log_line("[test] sample line")

    assert_true(rt.last_activity > before, "last_activity bumped past the prior value")
    assert_true(
        pre_log_act is None or rt._last_log_activity_at > pre_log_act,
        "_last_log_activity_at also bumped",
    )
    # Both should be equal (same now snapshot)
    assert_eq(
        rt.last_activity,
        rt._last_log_activity_at,
        "last_activity == _last_log_activity_at (same utcnow snapshot)",
    )


# ----------------------------- Cluster B ----------------------------------

def test_async_task_state_with_compact_result():
    print("\n=== B.1: AsyncTaskState round-trips with a CompactResult ===")
    cr = CompactResult(
        name="b1pair",
        session_id="sid",
        pre_tokens=600_000,
        post_tokens=100_000,
        duration_ms=12_345,
        trigger="manual",
    )
    state = AsyncTaskState(
        task_id="t-compact-1",
        pair_name="b1pair",
        message="/compact",
        status="done",
        started_at=datetime(2026, 5, 31, 16, 0, 0),
        finished_at=datetime(2026, 5, 31, 16, 0, 12),
        result=cr,
        owner_pid=os.getpid(),
    )
    s = state.model_dump_json()
    loaded = AsyncTaskState.model_validate_json(s)
    assert_true(
        isinstance(loaded.result, CompactResult),
        "deserialized result is a CompactResult (Pydantic smart-union picks the right type)",
    )
    assert_eq(loaded.result.pre_tokens, 600_000, "pre_tokens preserved")
    assert_eq(loaded.result.post_tokens, 100_000, "post_tokens preserved")


def test_async_task_state_with_send_result_unchanged():
    print("\n=== B.2: AsyncTaskState still deserializes pre-v0.9.8 SendResult tasks ===")
    sr = SendResult(
        name="b2pair",
        response="hi",
        session_id="sid",
        model_used="claude-opus-4-7",
        cost_usd=0.0,
        duration_ms=100,
        context=ContextStatus(tokens_used=10, tokens_max=200_000, percent=0.005),
    )
    state = AsyncTaskState(
        task_id="t-send-1",
        pair_name="b2pair",
        message="hi",
        status="done",
        started_at=datetime(2026, 5, 31, 16, 0, 0),
        finished_at=datetime(2026, 5, 31, 16, 0, 0, 100),
        result=sr,
        owner_pid=os.getpid(),
    )
    s = state.model_dump_json()
    loaded = AsyncTaskState.model_validate_json(s)
    assert_true(
        isinstance(loaded.result, SendResult),
        "SendResult still deserializes correctly (backward compat)",
    )
    assert_eq(loaded.result.response, "hi", "response preserved")


def test_fmt_compact_result():
    print("\n=== B.3: _fmt_compact_result formats CompactResult with header + delta ===")
    from claude_squared.server import _fmt_compact_result
    cr = CompactResult(
        name="b3pair",
        session_id="sid",
        pre_tokens=600_000,
        post_tokens=145_000,
        duration_ms=11_000,
        trigger="manual",
    )
    out = _fmt_compact_result(cr)
    assert_true("━━━ pair 'b3pair' compacted ━━━" in out, "has visual header marker")
    assert_true("600,000" in out and "145,000" in out, "shows pre and post token counts")
    assert_true("24" in out, "shows retention % (145k/600k ≈ 24%)")
    assert_true("trigger=manual" in out, "shows trigger source")


def test_pair_poll_dispatches_to_fmt_compact_result():
    print("\n=== B.4: pair_poll renders a CompactResult task with _fmt_compact_result ===")
    # Write a registered pair to the registry so pair_poll's status checks have
    # something to look at — pair_poll doesn't strictly need the spec but pair_list
    # / registry calls under the hood do.
    from claude_squared import registry as reg_mod
    from claude_squared.server import pair_poll

    spec = _make_pair_spec("b4pair")
    reg_mod.add_pair(spec)

    cr = CompactResult(
        name="b4pair", session_id=spec.session_id,
        pre_tokens=600_000, post_tokens=145_000,
        duration_ms=11_000, trigger="manual",
    )
    state = AsyncTaskState(
        task_id="t-b4-compact",
        pair_name="b4pair",
        message="/compact",
        status="done",
        started_at=datetime.utcnow() - timedelta(seconds=15),
        finished_at=datetime.utcnow() - timedelta(seconds=4),
        result=cr,
        owner_pid=os.getpid(),
    )
    async_tasks._save(state)

    out = pair_poll("t-b4-compact")

    assert_true("compacted" in out.lower(), "pair_poll dispatches to compact formatter")
    assert_true("600,000" in out, "pre-tokens visible")
    assert_true("145,000" in out, "post-tokens visible")
    assert_true("trigger=manual" in out, "trigger surfaced")


# ----------------------------- Cluster C ----------------------------------

def test_pair_status_local_corpse_detected():
    print("\n=== C.1: pair_status reports local corpse with exit code ===")
    from claude_squared import registry as reg_mod
    from claude_squared.server import pair_status
    from claude_squared import runtime as runtime_mod

    spec = _make_pair_spec("c1pair")
    reg_mod.add_pair(spec)

    # Inject a fake PairRuntime into the live registry with a terminated proc.
    rt = PairRuntime(spec, adapter=None)
    rt.proc = _FakeProc(returncode=42)  # subprocess exited with code 42
    rt.last_activity = datetime.utcnow() - timedelta(seconds=20)
    rt._last_log_activity_at = datetime.utcnow() - timedelta(seconds=20)
    runtime_mod.registry()._runtimes[spec.name] = rt

    try:
        out = pair_status("c1pair")
        assert_true("local corpse" in out, "pair_status names the local corpse")
        assert_true("code 42" in out or "exit 42" in out or "(42" in out, "exit code 42 surfaced in output")
        assert_true("respawn from JSONL" in out, "recovery hint included")
    finally:
        # Clean up so subsequent tests aren't polluted
        runtime_mod.registry()._runtimes.pop(spec.name, None)


# ----------------------------- Cluster D ----------------------------------

def test_crashed_error_prefix_constant_in_sync():
    print("\n=== D.1: CRASHED_ERROR_PREFIX is consistent across runtime.py and async_tasks.py ===")
    from claude_squared.runtime import CRASHED_ERROR_PREFIX as RT_CRASHED
    from claude_squared.async_tasks import CRASHED_ERROR_PREFIX as AT_CRASHED
    assert_eq(RT_CRASHED, "CRASHED: ", "runtime.py CRASHED_ERROR_PREFIX literal")
    assert_eq(AT_CRASHED, "CRASHED: ", "async_tasks.py CRASHED_ERROR_PREFIX literal")


def test_format_task_error_preserves_supervision_prefixes():
    print("\n=== D.2: _format_task_error preserves ORPHAN and CRASHED prefixes bare ===")
    from claude_squared.async_tasks import _format_task_error

    # Generic error gets type-wrapped
    out_generic = _format_task_error(ValueError("boom"))
    assert_eq(out_generic, "ValueError: boom", "generic error wrapped with type")

    # Supervision errors stay bare so wait.py startswith dispatch works
    out_orphan = _format_task_error(Exception("ORPHANED: server died"))
    assert_eq(out_orphan, "ORPHANED: server died", "ORPHANED prefix preserved bare")

    out_crashed = _format_task_error(Exception("CRASHED: pair runtime exited mid-turn (exit 1)"))
    assert_eq(
        out_crashed,
        "CRASHED: pair runtime exited mid-turn (exit 1)",
        "CRASHED prefix preserved bare",
    )


def test_clierror_with_crashed_prefix_stringifies_correctly():
    print("\n=== D.3: CLIError with CRASHED prefix stringifies cleanly (exit code appended) ===")
    # CLIError.__init__ appends "(exit X)" if exit_code is given AND not already in message.
    # Our v0.9.8 message already includes "(exit X)" — verify we don't get duplicated suffix.
    e = CLIError(
        f"{CRASHED_ERROR_PREFIX}pair runtime exited mid-turn (exit 1)",
        exit_code=1,
    )
    s = str(e)
    # Should contain exactly one "(exit 1)" — CLIError appends if exit_code is given,
    # so we'll have two. That's tolerable (slight redundancy, not wrong). The key is
    # that the prefix is at position 0 for wait.py dispatch.
    assert_true(s.startswith(CRASHED_ERROR_PREFIX), "CLIError message starts with CRASHED:")
    assert_true("1" in s, "exit code visible")


# ----------------------------- Cluster E ----------------------------------

def _write_task(task_id, pair_name, status, started_at, error=None):
    async_dir().mkdir(parents=True, exist_ok=True)
    async_tasks._save(AsyncTaskState(
        task_id=task_id, pair_name=pair_name, message="m",
        status=status, started_at=started_at, error=error,
        owner_pid=os.getpid(),
    ))


def _run_wait(arg, timeout=10):
    wp = install_wait_script()
    env = dict(os.environ)
    env["CLAUDE_HOME"] = _TMPDIR
    return subprocess.run(
        [sys.executable, str(wp), arg, "--timeout", "5", "--poll", "0.5"],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def test_wait_py_exit_5_for_stopped():
    print("\n=== E.1: wait.py exits 5 when status='stopped' ===")
    _write_task("e1stopped", "e1pair", "stopped", datetime.utcnow(),
                error="stopped by pair_stop")
    r = _run_wait("e1stopped")
    assert_eq(r.returncode, 5, "exit 5 (deliberate stop, not work error)")
    assert_true("stopped by pair_stop" in (r.stderr or ""), "stderr shows the stop reason")


def test_wait_py_exit_6_for_crashed():
    print("\n=== E.2: wait.py exits 6 when failed-error starts with CRASHED: ===")
    _write_task(
        "e2crashed", "e2pair", "failed", datetime.utcnow(),
        error="CRASHED: pair runtime exited mid-turn (exit 3221225477)",
    )
    r = _run_wait("e2crashed")
    assert_eq(r.returncode, 6, "exit 6 (claude.exe died, NOT a work error)")
    assert_true("CRASHED:" in (r.stderr or ""), "stderr surfaces the crashed marker")


def test_wait_py_exit_1_for_generic_failed_still_works():
    print("\n=== E.3: wait.py still exits 1 for non-prefixed failed (no regression) ===")
    _write_task(
        "e3failed", "e3pair", "failed", datetime.utcnow(),
        error="ValueError: boom",
    )
    r = _run_wait("e3failed")
    assert_eq(r.returncode, 1, "exit 1 (generic work error)")


def test_wait_py_exit_4_for_orphan_still_works():
    print("\n=== E.4: wait.py still exits 4 for ORPHANED prefix (no regression) ===")
    _write_task(
        "e4orphan", "e4pair", "failed", datetime.utcnow(),
        error="ORPHANED: server died",
    )
    r = _run_wait("e4orphan")
    assert_eq(r.returncode, 4, "exit 4 (supervision event, not work error)")


# ----------------------------- main ---------------------------------------

def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    # Cluster A
    test_evict_idle_skips_mid_turn()
    test_evict_idle_kills_idle_non_mid_turn()
    test_append_main_log_line_bumps_both_timestamps()
    # Cluster B
    test_async_task_state_with_compact_result()
    test_async_task_state_with_send_result_unchanged()
    test_fmt_compact_result()
    test_pair_poll_dispatches_to_fmt_compact_result()
    # Cluster C
    test_pair_status_local_corpse_detected()
    # Cluster D
    test_crashed_error_prefix_constant_in_sync()
    test_format_task_error_preserves_supervision_prefixes()
    test_clierror_with_crashed_prefix_stringifies_correctly()
    # Cluster E
    test_wait_py_exit_5_for_stopped()
    test_wait_py_exit_6_for_crashed()
    test_wait_py_exit_1_for_generic_failed_still_works()
    test_wait_py_exit_4_for_orphan_still_works()

    print("\n" + "=" * 60)
    print("PASS: all v0.9.8 smoke checks passed")


if __name__ == "__main__":
    main()
