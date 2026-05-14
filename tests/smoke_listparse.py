"""Smoke test for the MCP-boundary list/path coercion helpers.

Exercises every input shape we expect from misbehaving MCP host transports plus
the human-friendly conveniences (newline / semicolon / single value), with
particular attention to Windows + OneDrive path edge cases (spaces, dashes,
commas inside folder names).

Run from repo root:
    python -u tests/smoke_listparse.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Path setup so the script runs from repo root without install
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_squared.server import (  # noqa: E402
    _coerce_to_str_list,
    _normalize_path,
    _normalize_path_list,
)


def _eq(actual, expected, label):
    ok = actual == expected
    marker = "✓" if ok else "✗"
    print(f"  {marker} {label}")
    if not ok:
        print(f"      expected: {expected!r}")
        print(f"      actual:   {actual!r}")
    return ok


def test_coerce_basic():
    print("[coerce] basic")
    failures = 0
    failures += not _eq(_coerce_to_str_list(None), None, "None -> None")
    failures += not _eq(_coerce_to_str_list(""), None, "'' -> None")
    failures += not _eq(_coerce_to_str_list("   "), None, "whitespace-only -> None")
    failures += not _eq(_coerce_to_str_list([]), None, "[] -> None")
    failures += not _eq(_coerce_to_str_list(["a", "b"]), ["a", "b"], "[a,b] -> [a,b]")
    failures += not _eq(_coerce_to_str_list(["  a  ", "", "b"]), ["a", "b"], "trim + drop empties in list")
    failures += not _eq(_coerce_to_str_list([1, 2]), ["1", "2"], "non-str list elems coerced to str")
    return failures


def test_coerce_json_string():
    """The bug case: host JSON-encodes the list into a string."""
    print("[coerce] JSON-encoded string from misbehaving host")
    failures = 0
    failures += not _eq(
        _coerce_to_str_list('["a","b"]'),
        ["a", "b"],
        "simple JSON array string",
    )
    # The actual error case observed from CCD with extra_dirs=["C:\\Users\\alvar\\.claude"]:
    failures += not _eq(
        _coerce_to_str_list('["C:\\\\Users\\\\alvar\\\\.claude"]'),
        ["C:\\Users\\alvar\\.claude"],
        "Windows path inside JSON string (4-backslash escapes)",
    )
    failures += not _eq(
        _coerce_to_str_list('["Bash(git *)", "Read", "Edit"]'),
        ["Bash(git *)", "Read", "Edit"],
        "allowed_tools-shape JSON array",
    )
    failures += not _eq(
        _coerce_to_str_list('  ["a","b"]  '),
        ["a", "b"],
        "JSON array with surrounding whitespace",
    )
    # Malformed JSON falls through to other splits (here, single value)
    failures += not _eq(
        _coerce_to_str_list('[malformed'),
        ["[malformed"],
        "malformed JSON falls through to single-value",
    )
    return failures


def test_coerce_separators():
    print("[coerce] convenience separators")
    failures = 0
    failures += not _eq(
        _coerce_to_str_list("a;b;c"),
        ["a", "b", "c"],
        "semicolon-separated",
    )
    failures += not _eq(
        _coerce_to_str_list("a\nb\nc"),
        ["a", "b", "c"],
        "newline-separated",
    )
    failures += not _eq(
        _coerce_to_str_list(" a ; b ; c "),
        ["a", "b", "c"],
        "semicolon-separated with whitespace",
    )
    failures += not _eq(
        _coerce_to_str_list("a;;b"),
        ["a", "b"],
        "empty entries between separators dropped",
    )
    return failures


def test_coerce_single_value():
    print("[coerce] single-value strings")
    failures = 0
    failures += not _eq(_coerce_to_str_list("Read"), ["Read"], "bare token")
    failures += not _eq(
        _coerce_to_str_list("C:/Users/alvar/.claude"),
        ["C:/Users/alvar/.claude"],
        "single forward-slash path",
    )
    failures += not _eq(
        _coerce_to_str_list("C:\\Users\\alvar\\.claude"),
        ["C:\\Users\\alvar\\.claude"],
        "single backslash path",
    )
    return failures


def test_coerce_onedrive_paths():
    """OneDrive folder names contain spaces and sometimes dashes/commas. Make sure we
    don't accidentally split them on comma or other in-name characters."""
    print("[coerce] OneDrive path edge cases")
    failures = 0
    # OneDrive Personal: just spaces (none, actually)
    failures += not _eq(
        _coerce_to_str_list("C:\\Users\\alvar\\OneDrive"),
        ["C:\\Users\\alvar\\OneDrive"],
        "OneDrive Personal (no space)",
    )
    # OneDrive Business: space + dash + company name
    failures += not _eq(
        _coerce_to_str_list("C:\\Users\\alvar\\OneDrive - Anthropic"),
        ["C:\\Users\\alvar\\OneDrive - Anthropic"],
        "OneDrive Business with space and dash (single value)",
    )
    # OneDrive Business with comma in company name -- MUST NOT split
    failures += not _eq(
        _coerce_to_str_list("C:\\Users\\alvar\\OneDrive - Acme, Inc"),
        ["C:\\Users\\alvar\\OneDrive - Acme, Inc"],
        "OneDrive Business with comma in name (NOT split)",
    )
    # Multiple OneDrive paths via semicolon
    failures += not _eq(
        _coerce_to_str_list(
            "C:\\Users\\alvar\\OneDrive;C:\\Users\\alvar\\OneDrive - Anthropic"
        ),
        ["C:\\Users\\alvar\\OneDrive", "C:\\Users\\alvar\\OneDrive - Anthropic"],
        "Two OneDrive paths semicolon-separated",
    )
    # Multiple OneDrive paths via JSON
    failures += not _eq(
        _coerce_to_str_list(
            '["C:\\\\Users\\\\alvar\\\\OneDrive","C:\\\\Users\\\\alvar\\\\OneDrive - Anthropic"]'
        ),
        ["C:\\Users\\alvar\\OneDrive", "C:\\Users\\alvar\\OneDrive - Anthropic"],
        "Two OneDrive paths JSON-encoded",
    )
    return failures


