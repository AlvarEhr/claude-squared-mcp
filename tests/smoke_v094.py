"""v0.9.4 smoke: AskUserQuestion stripped at spawn + mode-aware permission handoff.

Pure unit tests — no live `claude` CLI subprocess. Run:
    python -u tests/smoke_v094.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v094_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_squared import registry as reg_mod
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.models import HEADLESS_INCOMPATIBLE_TOOLS, PairSpec, PermissionDenial, SendResult
from claude_squared.server import _fmt_send_result


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"  [FAIL] {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def assert_in(needle, haystack, label):
    if needle not in haystack:
        print(f"  [FAIL] {label}: {needle!r} not found in output")
        print("  ---- output ----")
        print(haystack)
        sys.exit(1)
    print(f"  [PASS] {label}")


def assert_not_in(needle, haystack, label):
    if needle in haystack:
        print(f"  [FAIL] {label}: {needle!r} unexpectedly present")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _send_result(name, denials):
    return SendResult(
        name=name, response="(reply text)", session_id="sid", model_used="claude-opus-4-8",
        cost_usd=0.0, duration_ms=10, permission_denials=denials,
    )


def test_askuserquestion_stripped_at_spawn():
    print("=== AskUserQuestion stripped from pair toolset at spawn ===")
    assert_in("AskUserQuestion", HEADLESS_INCOMPATIBLE_TOOLS,
              "AskUserQuestion in HEADLESS_INCOMPATIBLE_TOOLS")
    adapter = ClaudeAdapter()
    spec = PairSpec(name="probe", session_id="s1", model="opus", cwd=str(Path.cwd()))
    args = adapter._common_create_args(spec)
    # Every headless-incompatible tool must appear as a --disallowed-tools value
    for t in HEADLESS_INCOMPATIBLE_TOOLS:
        # find each occurrence preceded by the flag
        ok = any(args[i] == "--disallowed-tools" and args[i + 1] == t
                 for i in range(len(args) - 1))
        assert_eq(ok, True, f"--disallowed-tools {t} present in create args")
    # Our own pair tools still disallowed (didn't regress)
    assert_in("mcp__pair__*", args, "pair-tool namespace still disallowed")


def test_handoff_interactive_tool():
    print("\n=== Handoff: AskUserQuestion denial (structural, not permission) ===")
    reg_mod.add_pair(PairSpec(name="hpair", session_id="s2", model="opus",
                              permission_mode="bypassPermissions"))
    try:
        out = _fmt_send_result(_send_result("hpair", [PermissionDenial(tool_name="AskUserQuestion")]))
        # Reports the ACTUAL mode, not hardcoded "auto-mode"
        assert_in("permission_mode=bypassPermissions", out, "reports actual permission_mode")
        assert_not_in("blocked by auto-mode", out, "no hardcoded 'blocked by auto-mode'")
        # Structural remedy, not the bypass loop
        assert_in("cannot run in headless mode", out, "structural-remedy framing")
        assert_in("PLAIN TEXT", out, "tells orchestrator to re-request as plain text")
        assert_in("bypassPermissions will NOT help", out, "explicitly says bypass won't help")
    finally:
        reg_mod.remove_pair("hpair")


def test_handoff_permission_tool():
    print("\n=== Handoff: genuine permission denial (auto mode) ===")
    reg_mod.add_pair(PairSpec(name="ppair", session_id="s3", model="opus",
                              permission_mode="auto"))
    try:
        out = _fmt_send_result(_send_result("ppair", [PermissionDenial(tool_name="Bash")]))
        assert_in("permission_mode=auto", out, "reports auto mode")
        assert_in("override_permission_mode=\"bypassPermissions\"", out, "bypass remedy for real denial")
        assert_in("Bash", out, "names the denied tool")
        # Bash is not interactive, so no structural framing
        assert_not_in("cannot run in headless mode", out, "no structural framing for Bash")
    finally:
        reg_mod.remove_pair("ppair")


def test_handoff_mixed():
    print("\n=== Handoff: mixed (AskUserQuestion + Bash) → both remedies ===")
    reg_mod.add_pair(PairSpec(name="mpair", session_id="s4", model="opus",
                              permission_mode="auto"))
    try:
        out = _fmt_send_result(_send_result("mpair", [
            PermissionDenial(tool_name="AskUserQuestion"),
            PermissionDenial(tool_name="Bash"),
        ]))
        assert_in("cannot run in headless mode", out, "structural remedy present")
        assert_in("override_permission_mode=\"bypassPermissions\"", out, "permission remedy present")
    finally:
        reg_mod.remove_pair("mpair")


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    test_askuserquestion_stripped_at_spawn()
    test_handoff_interactive_tool()
    test_handoff_permission_tool()
    test_handoff_mixed()
    print("\n" + "=" * 60)
    print("PASS: all v0.9.4 smoke checks passed")


if __name__ == "__main__":
    main()
