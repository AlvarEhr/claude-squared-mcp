"""Cross-process pair lock + staleness smoke tests.

Verifies:
1. _PairLock acquire from the same process is fine (re-entrant via in-process Lock).
2. A subprocess holding the file lock blocks the main process until released.
3. PairRuntime.is_stale() returns True when JSONL mtime advances after a recorded send.
4. ToolCounter.reload() picks up cross-process index growth.

Run:
    python -u tests/smoke_crossproc_lock.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_squared.runtime import PairRuntime, ToolCounter  # noqa: E402
from claude_squared.models import PairSpec  # noqa: E402
from claude_squared.server import _PairLock, _with_pair_lock  # noqa: E402
from filelock import Timeout as FileLockTimeout  # noqa: E402


def _section(label):
    print(f"\n=== {label} ===")


def _check(condition, label):
    marker = "PASS" if condition else "FAIL"
    print(f"  [{marker}] {label}")
    return 0 if condition else 1


def test_lock_basic():
    _section("basic lock acquire/release")
    failures = 0
    with _PairLock("smoke_basic", timeout_s=2.0):
        failures += _check(True, "acquired lock in same process")
    failures += _check(True, "released lock cleanly")
    return failures


def test_lock_crossproc():
    """Spawn a subprocess that holds the lock for 4s; main acquires AFTER it releases.

    Validates: the file lock genuinely blocks across process boundaries.
    """
    _section("cross-process lock contention")
    failures = 0

    # Subprocess that acquires the lock and sleeps
    holder_script = f"""
import sys, time
sys.path.insert(0, r'{ROOT / "src"}')
from claude_squared.server import _PairLock
print('HOLDER_ACQUIRING', flush=True)
with _PairLock('smoke_crossproc', timeout_s=10.0):
    print('HOLDER_HELD', flush=True)
    time.sleep(4)
print('HOLDER_RELEASED', flush=True)
"""
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", holder_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Wait for the holder to actually have the lock
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if "HOLDER_HELD" in line:
            break
    else:
        proc.kill()
        return _check(False, "holder failed to acquire within 5s")

    # Now main process attempts to acquire with a SHORT timeout — should fail
    t0 = time.monotonic()
    short_timeout_failed = False
    try:
        with _PairLock("smoke_crossproc", timeout_s=1.5):
            pass
    except FileLockTimeout:
        short_timeout_failed = True
    elapsed_short = time.monotonic() - t0
    failures += _check(
        short_timeout_failed and 1.0 <= elapsed_short <= 3.0,
        f"short-timeout acquire failed as expected ({elapsed_short:.2f}s)",
    )

    # Now wait for holder to finish + acquire with longer timeout — should succeed
    t0 = time.monotonic()
    acquired = False
    try:
        with _PairLock("smoke_crossproc", timeout_s=10.0):
            acquired = True
    except FileLockTimeout:
        pass
    elapsed_long = time.monotonic() - t0
    proc.wait(timeout=2)
    failures += _check(
        acquired and elapsed_long <= 6.0,
        f"acquired after holder released ({elapsed_long:.2f}s, total)",
    )
    return failures


def test_runtime_is_stale():
    _section("PairRuntime.is_stale() detects external JSONL writes")
    failures = 0
    # Construct a PairRuntime against a synthetic spec; don't actually start the subprocess
    with tempfile.TemporaryDirectory() as d:
        d_path = Path(d)
        # We need a spec.cwd that maps to a project dir we control; trick: use the temp dir as cwd
        # and create a fake JSONL where the runtime expects it.
        spec = PairSpec(name="smoke_stale", session_id="00000000-0000-0000-0000-000000000001",
                        cwd=str(d_path))
        # We don't call rt.start() — just need to construct so _jsonl_path is set.
        # But PairRuntime needs a ClaudeAdapter — pass a dummy.

        class _DummyAdapter:
            pass

        # The PairRuntime __init__ touches log_dir; let it.
        rt = PairRuntime(spec, _DummyAdapter())

        # Manually plant a JSONL at the expected path
        jsonl_path = rt._jsonl_path
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_path.write_text('{"type":"system","subtype":"init"}\n', encoding="utf-8")

        # First send: _last_seen_jsonl_mtime is None, is_stale() returns False
        failures += _check(rt.is_stale() is False, "is_stale() False on first send")

        # Simulate a send: record current mtime
        rt._last_seen_jsonl_mtime = jsonl_path.stat().st_mtime

        # Right after, is_stale() should still be False (nothing changed)
        failures += _check(rt.is_stale() is False, "is_stale() False immediately after recording mtime")

        # Now another process writes — bump mtime
        time.sleep(0.05)  # ensure resolution gap
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write('{"type":"assistant","message":{}}\n')
        # is_stale() should now return True
        failures += _check(rt.is_stale() is True, "is_stale() True after external write")

    return failures


def test_toolcounter_reload():
    _section("ToolCounter.reload() picks up cross-process index growth")
    failures = 0
    with tempfile.TemporaryDirectory() as d:
        idx = Path(d) / "main.idx.json"
        idx.write_text(json.dumps({
            "T-1": {"tool_use_id": "toolu_a", "tool_name": "Read", "ts": "00:00:00"},
        }), encoding="utf-8")
        tc = ToolCounter(index_path=idx)
        failures += _check(tc.counter == 1, f"loaded counter=1 (got {tc.counter})")
        failures += _check(tc.id_map == {"toolu_a": 1}, f"loaded id_map (got {tc.id_map})")

        # Simulate other process growing the index
        idx.write_text(json.dumps({
            "T-1": {"tool_use_id": "toolu_a", "tool_name": "Read", "ts": "00:00:00"},
            "T-2": {"tool_use_id": "toolu_b", "tool_name": "Edit", "ts": "00:00:00"},
            "T-5": {"tool_use_id": "toolu_c", "tool_name": "Bash", "ts": "00:00:00"},
        }), encoding="utf-8")

        # Without reload, in-memory state is still T-1 only — would assign T-2 next (collision)
        failures += _check(tc.counter == 1, "pre-reload counter still 1 (proves staleness)")
        tc.reload()
        failures += _check(tc.counter == 5, f"post-reload counter=5 (got {tc.counter})")
        failures += _check(
            tc.id_map == {"toolu_a": 1, "toolu_b": 2, "toolu_c": 5},
            f"post-reload id_map (got {tc.id_map})",
        )
    return failures


def main() -> int:
    suites = [
        test_lock_basic,
        test_runtime_is_stale,
        test_toolcounter_reload,
        test_lock_crossproc,  # slow (~6s), put last
    ]
    total = 0
    for s in suites:
        total += s()
    print()
    if total:
        print(f"FAIL: {total} assertion(s) failed")
        return 1
    print("PASS: all cross-process correctness checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
