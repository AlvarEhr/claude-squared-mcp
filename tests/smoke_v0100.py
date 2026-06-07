"""v0.10.0 smoke: pair_fork + pair_rewind_points + pair_rewind + JSONL helpers.

Pure-Python (no real claude). The native --fork-session resume/rewrite behavior
was validated separately by an isolated integration probe; here we test OUR
logic: user-turn-point parsing (excludes tool_results), after-context, sentinel
finding, truncation (tail invariant), and the three tools end-to-end with a
synthetic registry + JSONL (fork uses a mocked adapter.fork).

Run:
    python -u tests/smoke_v0100.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v0100_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from datetime import datetime

from claude_squared import async_tasks
from claude_squared import registry as reg_mod
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.models import AsyncTaskState, PairSpec
from claude_squared.transcript import (
    find_sentinel_line,
    files_written_in_events,
    list_user_turn_points,
    truncate_jsonl_before_line,
)


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


def _ev(t, content=None, tool_use=None, tool_result=False, ts="2026-06-07T10:00:00Z"):
    """Build a minimal session-JSONL event."""
    msg = {}
    if tool_result:
        msg["content"] = [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]
    elif tool_use:
        msg["content"] = [{"type": "tool_use", "name": tool_use[0],
                           "input": {"file_path": tool_use[1]} if tool_use[1] else {}, "id": "tu"}]
    elif content is not None:
        msg["content"] = content  # str = user/assistant text
    return {"type": t, "message": msg, "timestamp": ts}


_SENTINEL = "__claude_squared_fork_init__ (auto-removed by pair_fork)"


def _write_synthetic(path: Path, include_sentinel=True):
    """A realistic small session: 2 real user turns (each with a file write),
    interleaved tool_result user events, optionally a sentinel fork turn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _ev("user", "first question"),                       # point 1
        _ev("assistant", "let me write that"),
        _ev("assistant", tool_use=("Write", "a.py")),
        _ev("user", tool_result=True),                       # NOT a point
        _ev("assistant", "done with a.py"),
        _ev("user", "second question"),                      # point 2
        _ev("assistant", tool_use=("Edit", "b.py")),
        _ev("user", tool_result=True),                       # NOT a point
        _ev("assistant", "done with b.py"),
    ]
    if include_sentinel:
        events += [
            _ev("user", _SENTINEL),                          # point 3 (fork sentinel)
            _ev("assistant", "ok"),
        ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return len(events)


# ----------------------------- helpers ------------------------------------

def test_user_turn_points_excludes_tool_results():
    print("=== 1.1: list_user_turn_points finds real user msgs, excludes tool_results ===")
    p = Path(_TMPDIR) / "syn1.jsonl"
    _write_synthetic(p)
    pts = list_user_turn_points(p)
    assert_eq(len(pts), 3, "3 points (2 real + sentinel; tool_results excluded)")
    assert_eq(pts[0]["preview"], "first question", "point 1 preview")
    assert_eq(pts[1]["preview"], "second question", "point 2 preview")
    assert_true(_SENTINEL in pts[2]["preview"], "point 3 is the sentinel")


def test_after_context_files():
    print("\n=== 1.2: after-context counts tool calls + files written ===")
    p = Path(_TMPDIR) / "syn2.jsonl"
    _write_synthetic(p)
    pts = list_user_turn_points(p)
    assert_true("a.py" in pts[0]["after_files"], "point 1 wrote a.py")
    assert_true("b.py" in pts[1]["after_files"], "point 2 wrote b.py")
    assert_true(pts[0]["after_tool_calls"] >= 1, "point 1 had >=1 tool call")


def test_find_sentinel_line():
    print("\n=== 1.3: find_sentinel_line locates the fork sentinel ===")
    p = Path(_TMPDIR) / "syn3.jsonl"
    _write_synthetic(p)
    line = find_sentinel_line(p, _SENTINEL)
    pts = list_user_turn_points(p)
    assert_eq(line, pts[2]["raw_line_index"], "sentinel line == point 3 raw_line_index")
    assert_true(find_sentinel_line(p, "no-such-sentinel") is None, "missing sentinel → None")


def test_truncate_and_dropped_files():
    print("\n=== 1.4: truncate_jsonl_before_line keeps [0,idx), returns dropped ===")
    p = Path(_TMPDIR) / "syn4.jsonl"
    _write_synthetic(p)
    pts = list_user_turn_points(p)
    # rewind to point 2 → drop point2's user msg + everything after
    dropped = truncate_jsonl_before_line(p, pts[1]["raw_line_index"])
    remaining = list_user_turn_points(p)
    assert_eq(len(remaining), 1, "only point 1 remains after truncation")
    files = files_written_in_events(dropped)
    assert_true("b.py" in files, "dropped range includes b.py write")
    assert_true("a.py" not in files, "a.py (kept range) NOT in dropped")
    # tail invariant: last retained event is an assistant turn, not a dangling tool_use
    last = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()][-1]
    assert_eq(last.get("type"), "assistant", "last retained event is an assistant turn (no dangling tool_use)")