def test_normalize_path():
    print("[normalize_path] expansion + cleanup")
    failures = 0
    failures += not _eq(_normalize_path(None), None, "None -> None")
    failures += not _eq(_normalize_path(""), None, "empty -> None")
    failures += not _eq(_normalize_path("   "), None, "whitespace-only -> None")

    # ~ expansion (with separator canonicalization)
    home = str(Path.home())
    failures += not _eq(
        _normalize_path("~/.claude"),
        os.path.normpath(os.path.join(home, ".claude")),
        "~/.claude expanded to home (normalized separators)",
    )

    # Quoted path -- strip surrounding quotes
    failures += not _eq(
        _normalize_path('"C:/Program Files/Foo"'),
        os.path.normpath("C:/Program Files/Foo"),
        "double-quoted path unquoted",
    )
    failures += not _eq(
        _normalize_path("'C:/Program Files/Foo'"),
        os.path.normpath("C:/Program Files/Foo"),
        "single-quoted path unquoted",
    )

    # Synthetic env var to test portable env expansion
    os.environ["CPMTEST_FAKE"] = "C:\\fake\\base"
    try:
        if os.name == "nt":
            failures += not _eq(
                _normalize_path("%CPMTEST_FAKE%\\sub"),
                os.path.normpath("C:\\fake\\base\\sub"),
                "%VAR% Windows env var expanded",
            )
        failures += not _eq(
            _normalize_path("$CPMTEST_FAKE/sub"),
            os.path.normpath("C:\\fake\\base/sub"),
            "$VAR Unix-style env var expanded",
        )
    finally:
        del os.environ["CPMTEST_FAKE"]

    # Path with internal spaces preserved (and normalized to OS separators)
    failures += not _eq(
        _normalize_path("C:/Users/alvar/OneDrive - Anthropic"),
        os.path.normpath("C:/Users/alvar/OneDrive - Anthropic"),
        "OneDrive Business path with space-dash-name preserved",
    )

    return failures


def test_normalize_path_list():
    print("[normalize_path_list] composes coerce + normalize")
    failures = 0
    home = str(Path.home())
    failures += not _eq(
        _normalize_path_list(_coerce_to_str_list("~/.claude;~/code")),
        [
            os.path.normpath(os.path.join(home, ".claude")),
            os.path.normpath(os.path.join(home, "code")),
        ],
        "two ~ paths semicolon-separated -> expanded list",
    )
    failures += not _eq(
        _normalize_path_list(_coerce_to_str_list(None)),
        None,
        "None -> None",
    )
    failures += not _eq(
        _normalize_path_list(_coerce_to_str_list("~/.claude")),
        [os.path.normpath(os.path.join(home, ".claude"))],
        "single ~/path -> [expanded]",
    )
    return failures


def main() -> int:
    suites = [
        test_coerce_basic,
        test_coerce_json_string,
        test_coerce_separators,
        test_coerce_single_value,
        test_coerce_onedrive_paths,
        test_normalize_path,
        test_normalize_path_list,
    ]
    total_failures = 0
    for suite in suites:
        total_failures += suite()
        print()
    print("=" * 60)
    if total_failures:
        print(f"FAIL: {total_failures} assertion(s) failed")
        return 1
    print("PASS: all coercion / normalization checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
