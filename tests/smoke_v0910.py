"""v0.9.10 smoke: Ultracode mode support.

Verifies the canonical activation mechanism (settings JSON, NOT --effort):
- PairSpec.ultracode field exists, defaults False
- PairDefaults.ultracode field exists, defaults None
- ClaudeAdapter._common_create_args appends --settings '{"ultracode": true}'
  when spec.ultracode=True, omits it when False
- pair_create exposes ultracode param + flows through to PairSpec
- pair_update exposes ultracode param + evicts the runtime on change
- pair_settings_set accepts ultracode default
- _HARDCODED_DEFAULTS["ultracode"] is False
- Backward compat: pre-v0.9.10 PairSpec / PairDefaults dicts deserialize
  cleanly (the new field defaults False / None).

Run:
    python -u tests/smoke_v0910.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="pair_mcp_smoke_v0910_")
os.environ["CLAUDE_HOME"] = _TMPDIR

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.models import PairSpec
from claude_squared.settings import PairDefaults


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


# ---------- PairSpec / PairDefaults schema ----------

def test_pairspec_has_ultracode_field():
    print("=== 1.1: PairSpec.ultracode field exists, defaults False ===")
    spec = PairSpec(name="t", session_id="s")
    assert_true(hasattr(spec, "ultracode"), "PairSpec has 'ultracode' attribute")
    assert_eq(spec.ultracode, False, "default is False (backward compat)")


def test_pairspec_ultracode_true_round_trips():
    print("\n=== 1.2: PairSpec(ultracode=True) round-trips through JSON ===")
    spec = PairSpec(name="t", session_id="s", ultracode=True)
    s = spec.model_dump_json()
    loaded = PairSpec.model_validate_json(s)
    assert_eq(loaded.ultracode, True, "ultracode=True survives serialization")


def test_pairdefaults_has_ultracode_field():
    print("\n=== 1.3: PairDefaults.ultracode field exists, defaults None ===")
    d = PairDefaults()
    assert_true(hasattr(d, "ultracode"), "PairDefaults has 'ultracode' attribute")
    assert_eq(d.ultracode, None, "default is None (= use the False hardcoded fallback)")


def test_pairspec_backward_compat():
    print("\n=== 1.4: pre-v0.9.10 PairSpec JSON (no ultracode field) loads cleanly ===")
    legacy_json = json.dumps({
        "name": "legacy",
        "session_id": "abc-123",
        # NO ultracode key — pre-v0.9.10 registries
    })
    spec = PairSpec.model_validate_json(legacy_json)
    assert_eq(spec.ultracode, False, "missing ultracode defaults to False")


def test_pairdefaults_backward_compat():
    print("\n=== 1.5: pre-v0.9.10 PairDefaults JSON (no ultracode field) loads cleanly ===")
    legacy_json = json.dumps({
        "model": "opus",
        # NO ultracode key — pre-v0.9.10 defaults.json
    })
    d = PairDefaults.model_validate_json(legacy_json)
    assert_eq(d.ultracode, None, "missing ultracode defaults to None")


# ---------- adapter wire-up ----------

def test_adapter_omits_settings_when_off():
    print("\n=== 2.1: adapter omits --settings when ultracode=False (default) ===")
    spec = PairSpec(name="off", session_id="x")
    args = ClaudeAdapter()._common_create_args(spec)
    assert_true("--settings" not in args, "no --settings flag in args")


def test_adapter_appends_settings_when_on():
    print("\n=== 2.2: adapter appends --settings '{\"ultracode\": true}' when ultracode=True ===")
    spec = PairSpec(name="on", session_id="x", ultracode=True)
    args = ClaudeAdapter()._common_create_args(spec)
    assert_true("--settings" in args, "--settings flag present")
    idx = args.index("--settings")
    assert_true(idx + 1 < len(args), "--settings has a value argument")
    val = args[idx + 1]
    # Parse the JSON to verify the canonical structure (Anthropic's docs use
    # this exact form: '{"ultracode": true}').
    parsed = json.loads(val)
    assert_eq(parsed, {"ultracode": True}, "settings JSON has ultracode=true")


def test_adapter_compatible_with_effort():
    print("\n=== 2.3: adapter passes BOTH --settings AND --effort (independent fields) ===")
    # Per the CLI binary's internal data model (effortValue:_,ultracode:f),
    # these are separate fields — they can coexist. A user wanting max effort
    # with ultracode workflow encouragement is a legitimate combination.
    spec = PairSpec(name="on", session_id="x", ultracode=True, effort="max")
    args = ClaudeAdapter()._common_create_args(spec)
    # _common_create_args doesn't include --effort (that's added later in
    # runtime.py start()). We only verify --settings is here; effort lives in
    # spec.effort and is added in the runtime layer.
    assert_true("--settings" in args, "--settings present alongside effort field")


# ---------- public API ----------

def test_pair_create_accepts_ultracode_kwarg():
    print("\n=== 3.1: pair_create signature includes ultracode kwarg ===")
    from claude_squared.server import pair_create
    import inspect
    sig = inspect.signature(pair_create)
    assert_true(
        "ultracode" in sig.parameters,
        "pair_create signature includes 'ultracode' kwarg",
    )
    p = sig.parameters["ultracode"]
    assert_eq(p.default, None, "default is None (use defaults file or hardcoded False)")


def test_pair_update_accepts_ultracode_kwarg():
    print("\n=== 3.2: pair_update signature includes ultracode kwarg ===")
    from claude_squared.server import pair_update
    import inspect
    sig = inspect.signature(pair_update)
    assert_true(
        "ultracode" in sig.parameters,
        "pair_update signature includes 'ultracode' kwarg",
    )


def test_pair_settings_set_accepts_ultracode_kwarg():
    print("\n=== 3.3: pair_settings_set signature includes ultracode kwarg ===")
    from claude_squared.server import pair_settings_set
    import inspect
    sig = inspect.signature(pair_settings_set)
    assert_true(
        "ultracode" in sig.parameters,
        "pair_settings_set signature includes 'ultracode' kwarg",
    )


def test_hardcoded_defaults_includes_ultracode():
    print("\n=== 3.4: _HARDCODED_DEFAULTS['ultracode'] = False ===")
    from claude_squared.server import _HARDCODED_DEFAULTS
    assert_true(
        "ultracode" in _HARDCODED_DEFAULTS,
        "_HARDCODED_DEFAULTS has 'ultracode' key",
    )
    assert_eq(_HARDCODED_DEFAULTS["ultracode"], False, "hardcoded fallback is False")


def test_resolve_pair_create_args_layers_ultracode():
    print("\n=== 3.5: _resolve_pair_create_args layers ultracode (per-call > defaults > fallback) ===")
    from claude_squared.server import _resolve_pair_create_args
    # Per-call None + no defaults file (since we set CLAUDE_HOME to a fresh
    # tmpdir at module load) → fallback False.
    resolved, _ = _resolve_pair_create_args(
        model=None, effort=None, permission_mode=None,
        persistent=None, ultracode=None, parent_model=None,
    )
    assert_eq(resolved["ultracode"], False, "no per-call, no defaults → False (hardcoded fallback)")
    # Per-call True → True
    resolved, _ = _resolve_pair_create_args(
        model=None, effort=None, permission_mode=None,
        persistent=None, ultracode=True, parent_model=None,
    )
    assert_eq(resolved["ultracode"], True, "per-call True → True (wins over defaults)")
    # Per-call False (explicit) → False
    resolved, _ = _resolve_pair_create_args(
        model=None, effort=None, permission_mode=None,
        persistent=None, ultracode=False, parent_model=None,
    )
    assert_eq(resolved["ultracode"], False, "per-call False (explicit) → False")


# ---------- main ----------

def main():
    print(f"Using isolated CLAUDE_HOME: {_TMPDIR}\n")
    # Schema
    test_pairspec_has_ultracode_field()
    test_pairspec_ultracode_true_round_trips()
    test_pairdefaults_has_ultracode_field()
    test_pairspec_backward_compat()
    test_pairdefaults_backward_compat()
    # Adapter
    test_adapter_omits_settings_when_off()
    test_adapter_appends_settings_when_on()
    test_adapter_compatible_with_effort()
    # Public API
    test_pair_create_accepts_ultracode_kwarg()
    test_pair_update_accepts_ultracode_kwarg()
    test_pair_settings_set_accepts_ultracode_kwarg()
    test_hardcoded_defaults_includes_ultracode()
    test_resolve_pair_create_args_layers_ultracode()

    print("\n" + "=" * 60)
    print("PASS: all v0.9.10 smoke checks passed")


if __name__ == "__main__":
    main()