def test_truncate_to_sentinel_for_fork():
    print("\n=== 1.5: truncating at the sentinel yields a clean fork at original tip ===")
    p = Path(_TMPDIR) / "syn5.jsonl"
    _write_synthetic(p, include_sentinel=True)
    line = find_sentinel_line(p, _SENTINEL)
    truncate_jsonl_before_line(p, line)
    pts = list_user_turn_points(p)
    assert_eq(len(pts), 2, "sentinel turn removed → back to 2 real points")
    assert_true(find_sentinel_line(p, _SENTINEL) is None, "sentinel gone after truncation")


# ----------------------------- rewind tool --------------------------------

def _make_pair(name, sid=None):
    cwd = str(Path(_TMPDIR) / "work")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    spec = PairSpec(name=name, session_id=sid or str(uuid.uuid4()), purpose="rw",
                    model="opus", effort="xhigh", permission_mode="auto",
                    cwd=cwd, turn_count=3)
    reg_mod.add_pair(spec)
    return spec


def test_pair_rewind_points_tool():
    print("\n=== 2.1: pair_rewind_points renders the numbered list ===")
    from claude_squared.server import pair_rewind_points
    spec = _make_pair("rwpair")
    path = ClaudeAdapter().transcript_path(spec)
    _write_synthetic(path, include_sentinel=False)
    out = pair_rewind_points("rwpair")
    assert_true("[1]" in out and "[2]" in out, "lists points 1 and 2")
    assert_true("first question" in out and "second question" in out, "shows previews")
    assert_true("file(s) written" in out or "a.py" in out or "tool call" in out, "shows after-context")


def test_pair_rewind_tool():
    print("\n=== 2.2: pair_rewind truncates + archives + resets turn_count + lists files ===")
    from claude_squared.server import pair_rewind
    spec = _make_pair("rwpair2")
    path = ClaudeAdapter().transcript_path(spec)
    _write_synthetic(path, include_sentinel=False)
    before = len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])
    out = pair_rewind("rwpair2", to_point=2)
    after = len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])
    assert_true(after < before, "JSONL shrank (events dropped)")
    assert_true("b.py" in out, "reports b.py written in dropped range")
    assert_true("archived" in out.lower(), "reports the safety archive")
    # archive file exists
    arch = list(reg_mod.archive_dir().glob("rwpair2-*-prerewind.jsonl"))
    assert_true(len(arch) == 1, "pre-rewind archive file created")
    # turn_count reset to point-1 = 1
    assert_eq(reg_mod.get_pair("rwpair2").turn_count, 1, "turn_count reset to to_point-1")


def test_pair_rewind_invalid_point():
    print("\n=== 2.3: pair_rewind on a bad point → clear error ===")
    from claude_squared.server import pair_rewind
    from claude_squared.errors import PairError
    spec = _make_pair("rwpair3")
    path = ClaudeAdapter().transcript_path(spec)
    _write_synthetic(path, include_sentinel=False)
    try:
        pair_rewind("rwpair3", to_point=99)
        print("  [FAIL] expected PairError for invalid point")
        sys.exit(1)
    except PairError as e:
        assert_true("No rewind point 99" in str(e), "names the invalid point + valid range")


