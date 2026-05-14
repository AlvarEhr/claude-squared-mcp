"""v0.8.1 smoke: pair_invoke allow-list + cli_paths consolidation.

Pure unit tests on the pieces that don't need a live `claude` CLI subprocess.
Run with: ``python -u tests/smoke_v081.py``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Force a fresh isolated CLAUDE_HOME so we don't pollute the real ~/.claude
_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v081_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Import AFTER CLAUDE_HOME is set so all path helpers see the override
from claude_squared import settings as settings_mod
from claude_squared.cli_paths import encode_cwd_for_project
from claude_squared.models import PairSpec
from claude_squared.server import _coerce_to_str_list, _invocation_allowed


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"  [FAIL] {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def assert_raises(exc_cls, fn, label):
    try:
        fn()
    except exc_cls as e:
        print(f"  [PASS] {label}: {str(e)[:80]}...")
        return
    except Exception as e:
        print(f"  [FAIL] {label}: expected {exc_cls.__name__}, got {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"  [FAIL] {label}: expected {exc_cls.__name__}, no exception raised")
    sys.exit(1)


# ============================================================================
# Item C: cli_paths.encode_cwd_for_project — single source of truth
# ============================================================================

def test_encode_cwd_for_project():
    print("=== cli_paths.encode_cwd_for_project ===")
    # Mirrors the CLI's regex: any non-alphanumeric → '-'
    assert_eq(encode_cwd_for_project("/c/Users/alvar/code"),
              "-c-Users-alvar-code", "POSIX-style path")
    assert_eq(encode_cwd_for_project(r"C:\Users\alvar\code"),
              "C--Users-alvar-code", "Windows backslash path")
    assert_eq(encode_cwd_for_project("foo bar baz"),
              "foo-bar-baz", "spaces replaced")
    assert_eq(encode_cwd_for_project("alphanum123only"),
              "alphanum123only", "alphanumeric preserved unchanged")
    # Underscores ARE non-alphanumeric — get replaced too (verified empirically against CLI)
    assert_eq(encode_cwd_for_project("foo_bar"),
              "foo-bar", "underscores ARE replaced (CLI behavior)")
    # Path object input handled
    assert_eq(encode_cwd_for_project(Path("/tmp/x.y")),
              "-tmp-x-y", "Path input works the same as str")
    # Idempotent: result of encoding is itself encoded the same way
    once = encode_cwd_for_project("/c/Users")
    twice = encode_cwd_for_project(once)
    assert_eq(once, twice, "encoding is idempotent")


def test_encoding_call_sites_use_single_source():
    print("\n=== Encoding call sites all import from cli_paths ===")
    # Verify the three previous mirror sites now resolve to the same function object
    from claude_squared.adapters import claude as adapter_mod
    from claude_squared import runtime as runtime_mod
    from claude_squared import server as server_mod
    from claude_squared.cli_paths import encode_cwd_for_project as canonical

    assert_eq(adapter_mod._encode_cwd_for_project, canonical,
              "adapters/claude.py uses cli_paths source")
    assert_eq(runtime_mod._encode_cwd_for_project, canonical,
              "runtime.py uses cli_paths source")
    assert_eq(server_mod._encode_cwd_for_project, canonical,
              "server.py uses cli_paths source")


# ============================================================================
# Item B: allow-list logic
# ============================================================================

def test_invocation_allowed_basic():
    print("\n=== _invocation_allowed semantics ===")
    # None = allow all (backward compat)
    assert_eq(_invocation_allowed("clear", None), True, "None allow-list = allow")
    assert_eq(_invocation_allowed("anything-at-all", None), True, "None allow-list = always allow")

    # [] = deny all (explicit lockdown)
    assert_eq(_invocation_allowed("clear", []), False, "[] allow-list = deny")
    assert_eq(_invocation_allowed("compact", []), False, "[] allow-list = deny anything")

    # Exact match
    assert_eq(_invocation_allowed("clear", ["clear", "compact"]), True, "exact match permits")
    assert_eq(_invocation_allowed("init", ["clear", "compact"]), False, "non-match denies")


def test_invocation_allowed_globs():
    print("\n=== _invocation_allowed glob patterns ===")
    # Wildcard matching
    assert_eq(_invocation_allowed("mcp__claude_ai_Hugging_Face__User_Summary",
                                  ["mcp__claude_ai_*"]), True, "MCP-server prefix glob")
    assert_eq(_invocation_allowed("mcp__other_namespace__action",
                                  ["mcp__claude_ai_*"]), False, "different MCP namespace excluded")
    # Mixed exact + glob
    assert_eq(_invocation_allowed("clear", ["clear", "compact", "mcp__*"]),
              True, "exact preserved alongside globs")
    assert_eq(_invocation_allowed("mcp__anything", ["clear", "compact", "mcp__*"]),
              True, "glob preserved alongside exacts")
    assert_eq(_invocation_allowed("init", ["clear", "compact", "mcp__*"]),
              False, "neither exact nor glob = deny")
    # ? single-char wildcard
    assert_eq(_invocation_allowed("foo1", ["foo?"]), True, "single-char wildcard")
    assert_eq(_invocation_allowed("foo10", ["foo?"]), False, "single-char wildcard rejects multi-char")
    # fnmatchcase = case-sensitive (important: 'Clear' != 'clear')
    assert_eq(_invocation_allowed("Clear", ["clear"]), False, "case-sensitive matching")


# ============================================================================
# Item B: PairSpec / PairDefaults storage
# ============================================================================

def test_pairspec_allowed_invocations():
    print("\n=== PairSpec.allowed_invocations storage ===")
    # Default: None (allow all)
    s = PairSpec(name="x", session_id="abc")
    assert_eq(s.allowed_invocations, None, "default unset = None")

    # Explicit set
    s2 = PairSpec(name="x", session_id="abc", allowed_invocations=["clear", "compact"])
    assert_eq(s2.allowed_invocations, ["clear", "compact"], "explicit list stored")

    # Lockdown
    s3 = PairSpec(name="x", session_id="abc", allowed_invocations=[])
    assert_eq(s3.allowed_invocations, [], "explicit [] lockdown stored")


def test_pairdefaults_deny_all_guard():
    print("\n=== PairDefaults [] foot-gun guard ===")
    # Empty list as default is REFUSED (would silently brick every fresh pair)
    assert_raises(
        ValueError,
        lambda: settings_mod.update_defaults(allowed_invocations=[]),
        "PairDefaults rejects [] (deny-all) as global default"
    )

    # Non-empty list IS accepted as default
    new_defaults, msgs = settings_mod.update_defaults(allowed_invocations=["clear", "compact"])
    assert_eq(new_defaults.allowed_invocations, ["clear", "compact"],
              "non-empty list accepted as default")
    # Reset for cleanliness
    settings_mod.reset_defaults()


def test_pairdefaults_load_save():
    print("\n=== PairDefaults round-trip with allowed_invocations ===")
    settings_mod.reset_defaults()
    # Set, reload, verify
    settings_mod.update_defaults(allowed_invocations=["clear", "mcp__claude_ai_*"])
    loaded = settings_mod.load_defaults()
    assert_eq(loaded.allowed_invocations, ["clear", "mcp__claude_ai_*"],
              "round-trip preserves list")
    settings_mod.reset_defaults()


# ============================================================================
# Bug #1 regression (v0.8.2): _coerce_to_str_list must preserve []
# under preserve_empty=True so lockdown intent reaches the validators.
# ============================================================================

def test_coerce_to_str_list_default_collapses_empty():
    print("\n=== _coerce_to_str_list: default behavior (collapse) ===")
    # Default behavior preserved for fields where [] == None semantically
    # (allowed_tools, extra_dirs, mcp_whitelist).
    assert_eq(_coerce_to_str_list([]), None, "default: [] -> None")
    assert_eq(_coerce_to_str_list(""), None, "default: '' -> None")
    assert_eq(_coerce_to_str_list("[]"), None, "default: '[]' -> None")
    # Whitespace-only items get dropped, and the resulting empty list collapses
    assert_eq(_coerce_to_str_list(["", "   "]), None,
              "default: list of empties collapses to None")


def test_coerce_to_str_list_preserve_empty():
    print("\n=== _coerce_to_str_list: preserve_empty=True keeps [] ===")
    # preserve_empty=True is the lockdown-friendly path used by allow-list call sites
    assert_eq(_coerce_to_str_list([], preserve_empty=True), [],
              "preserve_empty: [] survives as []")
    assert_eq(_coerce_to_str_list("", preserve_empty=True), [],
              "preserve_empty: '' survives as []")
    assert_eq(_coerce_to_str_list("[]", preserve_empty=True), [],
              "preserve_empty: '[]' parses then survives as []")
    assert_eq(_coerce_to_str_list(["", "   "], preserve_empty=True), [],
              "preserve_empty: list of empties survives as []")
    # None still returns None (preserve_empty doesn't change None semantics)
    assert_eq(_coerce_to_str_list(None, preserve_empty=True), None,
              "preserve_empty: None still returns None")
    # Non-empty inputs work the same way regardless of flag
    assert_eq(_coerce_to_str_list(["a", "b"], preserve_empty=True), ["a", "b"],
              "preserve_empty: non-empty list passes through")
    assert_eq(_coerce_to_str_list("a;b", preserve_empty=True), ["a", "b"],
              "preserve_empty: semicolon split passes through")


def test_pairdefaults_deny_all_guard_via_settings_set_path():
    print("\n=== Regression: foot-gun guard fires through pair_settings_set path ===")
    # Simulate exactly what pair_settings_set does:
    #   1. _coerce_to_str_list(value, preserve_empty=True) on the raw input
    #   2. Pass result to settings_mod.update_defaults(allowed_invocations=...)
    # The bug was: step 1 collapsed [] -> None, so step 2 saw None (allow-all)
    # instead of [] (deny-all), and the validator's foot-gun guard never fired.
    coerced = _coerce_to_str_list([], preserve_empty=True)
    assert_eq(coerced, [], "coercion preserves [] for downstream validator")
    assert_raises(
        ValueError,
        lambda: settings_mod.update_defaults(allowed_invocations=coerced),
        "guard fires when [] reaches update_defaults()"
    )
    # And the equivalent JSON-array string path
    coerced2 = _coerce_to_str_list("[]", preserve_empty=True)
    assert_eq(coerced2, [], "coercion of '[]' string preserves []")
    assert_raises(
        ValueError,
        lambda: settings_mod.update_defaults(allowed_invocations=coerced2),
        "guard fires when '[]' string reaches update_defaults()"
    )


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    test_encode_cwd_for_project()
    test_encoding_call_sites_use_single_source()
    test_invocation_allowed_basic()
    test_invocation_allowed_globs()
    test_pairspec_allowed_invocations()
    test_pairdefaults_deny_all_guard()
    test_pairdefaults_load_save()
    # v0.8.2 regression coverage for the [] → None coercion bug
    test_coerce_to_str_list_default_collapses_empty()
    test_coerce_to_str_list_preserve_empty()
    test_pairdefaults_deny_all_guard_via_settings_set_path()
    print("\n" + "=" * 60)
    print("PASS: all v0.8.1 + v0.8.2 smoke checks passed")


if __name__ == "__main__":
    main()
