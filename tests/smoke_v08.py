"""v0.8.0 smoke: settings/defaults + per-model effort coercion + match-parent fallback.

Covers the foundational pieces without requiring a live `claude` CLI install
(no subprocess spawn here — pure unit tests on the resolution + storage logic).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Force a fresh isolated CLAUDE_HOME so we don't pollute the real ~/.claude
_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v08_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Import AFTER setting CLAUDE_HOME so all path helpers see the override
from claude_squared import settings as settings_mod
from claude_squared.models import (
    PairSpec,
    coerce_effort_for_model,
    default_effort_for_model,
)


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"  [FAIL] {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def test_per_model_effort_defaults():
    print("=== Per-model effort defaults ===")
    assert_eq(default_effort_for_model("opus"), "xhigh", "opus defaults to xhigh")
    assert_eq(default_effort_for_model("claude-opus-4-7"), "xhigh", "full opus name still xhigh")
    assert_eq(default_effort_for_model("sonnet"), "high", "sonnet defaults to high")
    assert_eq(default_effort_for_model("claude-sonnet-4-6"), "high", "full sonnet name still high")
    assert_eq(default_effort_for_model("haiku"), None, "haiku defaults to None")
    assert_eq(default_effort_for_model("claude-haiku-4-5"), None, "full haiku name still None")
    assert_eq(default_effort_for_model("unknown-model"), "xhigh", "unknown family -> xhigh fallback")


def test_effort_coercion():
    print("\n=== Effort coercion (silent demote + transparency message) ===")
    # Opus: all 5 levels accepted, no message
    for eff in ("low", "medium", "high", "xhigh", "max"):
        coerced, msg = coerce_effort_for_model("opus", eff)
        assert_eq(coerced, eff, f"opus + {eff} passes through")
        assert_eq(msg, None, f"opus + {eff} no message")

    # Sonnet: low/medium/high pass, xhigh/max coerce to high with message
    for eff in ("low", "medium", "high"):
        coerced, msg = coerce_effort_for_model("sonnet", eff)
        assert_eq(coerced, eff, f"sonnet + {eff} passes through")
        assert_eq(msg, None, f"sonnet + {eff} no message")
    for eff in ("xhigh", "max"):
        coerced, msg = coerce_effort_for_model("sonnet", eff)
        assert_eq(coerced, "high", f"sonnet + {eff} -> high")
        assert msg is not None and "coerced" in msg.lower(), f"sonnet + {eff} has transparency msg"
        print(f"  [PASS] sonnet + {eff} message: {msg[:60]}...")

    # Haiku: any non-None effort -> None with message; None passes through silently
    coerced, msg = coerce_effort_for_model("haiku", None)
    assert_eq(coerced, None, "haiku + None passes through")
    assert_eq(msg, None, "haiku + None no message")
    for eff in ("low", "medium", "high", "xhigh", "max"):
        coerced, msg = coerce_effort_for_model("haiku", eff)
        assert_eq(coerced, None, f"haiku + {eff} -> None")
        assert msg is not None and "doesn't support" in msg, f"haiku + {eff} has transparency msg"


def test_pairspec_validator_safety_net():
    """The Pydantic model_validator(mode='before') must coerce inconsistent
    (model, effort) pairs at PairSpec construction — even if the API-boundary
    coercion in pair_create / pair_update is bypassed (registry migration,
    direct PairSpec(...) call, etc.)."""
    print("\n=== PairSpec validator safety net ===")
    spec = PairSpec(name="test", session_id="abc", model="haiku", effort="xhigh")
    assert_eq(spec.effort, None, "PairSpec coerces haiku+xhigh -> effort=None")

    spec2 = PairSpec(name="test", session_id="abc", model="claude-sonnet-4-6", effort="max")
    assert_eq(spec2.effort, "high", "PairSpec coerces sonnet+max -> effort=high")

    # Opus should pass through unchanged
    spec3 = PairSpec(name="test", session_id="abc", model="opus", effort="max")
    assert_eq(spec3.effort, "max", "PairSpec preserves opus+max")


def test_settings_storage_and_load():
    print("\n=== Settings file storage + load ===")
    # Empty initial state: defaults file doesn't exist yet
    d = settings_mod.load_defaults()
    assert_eq(d.model, None, "fresh load: model unset")
    assert_eq(d.effort, None, "fresh load: effort unset")
    assert_eq(d.persistent, None, "fresh load: persistent unset")

    # Set + reload roundtrip
    new_d, msgs = settings_mod.update_defaults(model="opus", effort="max", persistent=True)
    assert_eq(new_d.model, "opus", "after set: model=opus")
    assert_eq(new_d.effort, "max", "after set: effort=max")
    assert_eq(new_d.persistent, True, "after set: persistent=True")

    # Reload from disk
    d2 = settings_mod.load_defaults()
    assert_eq(d2.model, "opus", "reloaded from disk: model")
    assert_eq(d2.effort, "max", "reloaded from disk: effort")


def test_settings_bypass_permissions_guard():
    print("\n=== Settings bypassPermissions guard ===")
    # Reset to clean state
    settings_mod.reset_defaults()

    try:
        settings_mod.update_defaults(permission_mode="bypassPermissions")
        print("  [FAIL] bypassPermissions was allowed as a default — guard broken")
        sys.exit(1)
    except ValueError as e:
        if "bypassPermissions" in str(e) and "default" in str(e).lower():
            print(f"  [PASS] bypassPermissions rejected with helpful error: {str(e)[:70]}...")
        else:
            print(f"  [FAIL] bypassPermissions rejected but error wasn't informative: {e}")
            sys.exit(1)

    # Other valid permission modes should work
    new_d, _ = settings_mod.update_defaults(permission_mode="acceptEdits")
    assert_eq(new_d.permission_mode, "acceptEdits", "acceptEdits accepted as default")


def test_settings_unknown_field_rejection():
    print("\n=== Settings unknown-field rejection ===")
    settings_mod.reset_defaults()
    try:
        settings_mod.update_defaults(model="opus", nonexistent_field="xyz")
        print("  [FAIL] unknown field silently dropped — should have raised")
        sys.exit(1)
    except ValueError as e:
        if "nonexistent_field" in str(e) or "unknown" in str(e).lower():
            print(f"  [PASS] unknown field rejected: {str(e)[:70]}...")
        else:
            print(f"  [FAIL] unknown field rejected but error not informative: {e}")
            sys.exit(1)


def test_settings_model_change_auto_resets_effort():
    print("\n=== Settings: model change auto-resets incompatible effort ===")
    settings_mod.reset_defaults()
    settings_mod.update_defaults(model="opus", effort="max")

    # Switch to sonnet alone — effort=max is incompatible, should auto-reset to high
    new_d, msgs = settings_mod.update_defaults(model="sonnet")
    assert_eq(new_d.model, "sonnet", "model now sonnet")
    assert_eq(new_d.effort, "high", "effort auto-reset to sonnet's default (high)")
    assert any("auto-reset" in m for m in msgs), "auto-reset message surfaced"

    # Switch to haiku — effort should auto-reset to None
    new_d, msgs = settings_mod.update_defaults(model="haiku")
    assert_eq(new_d.model, "haiku", "model now haiku")
    assert_eq(new_d.effort, None, "effort auto-reset to None for haiku")


def test_settings_explicit_both_fields_no_autoreset():
    print("\n=== Settings: explicit (model, effort) bypasses auto-reset ===")
    settings_mod.reset_defaults()
    settings_mod.update_defaults(model="opus", effort="max")

    # Set both at once: no auto-reset, but coercion still fires (sonnet+max -> high)
    new_d, msgs = settings_mod.update_defaults(model="sonnet", effort="medium")
    assert_eq(new_d.effort, "medium", "explicit sonnet+medium preserved exactly")


def test_settings_reset():
    print("\n=== Settings reset ===")
    settings_mod.update_defaults(model="opus", effort="max")
    assert settings_mod.defaults_path().exists(), "defaults file should exist after set"
    settings_mod.reset_defaults()
    assert not settings_mod.defaults_path().exists(), "defaults file should be gone after reset"
    d = settings_mod.load_defaults()
    assert_eq(d.model, None, "after reset: model unset")
    print("  [PASS] reset removes file and load returns empty defaults")


def test_match_parent_resolution():
    """Tests the four branches of _resolve_match_parent_model:
       1. Explicit parent_model arg (no I/O)
       2. JSONL parse with model field
       3. JSONL exists but no model field (brand-new session)
       4. JSONL doesn't exist OR no CLAUDE_CODE_SESSION_ID env (graceful fallback)

    Bug #1 from historian's review (now fixed): the duplicate _encode_cwd_for_project
    in server.py was calling re.sub on a Path object, crashing all match-parent
    flows. This test exercises the JSONL path so the dedup regression is caught
    if anyone re-introduces the duplicate."""
    print("\n=== Match-parent resolution ===")
    from claude_squared.server import _resolve_match_parent_model

    # 1. Explicit parent_model arg short-circuits — no env / file I/O needed
    result, msg = _resolve_match_parent_model("claude-sonnet-4-6")
    assert_eq(result, "claude-sonnet-4-6", "explicit parent_model returned")
    assert "explicit parent_model" in msg, "explicit-arg path message"

    # 2. Save+restore env, then test JSONL paths under the isolated CLAUDE_HOME.
    # (Note: tmp dir at module load became CLAUDE_HOME, so `~/.claude/projects/...`
    # resolves under that tmp dir.)
    saved_sid = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    try:
        # 2a. No env var → fallback
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "opus", "no env var → opus fallback")
        assert "no CLAUDE_CODE_SESSION_ID" in msg, "no-env-var message"

        # 2b. env var set, but JSONL doesn't exist → fallback
        os.environ["CLAUDE_CODE_SESSION_ID"] = "fake-sid-no-file"
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "opus", "missing JSONL → opus fallback")
        assert "session JSONL not found" in msg, "missing-jsonl message"

        # 2c. env var set, JSONL exists with model field → return it
        from claude_squared.server import _encode_cwd_for_project
        from claude_squared.registry import claude_home
        sid = "test-sid-with-model"
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        encoded = _encode_cwd_for_project(str(Path.cwd()))
        jsonl_dir = claude_home() / "projects" / encoded
        jsonl_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = jsonl_dir / f"{sid}.jsonl"
        # Write 3 fake assistant messages; the LAST model wins
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6", "role": "assistant"}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-4-6", "role": "assistant"}}) + "\n")
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "claude-sonnet-4-6", "JSONL parse returns latest model")
        assert "detected" in msg and "session JSONL" in msg, "JSONL-detection message"

        # 2d. JSONL exists but no assistant messages with model field → fallback
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "opus", "no model in JSONL → opus fallback")
        assert "no assistant messages" in msg, "no-model-in-jsonl message"

        # 2e. JSONL has malformed lines mixed with valid ones — should not crash
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write("THIS IS NOT JSON\n")
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-haiku-4-5", "role": "assistant"}}) + "\n")
            f.write("\x00\x01garbage\n")
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "claude-haiku-4-5", "malformed lines tolerated; valid model still recovered")
    finally:
        # Restore env
        if saved_sid:
            os.environ["CLAUDE_CODE_SESSION_ID"] = saved_sid
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    test_per_model_effort_defaults()
    test_effort_coercion()
    test_pairspec_validator_safety_net()
    test_settings_storage_and_load()
    test_settings_bypass_permissions_guard()
    test_settings_unknown_field_rejection()
    test_settings_model_change_auto_resets_effort()
    test_settings_explicit_both_fields_no_autoreset()
    test_settings_reset()
    test_match_parent_resolution()
    print("\n" + "=" * 60)
    print("PASS: all v0.8.0 smoke checks passed")


if __name__ == "__main__":
    main()