# ----------------------------- fork tool (mocked adapter) -----------------

def test_pair_fork_tool():
    print("\n=== 3.1: pair_fork registers a new pair, truncates the sentinel, keeps both ===")
    from claude_squared.server import pair_fork
    import claude_squared.adapters.claude as cl

    spec = _make_pair("forkpair")
    src_path = ClaudeAdapter().transcript_path(spec)
    _write_synthetic(src_path, include_sentinel=False)  # source has 2 real turns

    # Mock adapter.fork: simulate --fork-session by copying source history to a
    # new sid path + appending the sentinel turn (what native fork would leave).
    def fake_fork(self, s, sentinel, timeout_seconds=300):
        new_sid = str(uuid.uuid4())
        new_path = self.transcript_path(s.model_copy(update={"session_id": new_sid}))
        new_path.parent.mkdir(parents=True, exist_ok=True)
        body = src_path.read_text(encoding="utf-8").rstrip("\n").splitlines()
        body.append(json.dumps(_ev("user", sentinel)))
        body.append(json.dumps(_ev("assistant", "ok")))
        new_path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return new_sid

    orig = cl.ClaudeAdapter.fork
    cl.ClaudeAdapter.fork = fake_fork
    try:
        out = pair_fork("forkpair")
    finally:
        cl.ClaudeAdapter.fork = orig

    assert_true("forkpair-fork" in out, "new pair named '<name>-fork'")
    fork_spec = reg_mod.get_pair("forkpair-fork")
    assert_true(fork_spec.session_id != spec.session_id, "fork has a NEW session id")
    # sentinel truncated off the fork's tip → back to 2 real points
    fpath = ClaudeAdapter().transcript_path(fork_spec)
    pts = list_user_turn_points(fpath)
    assert_eq(len(pts), 2, "fork JSONL has the 2 source turns, sentinel truncated")
    # source untouched
    assert_true(reg_mod.get_pair("forkpair").session_id == spec.session_id, "source session unchanged")


def test_pair_fork_collision_naming():
    print("\n=== 3.2: pair_fork name collision → -2 suffix ===")
    from claude_squared.server import pair_fork
    import claude_squared.adapters.claude as cl

    spec = _make_pair("dup")
    src_path = ClaudeAdapter().transcript_path(spec)
    _write_synthetic(src_path, include_sentinel=False)
    # pre-create "dup-fork" so the next fork must pick "dup-fork-2"
    _make_pair("dup-fork")

    def fake_fork(self, s, sentinel, timeout_seconds=300):
        new_sid = str(uuid.uuid4())
        np = self.transcript_path(s.model_copy(update={"session_id": new_sid}))
        np.parent.mkdir(parents=True, exist_ok=True)
        np.write_text(src_path.read_text(encoding="utf-8"), encoding="utf-8")
        return new_sid

    orig = cl.ClaudeAdapter.fork
    cl.ClaudeAdapter.fork = fake_fork
    try:
        out = pair_fork("dup")
    finally:
        cl.ClaudeAdapter.fork = orig
    assert_true("dup-fork-2" in out, "collision resolved to dup-fork-2")


# ----------------------------- terminal stop ------------------------------

def _running_task(name, tid):
    async_tasks._save(AsyncTaskState(
        task_id=tid, pair_name=name, message="m", status="running",
        started_at=datetime.utcnow(), owner_pid=os.getpid(),
    ))


