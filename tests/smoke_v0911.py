"""v0.9.11 smoke: read-only terminal subcommands (list / info / context).

These run via `python -m claude_squared <cmd>` and read ~/.claude/pairs/
directly — zero inference, no agent. Tests call the command functions in-process
(capturing stdout) plus one end-to-end subprocess check that the module
dispatches correctly.

Run:
    python -u tests/smoke_v0911.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v0911_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_squared import registry as reg_mod
from claude_squared.__main__ import (
    _cmd_context,
    _cmd_info,
    _cmd_list,
    _context_fill,
)
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.models import PairSpec


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


def _capture(fn, argv):
    """Call a _cmd_* function, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def _make_pair(name, model="opus", purpose="", turns=3, session_id=None):
    cwd = str(Path(_TMPDIR) / "work")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    spec = PairSpec(
        name=name,
        session_id=session_id or f"sid-{name}-0000",
        purpose=purpose,
        model=model,
        effort="xhigh",
        permission_mode="auto",
        cwd=cwd,
        turn_count=turns,
    )
    reg_mod.add_pair(spec)
    return spec


def _write_jsonl_with_usage(spec, input_tokens, cache_read=0, cache_creation=0):
    """Write a minimal session JSONL whose last assistant message has a usage
    block, so _context_fill has something to read."""
    tp = ClaudeAdapter().transcript_path(spec)
    tp.parent.mkdir(parents=True, exist_ok=True)
    ev = {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            }
        },
    }
    tp.write_text(json.dumps(ev) + "\n", encoding="utf-8")


# ----------------------------- list ---------------------------------------

def test_list_empty():
    print("=== 1.1: list with no pairs ===")
    code, out, _ = _capture(_cmd_list, [])
    assert_eq(code, 0, "exit 0")
    assert_true("No pairs registered" in out, "friendly empty message")


def test_list_shows_pairs():
    print("\n=== 1.2: list shows registered pairs ===")
    _make_pair("alpha", purpose="first pair")
    _make_pair("beta", model="claude-opus-4-8[1m]", purpose="second pair")
    code, out, _ = _capture(_cmd_list, [])
    assert_eq(code, 0, "exit 0")
    assert_true("alpha" in out and "beta" in out, "both pair names listed")
    assert_true("2 pair(s)" in out, "shows count")
    assert_true("first pair" in out, "shows purpose")


# ----------------------------- info ---------------------------------------

def test_info_basic():
    print("\n=== 2.1: info shows config for a pair ===")
    spec = _make_pair("gamma", model="claude-opus-4-8[1m]", purpose="the gamma pair")
    code, out, _ = _capture(_cmd_info, ["gamma"])
    assert_eq(code, 0, "exit 0")
    assert_true("gamma" in out, "pair name shown")
    assert_true(spec.session_id in out, "session id shown")
    assert_true("claude-opus-4-8[1m]" in out, "model shown")
    assert_true("the gamma pair" in out, "purpose shown")
    assert_true("transcript:" in out, "transcript path line shown")


def test_info_context_fill_zero_inference():
    print("\n=== 2.2: info shows context fill % computed from JSONL (zero inference) ===")
    spec = _make_pair("delta", model="claude-opus-4-8[1m]")
    # 1M-window model: 300k used → 30%
    _write_jsonl_with_usage(spec, input_tokens=100_000, cache_read=200_000)
    code, out, _ = _capture(_cmd_info, ["delta"])
    assert_eq(code, 0, "exit 0")
    assert_true("300,000" in out, "used tokens shown (100k+200k)")
    assert_true("1,000,000" in out, "1M window for [1m] model")
    assert_true("30%" in out, "30% fill")


def test_info_not_found():
    print("\n=== 2.3: info on unknown pair → exit 2 ===")
    code, out, err = _capture(_cmd_info, ["nope-not-real"])
    assert_eq(code, 2, "exit 2 not found")
    assert_true("No pair named" in err, "friendly not-found message on stderr")


def test_info_no_arg():
    print("\n=== 2.4: info with no pair name → exit 64 (usage) ===")
    code, _, err = _capture(_cmd_info, [])
    assert_eq(code, 64, "exit 64 usage")
    assert_true("Usage:" in err, "usage on stderr")


# ----------------------------- context ------------------------------------

def test_context_fill():
    print("\n=== 3.1: context shows fill % for a pair ===")
    spec = _make_pair("epsilon", model="opus")  # 200k window
    _write_jsonl_with_usage(spec, input_tokens=50_000)  # 50k / 200k = 25%
    code, out, _ = _capture(_cmd_context, ["epsilon"])
    assert_eq(code, 0, "exit 0")
    assert_true("25%" in out, "25% fill (50k/200k, non-1m model)")
    assert_true("zero inference" in out.lower(), "notes zero inference")


def test_context_warns_near_limit():
    print("\n=== 3.2: context warns at >=85% ===")
    spec = _make_pair("zeta", model="opus")
    _write_jsonl_with_usage(spec, input_tokens=180_000)  # 90%
    code, out, _ = _capture(_cmd_context, ["zeta"])
    assert_eq(code, 0, "exit 0")
    assert_true("90%" in out, "90% fill")
    assert_true("Near limit" in out or "pair_compact" in out, "warns to compact")


def test_context_no_turns():
    print("\n=== 3.3: context on a pair with no JSONL → 'no turns yet' ===")
    _make_pair("eta", model="opus")  # no JSONL written
    code, out, _ = _capture(_cmd_context, ["eta"])
    assert_eq(code, 0, "exit 0")
    assert_true("no turns yet" in out.lower(), "friendly no-turns message")


def test_context_fill_helper_none_when_no_jsonl():
    print("\n=== 3.4: _context_fill returns None when no JSONL exists ===")
    spec = _make_pair("theta", model="opus")
    assert_true(_context_fill(spec) is None, "_context_fill None for missing JSONL")


# ----------------------- end-to-end dispatch ------------------------------

def test_subprocess_dispatch_and_help():
    print("\n=== 4.1: `python -m claude_squared list` dispatches end-to-end ===")
    env = dict(os.environ)
    env["CLAUDE_HOME"] = _TMPDIR
    env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, "-m", "claude_squared", "list"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert_eq(r.returncode, 0, "list exits 0 via subprocess")
    assert_true("pair(s)" in r.stdout, "list output via subprocess")

    print("\n=== 4.2: `--help` lists the commands, exit 0 ===")
    r2 = subprocess.run(
        [sys.executable, "-m", "claude_squared", "--help"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert_eq(r2.returncode, 0, "--help exits 0")
    assert_true("list" in r2.stdout and "info" in r2.stdout and "context" in r2.stdout,
                "help lists the read-only commands")

    print("\n=== 4.3: unknown subcommand → exit 64 + usage ===")
    r3 = subprocess.run(
        [sys.executable, "-m", "claude_squared", "bogus-cmd"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert_eq(r3.returncode, 64, "unknown command exits 64")
    assert_true("unknown command" in r3.stderr, "names the unknown command")


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    test_list_empty()
    test_list_shows_pairs()
    test_info_basic()
    test_info_context_fill_zero_inference()
    test_info_not_found()
    test_info_no_arg()
    test_context_fill()
    test_context_warns_near_limit()
    test_context_no_turns()
    test_context_fill_helper_none_when_no_jsonl()
    test_subprocess_dispatch_and_help()
    print("\n" + "=" * 60)
    print("PASS: all v0.9.11 smoke checks passed")


if __name__ == "__main__":
    main()
