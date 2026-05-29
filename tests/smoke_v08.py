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
    """Tests the resolution ladder of _resolve_match_parent_model (v0.9.3):
       1. Explicit parent_model arg (no I/O)
       2. Exact env-var session JSONL → latest model
       3. Recency fallback: newest non-pair JSONL in cwd within the window
          (robust to a stale/frozen CLAUDE_CODE_SESSION_ID)
       4. Graceful static fallback to opus, with an honest message

    Also guards the v0.8.x dedup regression (_encode_cwd_for_project must accept
    str input) by exercising the JSONL path end-to-end."""
    print("\n=== Match-parent resolution ===")
    import time
    from claude_squared.server import _resolve_match_parent_model, _encode_cwd_for_project
    from claude_squared.registry import claude_home

    # 1. Explicit parent_model arg short-circuits — no env / file I/O needed
    result, msg = _resolve_match_parent_model("claude-sonnet-4-6")
    assert_eq(result, "claude-sonnet-4-6", "explicit parent_model returned")
    assert "explicit parent_model" in msg, "explicit-arg path message"

    encoded = _encode_cwd_for_project(str(Path.cwd()))
    jsonl_dir = claude_home() / "projects" / encoded

    def _reset_dir():
        if jsonl_dir.exists():
            for f in jsonl_dir.glob("*.jsonl"):
                f.unlink()
        jsonl_dir.mkdir(parents=True, exist_ok=True)

    def _write_jsonl(name, model, age_s=0.0):
        p = jsonl_dir / f"{name}.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            if model is not None:
                f.write(json.dumps({"type": "assistant", "message": {"model": model, "role": "assistant"}}) + "\n")
            else:
                f.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
        if age_s:
            t = time.time() - age_s
            os.utime(p, (t, t))
        return p

    saved_sid = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    try:
        # 2. Exact env-var session detection (last model in the file wins)
        _reset_dir()
        sid = "exact-sid"
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        p = jsonl_dir / f"{sid}.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-6"}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-4-6"}}) + "\n")
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "claude-sonnet-4-6", "exact env-var JSONL → latest model")
        assert "detected" in msg and "session JSONL" in msg, "exact-detection message"

        # 2e. Malformed lines tolerated; valid model still recovered
        with open(p, "w", encoding="utf-8") as f:
            f.write("THIS IS NOT JSON\n")
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-haiku-4-5"}}) + "\n")
            f.write("\x00\x01garbage\n")
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "claude-haiku-4-5", "malformed lines tolerated")

        # 3. RECENCY FALLBACK — env var stale (its JSONL missing), but a recent
        # non-pair JSONL exists → detect it. This is the v0.9.3 hardening.
        _reset_dir()
        os.environ["CLAUDE_CODE_SESSION_ID"] = "stale-sid-no-file"
        _write_jsonl("live-parent", "claude-opus-4-8")
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "claude-opus-4-8", "recency fallback detects live parent")
        assert "stale" in msg and "claude-opus-4-8" in msg, "recency-stale message"

        # 3b. No env var at all + recent JSONL → recency fallback ("unset")
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "claude-opus-4-8", "recency fallback with no env var")
        assert "unset" in msg, "recency-unset message"

        # 3c. A registered PAIR's JSONL must be EXCLUDED (no feedback loop).
        # Make the only recent JSONL a pair session → fall back to opus.
        _reset_dir()
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        from claude_squared import registry as reg_mod
        from claude_squared.models import PairSpec
        pair_sid = "pairs-own-session"
        reg_mod.add_pair(PairSpec(name="_mp_test_pair", session_id=pair_sid, model="opus"))
        try:
            _write_jsonl(pair_sid, "claude-opus-4-8")
            result, msg = _resolve_match_parent_model(None)
            assert_eq(result, "opus", "registered pair JSONL excluded → fallback")
        finally:
            reg_mod.remove_pair("_mp_test_pair")

        # 3d. Ambiguous — two recent non-pair JSONLs (concurrent sessions) → fallback
        _reset_dir()
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        _write_jsonl("sess-a", "claude-opus-4-8")
        _write_jsonl("sess-b", "claude-sonnet-4-6")
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "opus", "ambiguous concurrent sessions → fallback")
        assert "ambiguous" in msg or "concurrent" in msg, "ambiguous message"

        # 3e. A JSONL older than the recency window is not picked → fallback
        _reset_dir()
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        _write_jsonl("old-sess", "claude-opus-4-8", age_s=999.0)
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "opus", "JSONL beyond recency window → fallback")

        # 3f. Empty dir → fallback
        _reset_dir()
        result, msg = _resolve_match_parent_model(None)
        assert_eq(result, "opus", "no recent JSONL → fallback")
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
