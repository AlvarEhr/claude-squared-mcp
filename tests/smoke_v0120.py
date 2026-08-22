"""v0.12.0 smoke: premium-model (plan-gated, separate-limit) warning.

Pure-function test — no CLI, no network, no pair spawn. Verifies that the
premium warning fires on every spelling of a premium family, stays silent for
subscription-included models, and never crashes on the odd inputs that flow
through the same code path (``match-parent``, bare aliases, empty strings).

Run:  PYTHONIOENCODING=utf-8 python -u tests/smoke_v0120.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_squared.models import (  # noqa: E402
    SendResult,
    premium_model_note,
    parse_model_id,
)

passed = 0
failed = 0


def check(label: str, cond: bool) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


print("=== premium families FIRE (every spelling) ===")
for m in ("fable", "claude-fable-5", "claude-fable-5[1m]", "CLAUDE-FABLE-5",
          "claude-fable-6", "claude-fable-5-20260609"):
    note = premium_model_note(m)
    check(f"{m!r} warns", note is not None and "PREMIUM MODEL" in note)

print("\n=== subscription-included models STAY SILENT ===")
for m in ("opus", "claude-opus-5", "claude-opus-5[1m]", "claude-opus-4-8[1m]",
          "sonnet", "claude-sonnet-5", "haiku", "claude-haiku-4-5-20251001"):
    check(f"{m!r} silent", premium_model_note(m) is None)

print("\n=== odd inputs must not crash or false-fire ===")
for m in ("match-parent", "", "   ", "some-unknown-model", "claude-3-5-sonnet"):
    try:
        note = premium_model_note(m)
        check(f"{m!r} -> {note!r} (no exception, no fire)", note is None)
    except Exception as e:  # noqa: BLE001
        check(f"{m!r} raised {e!r}", False)

print("\n=== family keying is what makes it version-proof ===")
check("fable family parses", parse_model_id("claude-fable-5[1m]")[0] == "fable")
check("opus family parses", parse_model_id("claude-opus-5")[0] == "opus")

print("\n=== warning text is actionable ===")
note = premium_model_note("claude-fable-5")
check("names the model", "claude-fable-5" in note)
check("says it is allowed", "allowed" in note.lower())
check("asks for user confirmation", "confirm" in note.lower())

print("\n=== SendResult carries the note ===")
r = SendResult(
    name="x", response="hi", model_used="claude-opus-5",
    duration_ms=1, session_id="s", cost_usd=0.0,
)
check("premium_note defaults None", r.premium_note is None)
r.premium_note = "test"
check("premium_note assignable", r.premium_note == "test")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
