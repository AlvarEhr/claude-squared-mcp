"""Smoke test for v0.11.0 — model-handling hardening.

Covers, with NO real claude calls (pure logic + synthetic result envelopes):
  1. Model-id parsing: normalize_model_id / parse_model_id edge cases.
  2. Substitution detector: the two historian-critical fixes —
     full version-tuple compare (opus-4-9 → opus-4-8) and [1m]/date
     normalization (no false-fire on 1M pairs / dated snapshot ids).
  3. newer_version_available (the drift / "newer model available" primitive).
  4. _select_primary_model: the haiku-helper-first modelUsage must not win.
  5. _build_send_result end-to-end: model_substitution / safety_signal /
     stop_reason populated from synthetic CLI result envelopes.
  6. _fallback_args → --fallback-model plumbing.
  7. Schema round-trips + backward-compat for the new PairSpec / PairDefaults
     / SendResult fields.
  8. Server wiring: _resolve_pair_create_args returns fallback_model;
     _HARDCODED_DEFAULTS has the key.

Run: PYTHONIOENCODING=utf-8 python -u tests/smoke_v0110.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from claude_squared.adapters.claude import (
    ClaudeAdapter,
    _extract_stop_details,
    _select_primary_model,
)
from claude_squared.models import (
    PairSpec,
    SendResult,
    model_substitution_note,
    newer_version_available,
    normalize_model_id,
    parse_model_id,
)


def test_normalize_and_parse():
    assert normalize_model_id("claude-fable-5[1m]") == "claude-fable-5"
    assert normalize_model_id("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert normalize_model_id("  CLAUDE-OPUS-4-8  ") == "claude-opus-4-8"
    assert parse_model_id("claude-opus-4-8") == ("opus", (4, 8))
    assert parse_model_id("claude-fable-5[1m]") == ("fable", (5,))
    assert parse_model_id("opus") == ("opus", ())  # alias: family, no version
    assert parse_model_id("claude-opus-4-6-fast") == ("opus", (4, 6))  # drop -fast
    assert parse_model_id("claude-haiku-4-5-20251001") == ("haiku", (4, 5))
    # legacy family-in-the-middle naming → no leading family token → (None, ...)
    fam, _ = parse_model_id("claude-3-5-sonnet")
    assert fam is None
    print("  [1] normalize / parse edge cases OK")


def test_substitution_detector():
    # --- must NOT fire (normal serves) ---
    # alias request, full id served, same family
    assert model_substitution_note("opus", "claude-opus-4-8") is None
    # [1m] request vs bare served (the tier-caveat collision — would false-fire
    # without normalization)
    assert model_substitution_note("claude-fable-5[1m]", "claude-fable-5") is None
    assert model_substitution_note("fable", "claude-fable-5[1m]") is None
    # newer served than requested (not a downgrade)
    assert model_substitution_note("claude-opus-4-8", "claude-opus-4-9") is None
    # unparseable side → silent (no false alarm)
    assert model_substitution_note("claude-3-5-sonnet", "claude-opus-4-8") is None

    # --- must fire ---
    # cross-family security downgrade (fable → opus-4-8)
    assert model_substitution_note("claude-fable-5[1m]", "claude-opus-4-8") is not None
    # within-family downgrade, MINOR version (the exact opus-4.X threat) — needs
    # full-tuple compare, would be missed by major-only parsing
    assert model_substitution_note("claude-opus-4-9", "claude-opus-4-8") is not None
    print("  [2] substitution detector (incl. both critical fixes) OK")


def test_newer_version_available():
    assert newer_version_available("claude-opus-4-8", "claude-opus-4-9") == "claude-opus-4-9"
    assert newer_version_available("claude-opus-4-8", "claude-opus-4-8") is None
    assert newer_version_available("claude-opus-4-8", "claude-fable-5") is None  # diff family
    assert newer_version_available("opus", "claude-opus-4-9") is None  # alias has no version
    assert newer_version_available("claude-opus-4-9", "claude-opus-4-8") is None  # candidate older
    # tier suffix on either side doesn't matter
    assert newer_version_available("claude-opus-4-8[1m]", "claude-opus-4-9") == "claude-opus-4-9"
    print("  [3] newer_version_available OK")


def test_select_primary_model():
    # haiku helper listed FIRST must not win — family match picks the real model
    mu = {
        "claude-haiku-4-5-20251001": {"contextWindow": 200000},
        "claude-fable-5[1m]": {"contextWindow": 1000000},
    }
    assert _select_primary_model(mu, "claude-fable-5[1m]") == "claude-fable-5[1m]"
    assert _select_primary_model(mu, "fable") == "claude-fable-5[1m]"
    # downgrade: requested family absent → max-window pick (real model, not helper)
    mu2 = {
        "claude-haiku-4-5-20251001": {"contextWindow": 200000},
        "claude-opus-4-8": {"contextWindow": 1000000},
    }
    assert _select_primary_model(mu2, "claude-fable-5[1m]") == "claude-opus-4-8"
    # empty modelUsage → requested
    assert _select_primary_model({}, "fable") == "fable"
    print("  [4] _select_primary_model (helper-first defeated) OK")


def _envelope(model_keys, *, stop_reason="end_turn", is_error=False,
              api_error_status=None, stop_details=None, result="ok"):
    mu = {k: {"contextWindow": w} for k, w in model_keys}
    env = {
        "result": result,
        "session_id": "sid",
        "modelUsage": mu,
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
        "stop_reason": stop_reason,
        "is_error": is_error,
        "total_cost_usd": 0.0,
        "duration_ms": 10,
    }
    if api_error_status is not None:
        env["api_error_status"] = api_error_status
    if stop_details is not None:
        env["stop_details"] = stop_details
    return env


def test_build_send_result():
    ad = ClaudeAdapter()
    # NORMAL turn (fable pair, haiku helper present) → no signals, 1M window
    spec = PairSpec(name="t", session_id="00000000-0000-0000-0000-000000000000",
                    model="claude-fable-5[1m]", cwd=str(Path.cwd()))
    r = ad._build_send_result(spec, _envelope(
        [("claude-haiku-4-5-20251001", 200000), ("claude-fable-5[1m]", 1000000)]))
    assert r.model_used == "claude-fable-5[1m]"
    assert r.context.tokens_max == 1000000  # NOT the helper's 200k
    assert r.model_substitution is None and r.safety_signal is None and r.stop_reason is None

    # SILENT DOWNGRADE (fable requested, served opus-4-8) → substitution fires
    r2 = ad._build_send_result(spec, _envelope(
        [("claude-haiku-4-5-20251001", 200000), ("claude-opus-4-8", 1000000)]))
    assert r2.model_used == "claude-opus-4-8"
    assert r2.model_substitution and "opus-4-8" in r2.model_substitution

    # REFUSAL with cyber category → safety_signal carries category, kind=refusal
    r3 = ad._build_send_result(spec, _envelope(
        [("claude-fable-5[1m]", 1000000)], stop_reason="refusal",
        stop_details={"category": "cyber", "explanation": "blocked"}))
    assert r3.safety_signal and "cyber" in r3.safety_signal
    assert r3.safety_kind == "refusal" and r3.stop_reason == "refusal"

    # API ERROR STATUS (transient, e.g. 529) → kind=error (NOT refusal, NOT
    # model_unavailable — must not render as SAFETY BLOCK)
    r4 = ad._build_send_result(spec, _envelope(
        [("claude-fable-5[1m]", 1000000)], is_error=True, api_error_status="overloaded_error"))
    assert r4.safety_signal and "overloaded_error" in r4.safety_signal
    assert r4.safety_kind == "error"

    # MODEL UNAVAILABLE (pulled / access lost — the Fable-5 case): result event
    # carries the marker text + api_error_status 404 + empty modelUsage. Must be
    # kind=model_unavailable (PERMANENT, actionable) — NOT "transient, retry".
    r5 = ad._build_send_result(spec, _envelope(
        [], is_error=True, api_error_status=404,
        result="There's an issue with the selected model (claude-fable-5[1m]). "
               "It may not exist or you may not have access to it."))
    assert r5.safety_kind == "model_unavailable", r5.safety_kind
    assert "pair_update" in r5.safety_signal  # actionable guidance, not "retry"
    assert r5.model_substitution is None  # empty modelUsage → requested == served, no false 🔄

    # drift_note is server-set, not adapter-set → always None from the adapter
    assert r.drift_note is None
    print("  [5] _build_send_result envelopes (normal/downgrade/refusal/api-error/model-gone) OK")


def test_stop_details_multipath():
    # category enrichment must work wherever the CLI puts stop_details
    assert _extract_stop_details({"stop_details": {"category": "bio"}}) == {"category": "bio"}
    assert _extract_stop_details(
        {"message": {"stop_details": {"category": "cyber"}}}) == {"category": "cyber"}
    assert _extract_stop_details({"result_meta": {"stop_details": {"x": 1}}}) == {"x": 1}
    assert _extract_stop_details({"stop_reason": "end_turn"}) is None
    # refusal nested under message → category still extracted end-to-end
    ad = ClaudeAdapter()
    spec = PairSpec(name="t", session_id="00000000-0000-0000-0000-000000000000",
                    model="claude-fable-5[1m]", cwd=str(Path.cwd()))
    env = _envelope([("claude-fable-5[1m]", 1000000)], stop_reason="refusal")
    env["message"] = {"stop_details": {"category": "bio"}}
    r = ad._build_send_result(spec, env)
    assert r.safety_kind == "refusal" and "bio" in r.safety_signal
    print("  [5b] _extract_stop_details multi-path + nested enrichment OK")


def test_fallback_args():
    assert ClaudeAdapter._fallback_args(
        PairSpec(name="a", session_id="s", model="opus", fallback_model="claude-opus-4-8")
    ) == ["--fallback-model", "claude-opus-4-8"]
    assert ClaudeAdapter._fallback_args(PairSpec(name="a", session_id="s", model="opus")) == []
    assert ClaudeAdapter._fallback_args(
        PairSpec(name="a", session_id="s", model="opus", fallback_model="  ")
    ) == []
    # comma-list passes through verbatim (CLI splits it)
    assert ClaudeAdapter._fallback_args(
        PairSpec(name="a", session_id="s", model="opus", fallback_model="m1,m2")
    ) == ["--fallback-model", "m1,m2"]
    print("  [6] _fallback_args plumbing OK")


def test_schema_roundtrips():
    s = PairSpec(name="x", session_id="sid", model="fable",
                 fallback_model="claude-opus-4-8", last_drift_notice="claude-opus-4-9")
    s2 = PairSpec.model_validate(s.model_dump())
    assert s2.fallback_model == "claude-opus-4-8" and s2.last_drift_notice == "claude-opus-4-9"
    # backward-compat: pre-v0.11.0 spec dict (no new fields) loads with defaults
    old = PairSpec.model_validate({"name": "y", "session_id": "s", "model": "opus"})
    assert old.fallback_model is None and old.last_drift_notice is None

    r = SendResult(name="x", response="hi", session_id="s", model_used="claude-opus-4-8",
                   cost_usd=0, duration_ms=1, model_substitution="m", safety_signal="s",
                   stop_reason="refusal", drift_note="d")
    r2 = SendResult.model_validate(r.model_dump())
    assert r2.model_substitution and r2.safety_signal and r2.stop_reason and r2.drift_note
    # backward-compat: old SendResult dict without new fields
    old_r = SendResult.model_validate({"name": "x", "response": "h", "session_id": "s",
                                       "model_used": "opus", "cost_usd": 0, "duration_ms": 1})
    assert old_r.model_substitution is None and old_r.drift_note is None

    from claude_squared.settings import PairDefaults
    d = PairDefaults(fallback_model="claude-opus-4-8")
    assert PairDefaults.model_validate(d.model_dump()).fallback_model == "claude-opus-4-8"
    print("  [7] schema round-trips + backward-compat OK")


def test_server_wiring():
    from claude_squared import server
    resolved, _ = server._resolve_pair_create_args(
        model="fable", effort=None, permission_mode=None, persistent=None,
        ultracode=None, fallback_model="claude-opus-4-8", parent_model=None)
    assert resolved["fallback_model"] == "claude-opus-4-8"
    resolved2, _ = server._resolve_pair_create_args(
        model="opus", effort=None, permission_mode=None, persistent=None,
        ultracode=None, fallback_model=None, parent_model=None)
    assert resolved2["fallback_model"] is None
    assert "fallback_model" in server._HARDCODED_DEFAULTS
    assert server._HARDCODED_DEFAULTS["fallback_model"] is None
    print("  [8] server wiring (_resolve_pair_create_args, _HARDCODED_DEFAULTS) OK")


def main():
    print("=" * 60)
    print("v0.11.0 model-handling hardening smoke")
    print("=" * 60)
    test_normalize_and_parse()
    test_substitution_detector()
    test_newer_version_available()
    test_select_primary_model()
    test_build_send_result()
    test_stop_details_multipath()
    test_fallback_args()
    test_schema_roundtrips()
    test_server_wiring()
    print("=" * 60)
    print("PASS: all v0.11.0 smoke checks passed")
    print("=" * 60)


if __name__ == "__main__":
    main()