def test_stop_checker_honors_fresh_marker():
    print("\n=== 4.1: _make_stop_checker honors a fresh marker, marks task, dedupes ===")
    from claude_squared.server import _make_stop_checker, _stop_marker_path
    import time
    _make_pair("stopA")
    _running_task("stopA", "tStopA")
    checker = _make_stop_checker("stopA", "tStopA")  # captures turn_start = now
    assert_true(checker() is False, "no marker yet → False")
    mp = _stop_marker_path("stopA")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"pair_name": "stopA", "requested_at": time.time() + 1}), encoding="utf-8")
    assert_true(checker() is True, "fresh marker (ts > turn_start) → True")
    assert_true(async_tasks._was_stopped("tStopA"), "task marked stopped")
    assert_true(not mp.exists(), "marker deleted (hygiene) after honor")
    # high-water: a new marker NEWER than turn_start but OLDER than the honored
    # one must NOT be re-honored.
    mp.write_text(json.dumps({"pair_name": "stopA", "requested_at": time.time() + 0.5}), encoding="utf-8")
    assert_true(checker() is False, "marker below high-water not re-honored (Crack B)")


def test_stop_checker_ignores_stale_marker():
    print("\n=== 4.2: _make_stop_checker ignores a marker older than turn-start ===")
    from claude_squared.server import _make_stop_checker, _stop_marker_path
    import time
    _make_pair("stopB")
    _running_task("stopB", "tStopB")
    mp = _stop_marker_path("stopB")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"pair_name": "stopB", "requested_at": time.time() - 100}), encoding="utf-8")
    checker = _make_stop_checker("stopB", "tStopB")  # turn_start = now (after the stale marker)
    assert_true(checker() is False, "stale marker (ts < turn_start) → False (cross-turn safety)")
    assert_true(not async_tasks._was_stopped("tStopB"), "task NOT marked stopped")


def test_cmd_stop_writes_epoch_marker():
    print("\n=== 4.3: terminal `stop -y` writes an epoch-ts marker when in flight ===")
    from claude_squared.__main__ import _cmd_stop, _stop_requests_dir
    _make_pair("cmdStop")
    _running_task("cmdStop", "tCmdStop")
    code = _cmd_stop(["cmdStop", "-y"])
    assert_eq(code, 0, "exit 0")
    marker = _stop_requests_dir() / "cmdStop.json"
    assert_true(marker.exists(), "marker written")
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert_true(isinstance(data["requested_at"], float), "requested_at is epoch float (Crack D)")


def test_cmd_stop_nothing_in_flight():
    print("\n=== 4.4: terminal `stop` on an idle pair writes no marker ===")
    from claude_squared.__main__ import _cmd_stop, _stop_requests_dir
    _make_pair("idleStop")
    code = _cmd_stop(["idleStop", "-y"])
    assert_eq(code, 0, "exit 0")
    assert_true(not (_stop_requests_dir() / "idleStop.json").exists(), "no marker for idle pair")


def test_runner_receives_task_id():
    print("\n=== 4.5: async_tasks._go calls runner(task_id) ===")
    captured = {}

    def fake_runner(task_id):
        from claude_squared.models import SendResult, ContextStatus
        captured["task_id"] = task_id
        return SendResult(name="x", response="ok", session_id="s", model_used="m",
                          cost_usd=0.0, duration_ms=1,
                          context=ContextStatus(tokens_used=1, tokens_max=200000, percent=0.0))

    state = async_tasks.start_task("rpair", "msg", fake_runner)
    final = async_tasks.wait_for_task(state.task_id, timeout_s=5.0)
    assert_eq(final.status, "done", "task completed")
    assert_eq(captured.get("task_id"), state.task_id, "runner received the task_id")


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    test_user_turn_points_excludes_tool_results()
    test_after_context_files()
    test_find_sentinel_line()
    test_truncate_and_dropped_files()
    test_truncate_to_sentinel_for_fork()
    test_pair_rewind_points_tool()
    test_pair_rewind_tool()
    test_pair_rewind_invalid_point()
    test_pair_fork_tool()
    test_pair_fork_collision_naming()
    test_stop_checker_honors_fresh_marker()
    test_stop_checker_ignores_stale_marker()
    test_cmd_stop_writes_epoch_marker()
    test_cmd_stop_nothing_in_flight()
    test_runner_receives_task_id()
    print("\n" + "=" * 60)
    print("PASS: all v0.10.0 smoke checks passed")


if __name__ == "__main__":
    main()
